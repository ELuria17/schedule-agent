"""Tests for tasks.py — data-access layer over SQLite.

Covers ROADBLOCKS signals:
- §F4: ORDER BY (deadline_ts IS NULL), deadline_ts ASC — nulls-last.
- §D2: Canvas-authoritative reopen on upsert_from_source when upstream is open.
- §D1/§R4 adjacent: mark_done_by_source idempotency + history capture.

Uses the `db` fixture from conftest.py, which monkeypatches config.DB_PATH.
`tasks` imports `connect` from `config` at module load — but `connect` reads
DB_PATH at call time, so the monkeypatch carries through.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import tasks as tasks_mod
from providers.task_source import SourceTask


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ---------- list_all / list_active ordering (§F4) ----------

class TestListOrdering:
    def test_nulls_last_ordering(self, db, add_task):
        """ROADBLOCKS §F4: tasks with no deadline must sort after tasks with
        a deadline, then by deadline ascending, then by id ascending."""
        t_none = add_task("no deadline", 60)
        t_soon = add_task("soon", 60, deadline_ts=_utc(2026, 4, 22, 12).isoformat())
        t_later = add_task("later", 60, deadline_ts=_utc(2026, 5, 1, 12).isoformat())
        rows = tasks_mod.list_active()
        ids = [r["id"] for r in rows]
        assert ids == [t_soon, t_later, t_none]

    def test_equal_deadlines_break_tie_on_id(self, db, add_task):
        ts = _utc(2026, 4, 22, 12).isoformat()
        t1 = add_task("a", 60, deadline_ts=ts)
        t2 = add_task("b", 60, deadline_ts=ts)
        rows = tasks_mod.list_active()
        assert [r["id"] for r in rows] == [t1, t2]

    def test_list_active_excludes_done_and_hidden(self, db, add_task):
        active = add_task("active", 60)
        add_task("done", 60, status="done")
        add_task("hidden", 60, status="hidden")
        assert [r["id"] for r in tasks_mod.list_active()] == [active]


# ---------- upsert_from_source (§D2) ----------

class TestUpsertFromSource:
    def test_insert_when_new(self, db):
        src = SourceTask(
            source="canvas", source_id="assn-1",
            title="HW 1", course="CS 101",
            deadline_utc=_utc(2026, 4, 25, 23, 59),
            duration_hint_min=45, priority_hint="high",
        )
        row = tasks_mod.upsert_from_source(src)
        assert row["title"] == "HW 1"
        assert row["course"] == "CS 101"
        assert row["duration_min"] == 45
        assert row["priority"] == "high"
        assert row["status"] == "scheduled"
        assert row["source"] == "canvas"
        assert row["source_id"] == "assn-1"

    def test_update_preserves_in_progress_status(self, db):
        """A user might set status to in_progress locally. An upsert from
        the source shouldn't clobber that back to scheduled."""
        src = SourceTask(source="canvas", source_id="assn-1",
                         title="HW 1", course="CS 101")
        row = tasks_mod.upsert_from_source(src)
        tasks_mod.update(row["id"], status="in_progress")
        # Second upsert from same source should not override in_progress
        row2 = tasks_mod.upsert_from_source(src)
        assert row2["id"] == row["id"]
        assert row2["status"] == "in_progress"

    def test_d2_reopens_done_task_when_upstream_still_active(self, db):
        """ROADBLOCKS §D2: if we marked a source-backed task done (e.g. via
        a reminder-match false positive) but upstream still reports it open,
        the next upsert reopens it."""
        src = SourceTask(source="canvas", source_id="assn-1",
                         title="Essay draft", course="ENG 101")
        row = tasks_mod.upsert_from_source(src)
        # Simulate reminder-match false positive closing the task.
        tasks_mod.mark_complete(row["id"])
        assert tasks_mod.get(row["id"])["status"] == "done"
        # Canvas says it's still open. Reopen.
        row2 = tasks_mod.upsert_from_source(src)
        assert row2["status"] == "scheduled"
        assert row2["completed_at"] is None

    def test_update_refreshes_title_and_deadline(self, db):
        src = SourceTask(source="canvas", source_id="assn-1",
                         title="Old title", course="ENG 101",
                         deadline_utc=_utc(2026, 4, 25))
        tasks_mod.upsert_from_source(src)
        src2 = SourceTask(source="canvas", source_id="assn-1",
                          title="Renamed", course="ENG 101",
                          deadline_utc=_utc(2026, 4, 30))
        row = tasks_mod.upsert_from_source(src2)
        assert row["title"] == "Renamed"
        assert row["deadline_ts"].startswith("2026-04-30")


# ---------- mark_done_by_source ----------

class TestMarkDoneBySource:
    def test_marks_task_done(self, db):
        src = SourceTask(source="canvas", source_id="assn-9",
                         title="Lab 3", course="CHEM 110")
        row = tasks_mod.upsert_from_source(src)
        out = tasks_mod.mark_done_by_source("canvas", "assn-9")
        assert out is not None
        assert out["id"] == row["id"]
        assert out["status"] == "done"
        assert out["completed_at"] is not None

    def test_idempotent_on_already_done(self, db):
        src = SourceTask(source="canvas", source_id="assn-9",
                         title="Lab 3", course="CHEM 110")
        tasks_mod.upsert_from_source(src)
        first = tasks_mod.mark_done_by_source("canvas", "assn-9")
        second = tasks_mod.mark_done_by_source("canvas", "assn-9")
        # Same id, still done. The early-return skips re-writing completed_at.
        assert first["id"] == second["id"]
        assert second["status"] == "done"

    def test_returns_none_when_missing(self, db):
        assert tasks_mod.mark_done_by_source("canvas", "nonexistent") is None

    def test_records_history_when_actual_min_given(self, db):
        src = SourceTask(source="canvas", source_id="a-1",
                         title="Quiz", course="BIO 110",
                         duration_hint_min=30)
        row = tasks_mod.upsert_from_source(src)
        tasks_mod.mark_done_by_source("canvas", "a-1", actual_min=42)
        # Next same-course same-token task should get ~42 as suggested duration.
        guess = tasks_mod.suggest_duration("Quiz 2", "BIO 110")
        assert guess == 42


# ---------- Duration suggestion from history ----------

class TestSuggestDuration:
    def test_no_history_returns_none(self, db):
        assert tasks_mod.suggest_duration("anything", "SUBJ 101") is None

    def test_median_of_matching_history(self, db):
        # Three completed tasks in the same course with a shared "essay" token.
        for actual in (30, 60, 90):
            src = SourceTask(source="canvas", source_id=f"e-{actual}",
                             title="Essay draft", course="ENG 201")
            tasks_mod.upsert_from_source(src)
            tasks_mod.mark_done_by_source("canvas", f"e-{actual}", actual_min=actual)
        assert tasks_mod.suggest_duration("Essay final", "ENG 201") == 60

    def test_course_filter_excludes_other_courses(self, db):
        src = SourceTask(source="canvas", source_id="m-1",
                         title="Problem set", course="MATH 101")
        tasks_mod.upsert_from_source(src)
        tasks_mod.mark_done_by_source("canvas", "m-1", actual_min=120)
        # Same title, different course → no match.
        assert tasks_mod.suggest_duration("Problem set", "PHYS 101") is None
