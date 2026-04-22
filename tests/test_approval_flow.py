"""End-to-end tests for the source-approval gate.

Flow being exercised:
  TaskSource(require_approval=True) → upsert_from_source(..., require_approval=True)
    → task lands in `pending_review`
    → solver.resolve() ignores it (no chunks)
    → tasks.approve(id) moves to `scheduled`
    → solver now places it
    → tasks.reject(id) moves to `hidden`

Uses the `db` fixture from conftest.py — no real providers or HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import solver
import tasks as tasks_mod
from providers.task_source import SourceTask


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _ny(y, m, d, hh=0, mm=0):
    """Convert a NY-local clock reading to UTC. April 20 2026 is EDT (UTC-4)."""
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


# ---------- upsert routing ----------

class TestUpsertRouting:
    def test_require_approval_false_goes_straight_to_scheduled(self, db):
        src = SourceTask(source="canvas", source_id="a-1",
                         title="HW 1", course="CS 101")
        row = tasks_mod.upsert_from_source(src, require_approval=False)
        assert row["status"] == "scheduled"

    def test_require_approval_true_lands_in_pending_review(self, db):
        src = SourceTask(source="canvas", source_id="a-1",
                         title="HW 1", course="CS 101")
        row = tasks_mod.upsert_from_source(src, require_approval=True)
        assert row["status"] == "pending_review"

    def test_update_does_not_demote_existing_task(self, db):
        """A task that's already in the active pool should not regress to
        pending_review when upsert runs again (even with require_approval=True)."""
        src = SourceTask(source="canvas", source_id="a-1",
                         title="HW 1", course="CS 101")
        first = tasks_mod.upsert_from_source(src, require_approval=False)
        assert first["status"] == "scheduled"
        second = tasks_mod.upsert_from_source(src, require_approval=True)
        assert second["status"] == "scheduled"

    def test_pending_review_stays_pending_on_resync(self, db):
        """Two upserts in a row with approval=True and no user action between
        them should leave the task still in pending_review (not duplicate)."""
        src = SourceTask(source="canvas", source_id="a-1",
                         title="HW 1", course="CS 101")
        tasks_mod.upsert_from_source(src, require_approval=True)
        row = tasks_mod.upsert_from_source(src, require_approval=True)
        assert row["status"] == "pending_review"
        assert len(tasks_mod.list_pending_review()) == 1


# ---------- list_active / solver ignore ----------

class TestSolverIgnoresPending:
    def test_pending_tasks_excluded_from_list_active(self, db):
        src = SourceTask(source="canvas", source_id="a-1", title="HW 1")
        tasks_mod.upsert_from_source(src, require_approval=True)
        assert tasks_mod.list_active() == []
        assert len(tasks_mod.list_pending_review()) == 1

    def test_resolve_does_not_place_pending_task(self, db):
        src = SourceTask(source="canvas", source_id="a-1", title="HW 1")
        tasks_mod.upsert_from_source(src, require_approval=True)
        result = solver.resolve(now_utc=_ny(2026, 4, 20, 9), window_days=1)
        assert result.chunks == []
        assert result.at_risk == []


# ---------- approve / reject ----------

class TestApproveReject:
    def test_approve_moves_to_scheduled(self, db):
        src = SourceTask(source="canvas", source_id="a-1", title="HW 1")
        row = tasks_mod.upsert_from_source(src, require_approval=True)
        updated = tasks_mod.approve(row["id"])
        assert updated["status"] == "scheduled"
        assert tasks_mod.list_pending_review() == []
        assert [t["id"] for t in tasks_mod.list_active()] == [row["id"]]

    def test_reject_moves_to_hidden(self, db):
        src = SourceTask(source="canvas", source_id="a-1", title="HW 1")
        row = tasks_mod.upsert_from_source(src, require_approval=True)
        updated = tasks_mod.reject(row["id"])
        assert updated["status"] == "hidden"
        assert tasks_mod.list_pending_review() == []
        assert tasks_mod.list_active() == []

    def test_approve_noop_on_already_scheduled(self, db, add_task):
        tid = add_task("active", 60, status="scheduled")
        updated = tasks_mod.approve(tid)
        assert updated["status"] == "scheduled"

    def test_reject_noop_on_already_done(self, db, add_task):
        tid = add_task("done", 60, status="done")
        updated = tasks_mod.reject(tid)
        assert updated["status"] == "done"

    def test_approve_missing_returns_none(self, db):
        assert tasks_mod.approve(9999) is None

    def test_reject_missing_returns_none(self, db):
        assert tasks_mod.reject(9999) is None

    def test_resolve_places_task_after_approval(self, db):
        """Full loop: pending → approved → solver places it."""
        src = SourceTask(source="canvas", source_id="a-1",
                         title="HW 1", duration_hint_min=60)
        row = tasks_mod.upsert_from_source(src, require_approval=True)
        pre = solver.resolve(now_utc=_ny(2026, 4, 20, 9), window_days=1)
        assert pre.chunks == []

        tasks_mod.approve(row["id"])
        post = solver.resolve(now_utc=_ny(2026, 4, 20, 9), window_days=1)
        assert len(post.chunks) == 1
        assert post.chunks[0].title == "HW 1"
