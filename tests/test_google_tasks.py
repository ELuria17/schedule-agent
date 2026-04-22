"""Tests for providers/google_tasks.py — mocked at the googleapiclient
layer (same pattern as test_google_calendar.py).

We fake `discovery.build() → service.tasklists().list().execute()` and
`service.tasks().list(...).execute()` with scripted response dicts,
then assert SourceTask normalization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers.google_tasks import GoogleTasksProvider


# ---------- Helpers (top of file — reviewable in isolation) ----------

class _FakeExec:
    def __init__(self, result, *, raise_exc=None):
        self._result = result
        self._raise = raise_exc

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeTaskLists:
    def __init__(self, lists):
        self._lists = lists
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        return _FakeExec({"items": self._lists})


class _FakeTasks:
    def __init__(self, tasks_by_listid):
        self._tasks_by_listid = tasks_by_listid
        self.list_calls: list[dict] = []

    def list(self, **kw):
        self.list_calls.append(kw)
        target = self._tasks_by_listid.get(kw["tasklist"], [])
        if isinstance(target, Exception):
            return _FakeExec(None, raise_exc=target)
        return _FakeExec({"items": target})


class _FakeTasksService:
    def __init__(self, lists, tasks_by_listid):
        self._tasklists = _FakeTaskLists(lists)
        self._tasks = _FakeTasks(tasks_by_listid)

    def tasklists(self):
        return self._tasklists

    def tasks(self):
        return self._tasks


@pytest.fixture
def patch_build(monkeypatch):
    state = {"service": None}

    def _fake_build(api, version, credentials=None, cache_discovery=True):
        assert api == "tasks"
        assert version == "v1"
        return state["service"]

    import sys, types
    fake_discovery = types.ModuleType("googleapiclient.discovery")
    fake_discovery.build = _fake_build
    fake_pkg = types.ModuleType("googleapiclient")
    fake_pkg.discovery = fake_discovery
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery)

    def install(service):
        state["service"] = service
    return install


@pytest.fixture
def stub_creds(monkeypatch):
    monkeypatch.setattr(
        GoogleTasksProvider, "_load_credentials",
        lambda self: SimpleNamespace(token="fake"),
    )


def _provider(tmp_path, **overrides):
    defaults = dict(
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "google_token.json",
    )
    defaults.update(overrides)
    return GoogleTasksProvider(**defaults)


def _raw_task(tid, title, *, status="needsAction", due=None, notes=None):
    item = {"id": tid, "title": title, "status": status}
    if due is not None:
        item["due"] = due
    if notes is not None:
        item["notes"] = notes
    return item


# ---------- Tests ----------

def test_fetches_from_all_lists_when_list_ids_is_none(
        tmp_path, patch_build, stub_creds):
    lists = [
        {"id": "list-A", "title": "Personal"},
        {"id": "list-B", "title": "Work"},
    ]
    tasks_by_listid = {
        "list-A": [_raw_task("t1", "Buy groceries")],
        "list-B": [_raw_task("t2", "Draft memo")],
    }
    patch_build(_FakeTasksService(lists, tasks_by_listid))

    p = _provider(tmp_path)
    out = p.list_active_tasks()
    assert {t.source_id for t in out} == {"list-A::t1", "list-B::t2"}
    assert all(t.source == "google_tasks" for t in out)


def test_list_ids_filter_respected(tmp_path, patch_build, stub_creds):
    lists = [
        {"id": "list-A", "title": "Personal"},
        {"id": "list-B", "title": "Work"},
    ]
    tasks_by_listid = {
        "list-A": [_raw_task("t1", "Buy groceries")],
        "list-B": [_raw_task("t2", "Draft memo")],
    }
    svc = _FakeTasksService(lists, tasks_by_listid)
    patch_build(svc)

    p = _provider(tmp_path, list_ids=["list-B"])
    out = p.list_active_tasks()
    # Only list-B should be scanned; tasklists().list() must NOT be called.
    assert [t.source_id for t in out] == ["list-B::t2"]
    assert svc._tasklists.list_calls == 0
    hit = {c["tasklist"] for c in svc._tasks.list_calls}
    assert hit == {"list-B"}


def test_completed_task_sets_is_completed_true(tmp_path, patch_build, stub_creds):
    lists = [{"id": "L", "title": "L"}]
    tasks = {
        "L": [
            _raw_task("a", "open", status="needsAction"),
            _raw_task("b", "done", status="completed"),
        ],
    }
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    by_id = {t.source_id: t for t in out}
    assert by_id["L::a"].is_completed is False
    assert by_id["L::b"].is_completed is True


def test_due_date_parses_to_timezone_aware_utc_datetime(
        tmp_path, patch_build, stub_creds):
    lists = [{"id": "L", "title": "L"}]
    tasks = {"L": [
        _raw_task("t1", "With due", due="2026-04-25T00:00:00.000Z"),
        _raw_task("t2", "No due"),
    ]}
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    by_id = {t.source_id: t for t in out}
    dl = by_id["L::t1"].deadline_utc
    assert dl is not None
    assert dl.tzinfo is not None
    assert dl == datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
    assert by_id["L::t2"].deadline_utc is None


def test_source_id_contains_list_id_and_task_id(tmp_path, patch_build, stub_creds):
    lists = [{"id": "my-list-id", "title": "X"}]
    tasks = {"my-list-id": [_raw_task("abc-123", "Ping support")]}
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    assert out[0].source_id == "my-list-id::abc-123"
    assert out[0].extra == {"list_id": "my-list-id"}


def test_notes_threaded_through(tmp_path, patch_build, stub_creds):
    lists = [{"id": "L", "title": "L"}]
    tasks = {"L": [_raw_task("t1", "Work", notes="check sheet 3")]}
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    assert out[0].notes == "check sheet 3"


def test_duration_and_priority_are_not_set(tmp_path, patch_build, stub_creds):
    lists = [{"id": "L", "title": "L"}]
    tasks = {"L": [_raw_task("t1", "A task")]}
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    assert out[0].duration_hint_min is None
    assert out[0].priority_hint is None


def test_broken_list_does_not_kill_batch(tmp_path, patch_build, stub_creds):
    lists = [
        {"id": "good", "title": "Good"},
        {"id": "bad", "title": "Bad"},
    ]
    tasks = {
        "good": [_raw_task("t1", "OK")],
        "bad": RuntimeError("API 500"),
    }
    patch_build(_FakeTasksService(lists, tasks))

    out = _provider(tmp_path).list_active_tasks()
    assert [t.source_id for t in out] == ["good::t1"]


def test_showcompleted_and_showhidden_false(tmp_path, patch_build, stub_creds):
    lists = [{"id": "L", "title": "L"}]
    tasks = {"L": [_raw_task("t1", "Task")]}
    svc = _FakeTasksService(lists, tasks)
    patch_build(svc)

    _provider(tmp_path).list_active_tasks()
    call = svc._tasks.list_calls[0]
    assert call["showCompleted"] is False
    assert call["showHidden"] is False
    assert call["tasklist"] == "L"


def test_require_approval_propagates(tmp_path):
    p = _provider(tmp_path, require_approval=True)
    assert p.require_approval is True
    p2 = _provider(tmp_path)
    assert p2.require_approval is False


# ---------- health_check ----------

def test_health_check_missing_credentials(tmp_path):
    info = _provider(tmp_path).health_check()
    assert info["source"] == "google_tasks"
    assert info["token_present"] is False
    assert "credentials.json not found" in info["error"]


def test_health_check_missing_token(tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    info = _provider(tmp_path).health_check()
    assert info["token_present"] is False
    assert "refresh token" in info["error"].lower()


def test_health_check_success_shape(tmp_path, patch_build, stub_creds):
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")
    lists = [
        {"id": "L1", "title": "Personal"},
        {"id": "L2", "title": "Work"},
    ]
    tasks = {
        "L1": [_raw_task("a", "x"), _raw_task("b", "y")],
        "L2": [_raw_task("c", "z")],
    }
    patch_build(_FakeTasksService(lists, tasks))

    info = _provider(tmp_path).health_check()
    assert info["source"] == "google_tasks"
    assert info["list_count"] == 2
    assert info["total_tasks_visible"] == 3
    assert "error" not in info
