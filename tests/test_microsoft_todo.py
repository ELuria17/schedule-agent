"""Tests for providers/microsoft_todo.py.

Mocks the microsoft_graph_auth helpers and verifies list iteration,
task normalization, importance → priority_hint mapping, completion
status, and due-date parsing. No msal, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from providers import microsoft_todo as mod
from providers.microsoft_todo import MicrosoftTodoProvider


class _FakeGraph:
    def __init__(self, *, lists=None, tasks_by_list=None):
        self.lists = lists or []
        self.tasks_by_list = tasks_by_list or {}
        self.get_calls: list[str] = []

    def get_access_token(self, *, client_id, tenant="common",
                         token_path=None, scopes=None):
        return "fake-token"

    def graph_get(self, token, path, params=None, **kw):
        self.get_calls.append(path)
        if path == "/me/todo/lists":
            return {"value": [dict(l) for l in self.lists]}
        if path.startswith("/me/todo/lists/"):
            list_id = path.split("/me/todo/lists/")[1].split("/")[0]
            return {"value": list(self.tasks_by_list.get(list_id, []))}
        return {"value": []}

    def token_status(self, *, token_path):
        return {"token_present": True, "accounts": ["u@example.com"]}


@pytest.fixture
def patch_graph(monkeypatch):
    def _install(fg):
        monkeypatch.setattr(mod.mg, "get_access_token", fg.get_access_token)
        monkeypatch.setattr(mod.mg, "graph_get", fg.graph_get)
        monkeypatch.setattr(mod.mg, "token_status", fg.token_status)
        return fg
    return _install


def _task(tid, *, title="Do thing", status="notStarted", importance="normal",
          due=None, body=None):
    t = {"id": tid, "title": title, "status": status,
         "importance": importance, "body": body}
    if due:
        t["dueDateTime"] = due
    return t


def _provider(tmp_path, **kw):
    defaults = dict(
        client_id="abc",
        token_path=tmp_path / "microsoft_token.json",
    )
    defaults.update(kw)
    return MicrosoftTodoProvider(**defaults)


# ---------- list_active_tasks ----------

class TestListActiveTasks:
    def test_fetches_from_all_lists(self, tmp_path, patch_graph):
        fg = patch_graph(_FakeGraph(
            lists=[
                {"id": "L1", "displayName": "Tasks"},
                {"id": "L2", "displayName": "Groceries"},
            ],
            tasks_by_list={
                "L1": [_task("t1", title="Write proposal", importance="high")],
                "L2": [_task("t2", title="Milk", importance="low")],
            },
        ))
        out = _provider(tmp_path).list_active_tasks()
        by_id = {t.source_id: t for t in out}
        assert "L1::t1" in by_id
        assert "L2::t2" in by_id
        assert by_id["L1::t1"].priority_hint == "high"
        assert by_id["L2::t2"].priority_hint == "low"
        assert by_id["L1::t1"].course == "Tasks"
        assert by_id["L2::t2"].course == "Groceries"

    def test_list_id_filter_restricts(self, tmp_path, patch_graph):
        fg = patch_graph(_FakeGraph(
            lists=[
                {"id": "L1", "displayName": "Tasks"},
                {"id": "L2", "displayName": "Groceries"},
            ],
            tasks_by_list={
                "L1": [_task("t1")], "L2": [_task("t2")],
            },
        ))
        out = _provider(tmp_path, list_ids=["L2"]).list_active_tasks()
        ids = [t.source_id for t in out]
        assert ids == ["L2::t2"]

    def test_completed_tasks_are_emitted_as_completed(self, tmp_path, patch_graph):
        patch_graph(_FakeGraph(
            lists=[{"id": "L1", "displayName": "Tasks"}],
            tasks_by_list={"L1": [
                _task("active", status="notStarted"),
                _task("done", status="completed"),
                _task("wip", status="inProgress"),
            ]},
        ))
        out = _provider(tmp_path).list_active_tasks()
        by_id = {t.source_id: t for t in out}
        assert by_id["L1::active"].is_completed is False
        assert by_id["L1::done"].is_completed is True
        assert by_id["L1::wip"].is_completed is False

    def test_importance_mapping(self, tmp_path, patch_graph):
        patch_graph(_FakeGraph(
            lists=[{"id": "L1", "displayName": "Tasks"}],
            tasks_by_list={"L1": [
                _task("h", importance="high"),
                _task("n", importance="normal"),
                _task("l", importance="low"),
                _task("none", importance=""),
            ]},
        ))
        out = _provider(tmp_path).list_active_tasks()
        hints = {t.source_id: t.priority_hint for t in out}
        assert hints["L1::h"] == "high"
        assert hints["L1::n"] is None
        assert hints["L1::l"] == "low"
        assert hints["L1::none"] is None

    def test_due_date_parsing(self, tmp_path, patch_graph):
        patch_graph(_FakeGraph(
            lists=[{"id": "L1", "displayName": "Tasks"}],
            tasks_by_list={"L1": [
                _task("a", due={"dateTime": "2026-04-25T17:00:00.0000000",
                                "timeZone": "UTC"}),
                _task("b", due={"dateTime": "2026-04-26T09:30:00",
                                "timeZone": "UTC"}),
                _task("none"),  # no due
            ]},
        ))
        out = _provider(tmp_path).list_active_tasks()
        by_id = {t.source_id: t for t in out}
        assert by_id["L1::a"].deadline_utc == datetime(2026, 4, 25, 17, tzinfo=timezone.utc)
        assert by_id["L1::b"].deadline_utc == datetime(2026, 4, 26, 9, 30, tzinfo=timezone.utc)
        assert by_id["L1::none"].deadline_utc is None

    def test_body_passes_through_as_notes(self, tmp_path, patch_graph):
        patch_graph(_FakeGraph(
            lists=[{"id": "L1", "displayName": "Tasks"}],
            tasks_by_list={"L1": [
                _task("a", body={"contentType": "text", "content": "Remember to include chart"}),
                _task("b"),  # no body
            ]},
        ))
        out = _provider(tmp_path).list_active_tasks()
        by_id = {t.source_id: t for t in out}
        assert by_id["L1::a"].notes == "Remember to include chart"
        assert by_id["L1::b"].notes is None

    def test_empty_when_auth_fails(self, tmp_path, monkeypatch):
        def _raise(**kw):
            raise RuntimeError("no token")
        monkeypatch.setattr(mod.mg, "get_access_token", _raise)
        assert _provider(tmp_path).list_active_tasks() == []

    def test_empty_when_lists_fetch_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.mg, "get_access_token", lambda **kw: "t")
        def _raise(token, path, params=None, **kw):
            raise RuntimeError("500")
        monkeypatch.setattr(mod.mg, "graph_get", _raise)
        assert _provider(tmp_path).list_active_tasks() == []

    def test_one_bad_list_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.mg, "get_access_token", lambda **kw: "t")

        def _get(token, path, params=None, **kw):
            if path == "/me/todo/lists":
                return {"value": [
                    {"id": "L1", "displayName": "OK"},
                    {"id": "Lbad", "displayName": "Broken"},
                ]}
            if path.startswith("/me/todo/lists/Lbad/"):
                raise RuntimeError("nope")
            if path.startswith("/me/todo/lists/L1/"):
                return {"value": [_task("t1", title="Alive")]}
            return {"value": []}

        monkeypatch.setattr(mod.mg, "graph_get", _get)
        out = _provider(tmp_path).list_active_tasks()
        assert [t.source_id for t in out] == ["L1::t1"]


# ---------- health_check ----------

class TestHealthCheck:
    def test_reports_missing_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.mg, "token_status",
                            lambda *, token_path: {"token_present": False,
                                                    "accounts": []})
        info = _provider(tmp_path).health_check()
        assert info["token_present"] is False
        assert "authorize_microsoft" in info["error"]

    def test_reports_ready(self, tmp_path, patch_graph):
        patch_graph(_FakeGraph())
        info = _provider(tmp_path).health_check()
        assert info["token_present"] is True
        assert info["source"] == "microsoft_todo"
        assert "error" not in info
