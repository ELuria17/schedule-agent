"""Tests for providers/todoist_task_source.py — mocked at `requests.get`.

Same pattern as test_canvas_provider.py: monkeypatch the provider
module's `requests.get` with a scripted fake that returns canned Todoist
JSON payloads, then assert the SourceTask normalization is correct.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers import todoist_task_source as todoist_mod
from providers.todoist_task_source import TodoistTaskSource


def _fake_response(body, status=200):
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        raise_for_status=lambda: None,
    )


class _FakeGet:
    """Records requests.get calls and dispatches canned responses."""
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for key, body in self.routes.items():
            if key in url:
                return _fake_response(
                    body(url, params) if callable(body) else body
                )
        return _fake_response([], status=404)


# ---------- Happy path ----------

class TestListActiveTasks:
    def _provider(self, **kw):
        kw.setdefault("token", "stub-token")
        return TodoistTaskSource(**kw)

    def test_happy_path_normalizes_tasks(self, monkeypatch):
        payload = [
            {
                "id": "123", "content": "Write Q3 report",
                "description": "Include data from August.",
                "due": {"date": "2026-05-01", "datetime": "2026-05-01T17:00:00Z"},
                "priority": 3,
                "labels": ["Work"],
                "project_id": "p-work",
                "url": "https://todoist.com/task/123",
                "is_completed": False,
            },
        ]
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": payload}))

        out = self._provider().list_active_tasks()
        assert len(out) == 1
        t = out[0]
        assert t.source == "todoist"
        assert t.source_id == "123"
        assert t.title == "Write Q3 report"
        assert t.priority_hint == "high"
        assert t.course == "Work"
        assert t.deadline_utc == datetime(2026, 5, 1, 17, tzinfo=timezone.utc)
        assert t.notes == "Include data from August."
        assert t.is_completed is False
        assert t.extra["project_id"] == "p-work"
        assert t.extra["url"].endswith("/123")

    def test_priority_mapping_covers_all_levels(self, monkeypatch):
        payload = [
            {"id": "1", "content": "p4", "priority": 4, "labels": []},
            {"id": "2", "content": "p3", "priority": 3, "labels": []},
            {"id": "3", "content": "p2", "priority": 2, "labels": []},
            {"id": "4", "content": "p1", "priority": 1, "labels": []},
        ]
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": payload}))
        out = self._provider().list_active_tasks()
        assert [t.priority_hint for t in out] == ["asap", "high", "medium", "low"]

    def test_date_only_due_becomes_end_of_day_utc(self, monkeypatch):
        """A due 'date' (no time) should be treated as 23:59 UTC so the
        solver still schedules it before the deadline."""
        payload = [{
            "id": "1", "content": "Due someday",
            "due": {"date": "2026-04-25"},   # no datetime
            "priority": 1, "labels": [],
        }]
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": payload}))
        out = self._provider().list_active_tasks()
        assert out[0].deadline_utc == datetime(2026, 4, 25, 23, 59, tzinfo=timezone.utc)

    def test_no_due_means_no_deadline(self, monkeypatch):
        payload = [{
            "id": "1", "content": "Whenever",
            "due": None, "priority": 1, "labels": [],
        }]
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": payload}))
        out = self._provider().list_active_tasks()
        assert out[0].deadline_utc is None

    def test_default_duration_hint_applied(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": [
                                {"id": "1", "content": "x", "priority": 1, "labels": []}
                            ]}))
        out = self._provider(default_duration_min=75).list_active_tasks()
        assert out[0].duration_hint_min == 75

    def test_untitled_task_gets_placeholder(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": [
                                {"id": "1", "content": "", "priority": 1, "labels": []}
                            ]}))
        out = self._provider().list_active_tasks()
        assert out[0].title == "(untitled Todoist task)"


# ---------- Filters ----------

class TestFilters:
    def _payload(self):
        return [
            {"id": "a", "content": "Work item", "project_id": "p-work",
             "labels": ["focus"], "priority": 1},
            {"id": "b", "content": "Home item", "project_id": "p-home",
             "labels": ["errand"], "priority": 1},
            {"id": "c", "content": "Side item", "project_id": "p-side",
             "labels": ["focus", "side-proj"], "priority": 1},
        ]

    def test_project_filter_drops_non_matching(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": self._payload()}))
        p = TodoistTaskSource(token="t", project_ids=["p-work", "p-side"])
        ids = [t.source_id for t in p.list_active_tasks()]
        assert ids == ["a", "c"]

    def test_label_filter_keeps_overlap(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": self._payload()}))
        p = TodoistTaskSource(token="t", label_filter=["focus"])
        ids = [t.source_id for t in p.list_active_tasks()]
        assert ids == ["a", "c"]

    def test_filters_combine_additively(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": self._payload()}))
        p = TodoistTaskSource(
            token="t", project_ids=["p-work", "p-home"],
            label_filter=["errand"],
        )
        ids = [t.source_id for t in p.list_active_tasks()]
        assert ids == ["b"]


# ---------- Error handling ----------

class TestErrorHandling:
    def test_http_error_returns_empty_list(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("todoist down")
        monkeypatch.setattr(todoist_mod.requests, "get", boom)
        assert TodoistTaskSource(token="t").list_active_tasks() == []

    def test_malformed_due_gracefully_ignored(self, monkeypatch):
        payload = [{
            "id": "1", "content": "Bad due",
            "due": {"datetime": "not-a-date"},
            "priority": 1, "labels": [],
        }]
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/tasks": payload}))
        out = TodoistTaskSource(token="t").list_active_tasks()
        assert out[0].deadline_utc is None


# ---------- health_check ----------

class TestHealthCheck:
    def test_reports_project_count(self, monkeypatch):
        monkeypatch.setattr(todoist_mod.requests, "get",
                            _FakeGet({"/projects": [
                                {"id": "p1", "name": "Work"},
                                {"id": "p2", "name": "Home"},
                            ]}))
        info = TodoistTaskSource(token="t").health_check()
        assert info["source"] == "todoist"
        assert info["projects_visible"] == 2
        assert "error" not in info

    def test_reports_error_when_request_fails(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("no network")
        monkeypatch.setattr(todoist_mod.requests, "get", boom)
        info = TodoistTaskSource(token="t").health_check()
        assert "error" in info
        assert "RuntimeError" in info["error"]

    def test_require_approval_surfaces_in_health(self):
        p = TodoistTaskSource(token="t", require_approval=True)
        assert p.require_approval is True
