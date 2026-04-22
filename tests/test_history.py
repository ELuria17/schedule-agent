"""Tests for history.py — solver-log + session persistence.

Uses the `db` fixture (throwaway SQLite bootstrapped with the full schema).
History is append-only from the orchestrator's POV; these tests exercise
the CRUD surface directly.
"""
from __future__ import annotations

import json

import pytest

import history


# ---------- Solver log ----------

class TestSolverLog:
    def test_record_then_list(self, db):
        history.record_solver_run({
            "trigger": "manual", "ts": "2026-04-21T10:00:00-04:00",
            "chunks": 3, "at_risk": 0, "scheduled_min": 180,
        })
        runs = history.list_solver_runs()
        assert len(runs) == 1
        assert runs[0]["trigger"] == "manual"
        assert runs[0]["chunks"] == 3

    def test_list_most_recent_first(self, db):
        for i, ts in enumerate(("2026-04-20T09:00:00-04:00",
                                "2026-04-21T09:00:00-04:00",
                                "2026-04-22T09:00:00-04:00")):
            history.record_solver_run({"trigger": f"run_{i}", "ts": ts, "chunks": i})
        runs = history.list_solver_runs()
        triggers = [r["trigger"] for r in runs]
        assert triggers == ["run_2", "run_1", "run_0"]

    def test_limit_respected(self, db):
        for i in range(5):
            history.record_solver_run({"trigger": f"r{i}",
                                       "ts": f"2026-04-{20 + i}T00:00:00-04:00"})
        assert len(history.list_solver_runs(limit=3)) == 3

    def test_error_run_captured(self, db):
        history.record_solver_run({
            "trigger": "poll", "ts": "2026-04-21T10:00:00-04:00",
            "error": "ValueError: boom",
        })
        runs = history.list_solver_runs()
        assert runs[0].get("error") == "ValueError: boom"

    def test_trim_keeps_newest(self, db):
        for i in range(10):
            history.record_solver_run({"trigger": f"r{i}",
                                       "ts": f"2026-04-{10 + i}T00:00:00-04:00"})
        deleted = history.trim_solver_log(keep=3)
        assert deleted == 7
        remaining = history.list_solver_runs(limit=10)
        assert [r["trigger"] for r in remaining] == ["r9", "r8", "r7"]


# ---------- Sessions ----------

class TestSessions:
    def test_upsert_then_get(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent",
            title="Morning plan", status="running",
            started_at="2026-04-21T08:00:00-04:00",
        )
        got = history.get_session("ses_1")
        assert got["agent_key"] == "schedule-agent"
        assert got["status"] == "running"
        assert got["title"] == "Morning plan"

    def test_upsert_updates_status_and_finished(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Plan",
            status="running", started_at="2026-04-21T08:00:00-04:00",
        )
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Plan",
            status="done", started_at="2026-04-21T08:00:00-04:00",
            finished_at="2026-04-21T08:05:00-04:00",
        )
        got = history.get_session("ses_1")
        assert got["status"] == "done"
        assert got["finished_at"] == "2026-04-21T08:05:00-04:00"
        assert got["error"] is None

    def test_error_status_captures_error_text(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Plan",
            status="running", started_at="2026-04-21T08:00:00-04:00",
        )
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Plan",
            status="error", started_at="2026-04-21T08:00:00-04:00",
            finished_at="2026-04-21T08:01:00-04:00",
            error="RuntimeError: upstream down",
        )
        got = history.get_session("ses_1")
        assert got["status"] == "error"
        assert got["error"] == "RuntimeError: upstream down"

    def test_list_sessions_most_recent_first(self, db):
        for i, ts in enumerate(("2026-04-20T10:00:00-04:00",
                                "2026-04-21T10:00:00-04:00",
                                "2026-04-22T10:00:00-04:00")):
            history.upsert_session(
                session_id=f"ses_{i}", agent_key="schedule-agent",
                title=f"Run {i}", status="done", started_at=ts,
                finished_at=ts,
            )
        out = history.list_sessions()
        ids = [s["id"] for s in out]
        assert ids == ["ses_2", "ses_1", "ses_0"]

    def test_list_sessions_includes_event_count(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Run",
            status="running", started_at="2026-04-21T10:00:00-04:00",
        )
        history.append_session_event("ses_1", 0, "2026-04-21T10:00:01-04:00",
                                     {"type": "status", "status": "running"})
        history.append_session_event("ses_1", 1, "2026-04-21T10:00:02-04:00",
                                     {"type": "message", "text": "hi"})
        rows = history.list_sessions()
        assert rows[0]["event_count"] == 2

    def test_rename_migrates_session_and_events(self, db):
        history.upsert_session(
            session_id="pending_abc", agent_key="schedule-agent", title="Run",
            status="running", started_at="2026-04-21T10:00:00-04:00",
        )
        history.append_session_event("pending_abc", 0, "2026-04-21T10:00:01-04:00",
                                     {"type": "status", "status": "running"})
        history.rename_session("pending_abc", "ses_xyz")
        assert history.get_session("pending_abc") is None
        moved = history.get_session("ses_xyz")
        assert moved["status"] == "running"
        events = history.get_session_events("ses_xyz")
        assert len(events) == 1
        assert events[0]["type"] == "status"

    def test_invalid_status_silently_logged_not_raised(self, db, capsys):
        """history write failures are intentionally swallowed — a bad history
        insert shouldn't crash a live agent run. The error goes to stderr."""
        history.upsert_session(
            session_id="s", agent_key="schedule-agent", title="t",
            status="bogus", started_at="2026-04-21T00:00:00-04:00",
        )
        captured = capsys.readouterr()
        assert "CHECK constraint failed" in captured.err
        assert history.get_session("s") is None


# ---------- Session events ----------

class TestSessionEvents:
    def test_events_preserve_seq_order(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Run",
            status="running", started_at="2026-04-21T10:00:00-04:00",
        )
        # Insert out of order — ordering is by seq, not insert order.
        history.append_session_event("ses_1", 2, "ts2", {"type": "a"})
        history.append_session_event("ses_1", 0, "ts0", {"type": "b"})
        history.append_session_event("ses_1", 1, "ts1", {"type": "c"})
        events = history.get_session_events("ses_1")
        assert [e["type"] for e in events] == ["b", "c", "a"]

    def test_events_deleted_when_session_deleted(self, db):
        """Foreign-key ON DELETE CASCADE cleans up events."""
        import sqlite3
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Run",
            status="running", started_at="2026-04-21T10:00:00-04:00",
        )
        history.append_session_event("ses_1", 0, "t", {"type": "x"})
        conn = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM sessions WHERE id=?", ("ses_1",))
        conn.close()
        assert history.get_session_events("ses_1") == []

    def test_unserializable_payload_still_stored(self, db):
        history.upsert_session(
            session_id="ses_1", agent_key="schedule-agent", title="Run",
            status="running", started_at="2026-04-21T10:00:00-04:00",
        )
        class Weird:
            pass
        history.append_session_event("ses_1", 0, "t", {"type": "x", "obj": Weird()})
        events = history.get_session_events("ses_1")
        # Payload replaced with a marker; the record isn't lost.
        assert len(events) == 1
