"""Tests for providers/canvas_task_source.py.

No real HTTP calls — we monkeypatch `requests.get` on the provider module
and hand back scripted JSON responses. The goal is to lock in the
ROADBLOCKS §C1 (response trimming), §C2 (has_submitted_submissions →
is_completed), and §C3 (course code extraction) behaviors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from providers import canvas_task_source as canvas_mod
from providers.canvas_task_source import CanvasTaskSource


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_response(json_body, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: json_body,
        raise_for_status=lambda: None,
    )


class _FakeGet:
    """Records requests.get calls and dispatches canned responses by path."""
    def __init__(self, routes):
        # routes: dict[path_substring, json_body] or a callable(path, params) → body
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for key, body in self.routes.items():
            if key in url:
                if callable(body):
                    return _fake_response(body(url, params or {}))
                return _fake_response(body)
        return _fake_response([], status_code=404)


# ---------- Static helpers ----------

class TestStaticHelpers:
    def test_extract_course_code_basic(self):
        assert CanvasTaskSource._extract_course_code(
            "ECON104 - Dave Brown - SP26") == "ECON 104"

    def test_extract_course_code_with_space(self):
        assert CanvasTaskSource._extract_course_code(
            "MGMT 301, Section 008: Basic Mgmt") == "MGMT 301"

    def test_extract_course_code_none_when_no_match(self):
        assert CanvasTaskSource._extract_course_code("Random Seminar") is None

    def test_duration_hint_quiz_time_limit_wins(self):
        a = {"time_limit": 50, "name": "Midterm quiz",
             "submission_types": ["online_quiz"]}
        assert CanvasTaskSource._duration_hint(a) == 50

    def test_duration_hint_name_heuristics(self):
        assert CanvasTaskSource._duration_hint(
            {"name": "Read Chapter 12", "submission_types": []}) == 30
        assert CanvasTaskSource._duration_hint(
            {"name": "HW 4 Problem Set", "submission_types": []}) == 90
        assert CanvasTaskSource._duration_hint(
            {"name": "Final project proposal", "submission_types": []}) == 120

    def test_priority_hint_by_hours_until(self):
        now = datetime.now(timezone.utc)
        assert CanvasTaskSource._priority_hint(now + timedelta(hours=5)) == "asap"
        assert CanvasTaskSource._priority_hint(now + timedelta(hours=48)) == "high"
        assert CanvasTaskSource._priority_hint(now + timedelta(days=5)) == "medium"
        assert CanvasTaskSource._priority_hint(now + timedelta(days=21)) == "low"
        assert CanvasTaskSource._priority_hint(None) is None


# ---------- list_active_tasks ----------

class TestListActiveTasks:
    def _provider(self):
        return CanvasTaskSource(base_url="https://school.test/api/v1",
                                token="stub-token")

    def test_happy_path_returns_normalized_tasks(self, monkeypatch):
        now = datetime.now(timezone.utc)
        def router(url, params):
            if "/courses/" in url and "/assignments" in url:
                return [
                    {"id": 101, "name": "HW 1",
                     "due_at": _iso(now + timedelta(days=2)),
                     "has_submitted_submissions": False,
                     "submission_types": ["online_upload"]},
                    {"id": 102, "name": "Reading Ch. 5",
                     "due_at": _iso(now + timedelta(days=5)),
                     "has_submitted_submissions": False,
                     "submission_types": []},
                ]
            if url.endswith("/courses"):
                return [{"id": 1, "name": "ECON104 - Prof Smith - SP26"}]
            return []
        monkeypatch.setattr(canvas_mod.requests, "get",
                            _FakeGet({"courses": router}))

        out = self._provider().list_active_tasks()
        assert len(out) == 2
        titles = {t.title for t in out}
        assert titles == {"HW 1", "Reading Ch. 5"}
        assert all(t.source == "canvas" for t in out)
        assert all(t.course == "ECON 104" for t in out)
        # has_submitted=False on both → is_completed should be False.
        assert not any(t.is_completed for t in out)

    def test_submitted_assignment_marked_completed(self, monkeypatch):
        """ROADBLOCKS §C2: has_submitted_submissions → is_completed=True."""
        now = datetime.now(timezone.utc)
        def router(url, params):
            if "/assignments" in url:
                return [{
                    "id": 7, "name": "Lab 2",
                    "due_at": _iso(now + timedelta(days=1)),
                    "has_submitted_submissions": True,
                    "submission_types": ["online_upload"],
                }]
            if url.endswith("/courses"):
                return [{"id": 1, "name": "CHEM110 - Doe"}]
            return []
        monkeypatch.setattr(canvas_mod.requests, "get",
                            _FakeGet({"courses": router}))
        out = self._provider().list_active_tasks()
        assert len(out) == 1
        assert out[0].is_completed is True

    def test_out_of_window_assignments_filtered(self, monkeypatch):
        """ROADBLOCKS §C1 adjacent: _is_relevant drops assignments outside
        the -3d..+21d due-date window."""
        now = datetime.now(timezone.utc)
        def router(url, params):
            if "/assignments" in url:
                return [
                    {"id": 200, "name": "Way in the future",
                     "due_at": _iso(now + timedelta(days=60)),
                     "has_submitted_submissions": False,
                     "submission_types": []},
                    {"id": 201, "name": "Ancient",
                     "due_at": _iso(now - timedelta(days=60)),
                     "has_submitted_submissions": False,
                     "submission_types": []},
                    {"id": 202, "name": "In window",
                     "due_at": _iso(now + timedelta(days=5)),
                     "has_submitted_submissions": False,
                     "submission_types": []},
                ]
            if url.endswith("/courses"):
                return [{"id": 1, "name": "BIOL110"}]
            return []
        monkeypatch.setattr(canvas_mod.requests, "get",
                            _FakeGet({"courses": router}))
        out = self._provider().list_active_tasks()
        assert [t.title for t in out] == ["In window"]

    def test_assignments_without_due_at_dropped(self, monkeypatch):
        def router(url, params):
            if "/assignments" in url:
                return [{"id": 1, "name": "No due date",
                         "due_at": None,
                         "has_submitted_submissions": False,
                         "submission_types": []}]
            if url.endswith("/courses"):
                return [{"id": 1, "name": "BIOL110"}]
            return []
        monkeypatch.setattr(canvas_mod.requests, "get",
                            _FakeGet({"courses": router}))
        out = self._provider().list_active_tasks()
        assert out == []

    def test_course_fetch_failure_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("canvas down")
        monkeypatch.setattr(canvas_mod.requests, "get", boom)
        assert self._provider().list_active_tasks() == []

    def test_require_approval_attribute_propagates(self):
        p = CanvasTaskSource(base_url="https://school.test/api/v1",
                             token="stub-token", require_approval=True)
        assert p.require_approval is True
        health = p.health_check()
        # health_check makes an HTTP call; expect the error field, but the
        # require_approval flag should still surface.
        assert health["require_approval"] is True
