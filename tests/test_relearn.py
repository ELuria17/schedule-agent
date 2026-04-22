"""Tests for the continuous learning loop.

Exercises tasks.update() pinning duration on explicit edits, and
tasks.relearn_durations() rewriting estimates from task_history for
unpinned source tasks only.
"""
from __future__ import annotations

import pytest


def _record_history(conn, task_id: int, estimated_min: int, actual_min: int):
    conn.execute(
        "INSERT INTO task_history(task_id, estimated_min, actual_min) "
        "VALUES(?,?,?)", (task_id, estimated_min, actual_min),
    )


def _make_task(conn, *, title, course, duration_min, source, source_id=None,
               status="scheduled", duration_locked=0):
    cur = conn.execute(
        "INSERT INTO tasks(title, course, duration_min, source, source_id, "
        "status, duration_locked) VALUES(?,?,?,?,?,?,?)",
        (title, course, duration_min, source, source_id, status, duration_locked),
    )
    return cur.lastrowid


def _make_completed(conn, *, title, course, actual_min):
    """Insert a DONE task with a matching task_history row — the shape
    suggest_duration() walks to compute a median."""
    cur = conn.execute(
        "INSERT INTO tasks(title, course, duration_min, source, status) "
        "VALUES(?,?,?,?,?)",
        (title, course, 60, "canvas", "done"),
    )
    tid = cur.lastrowid
    _record_history(conn, tid, 60, actual_min)
    return tid


class TestUpdateLocksDuration:
    def test_duration_edit_sets_locked(self, db):
        import tasks as tasks_mod
        with db._connect() as conn:
            tid = _make_task(
                conn, title="Acme proposal", course="Client",
                duration_min=60, source="canvas", source_id="1",
            )

        tasks_mod.update(tid, duration_min=90)
        with db._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["duration_min"] == 90
        assert row["duration_locked"] == 1

    def test_non_duration_edits_do_not_lock(self, db):
        import tasks as tasks_mod
        with db._connect() as conn:
            tid = _make_task(
                conn, title="Acme proposal", course="Client",
                duration_min=60, source="canvas", source_id="1",
            )

        tasks_mod.update(tid, title="Acme proposal v2", priority="high")
        with db._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["title"] == "Acme proposal v2"
        assert row["duration_locked"] == 0


class TestRelearnDurations:
    def test_updates_source_task_from_history(self, db):
        import tasks as tasks_mod

        with db._connect() as conn:
            # Three completed tasks, matching course + shared title token.
            # Median of [40,45,50] = 45 min.
            for actual in (40, 45, 50):
                _make_completed(conn, title="Draft proposal for Acme",
                                course="Client", actual_min=actual)
            # Active task with stale 90-min estimate.
            tid = _make_task(
                conn, title="Draft proposal for Beta Corp", course="Client",
                duration_min=90, source="canvas", source_id="9",
            )

        n = tasks_mod.relearn_durations()
        assert n == 1
        with db._connect() as conn:
            row = conn.execute("SELECT duration_min FROM tasks WHERE id=?",
                               (tid,)).fetchone()
        assert row["duration_min"] == 45

    def test_skips_locked_task(self, db):
        import tasks as tasks_mod

        with db._connect() as conn:
            for actual in (40, 45, 50):
                _make_completed(conn, title="Draft proposal for Acme",
                                course="Client", actual_min=actual)
            tid = _make_task(
                conn, title="Draft proposal for Beta", course="Client",
                duration_min=90, source="canvas", source_id="9",
                duration_locked=1,   # user said 90, leave it alone
            )

        tasks_mod.relearn_durations()
        with db._connect() as conn:
            row = conn.execute("SELECT duration_min FROM tasks WHERE id=?",
                               (tid,)).fetchone()
        assert row["duration_min"] == 90

    def test_skips_manual_source(self, db):
        import tasks as tasks_mod

        with db._connect() as conn:
            for actual in (40, 45, 50):
                _make_completed(conn, title="Draft proposal for Acme",
                                course="Client", actual_min=actual)
            tid = _make_task(
                conn, title="Draft proposal for Beta", course="Client",
                duration_min=90, source="manual",
            )

        tasks_mod.relearn_durations()
        with db._connect() as conn:
            row = conn.execute("SELECT duration_min FROM tasks WHERE id=?",
                               (tid,)).fetchone()
        assert row["duration_min"] == 90

    def test_skips_small_change_under_threshold(self, db):
        import tasks as tasks_mod

        with db._connect() as conn:
            # History suggests ~58 min; original estimate 60. Diff ≈ 3% → skip.
            for actual in (56, 58, 60):
                _make_completed(conn, title="Daily standup notes",
                                course="Ops", actual_min=actual)
            tid = _make_task(
                conn, title="Daily standup notes writeup", course="Ops",
                duration_min=60, source="canvas", source_id="9",
            )

        n = tasks_mod.relearn_durations()
        assert n == 0  # below 10% threshold
        with db._connect() as conn:
            row = conn.execute("SELECT duration_min FROM tasks WHERE id=?",
                               (tid,)).fetchone()
        assert row["duration_min"] == 60

    def test_applies_only_to_active_tasks(self, db):
        """Done / blocked / pending_review tasks don't need re-estimating."""
        import tasks as tasks_mod

        with db._connect() as conn:
            for actual in (40, 45, 50):
                _make_completed(conn, title="Draft proposal for Acme",
                                course="Client", actual_min=actual)
            tids = [
                _make_task(conn, title="Draft proposal for D1", course="Client",
                           duration_min=90, source="canvas", source_id="a",
                           status="done"),
                _make_task(conn, title="Draft proposal for D2", course="Client",
                           duration_min=90, source="canvas", source_id="b",
                           status="blocked"),
                _make_task(conn, title="Draft proposal for D3", course="Client",
                           duration_min=90, source="canvas", source_id="c",
                           status="pending_review"),
                _make_task(conn, title="Draft proposal for D4", course="Client",
                           duration_min=90, source="canvas", source_id="d",
                           status="scheduled"),  # this one DOES get updated
            ]

        tasks_mod.relearn_durations()
        with db._connect() as conn:
            durations = {r["status"]: r["duration_min"] for r in conn.execute(
                "SELECT status, duration_min FROM tasks WHERE id IN (?,?,?,?)",
                tuple(tids),
            )}
        assert durations["done"] == 90
        assert durations["blocked"] == 90
        assert durations["pending_review"] == 90
        assert durations["scheduled"] == 45
