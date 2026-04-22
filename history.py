"""History persistence: solver runs + agent sessions.

These were originally in-memory deques (`_SOLVER_LOG`, `SESSIONS`,
`SESSION_ORDER`). An orchestrator restart — via launchd KeepAlive or a
Docker redeploy — wiped them, so the hub's activity lists reset to empty.
This module mirrors the same records into SQLite so history survives.

The orchestrator still keeps the in-memory structures for live work
(active SSE subscribers, mid-flight event fan-out). This module is a
second writer + authoritative reader; the memory copy is a live cache.

All timestamps are ISO-8601 strings. Event payloads are JSON-serialized;
callers should pass dicts that `json.dumps(default=str)` can handle.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from config import connect


# ---------- Solver log ----------

def record_solver_run(entry: dict) -> int:
    """Append one solver-run record. Returns the row id. Never raises —
    a failed write logs to stderr and returns 0 (history is best-effort)."""
    payload = json.dumps(entry, default=str)
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO solver_log(ts, trigger, chunks, at_risk, error, entry_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    entry.get("ts") or "",
                    entry.get("trigger") or "unknown",
                    entry.get("chunks"),
                    entry.get("at_risk"),
                    entry.get("error"),
                    payload,
                ),
            )
            return int(cur.lastrowid or 0)
    except Exception as ex:
        import sys
        print(f"[history] record_solver_run failed: {ex}", file=sys.stderr)
        return 0


def list_solver_runs(limit: int = 30) -> list[dict]:
    """Most-recent-first list of solver runs, reconstituted from stored JSON."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT entry_json FROM solver_log ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["entry_json"]))
        except Exception:
            continue
    return out


def trim_solver_log(keep: int = 200) -> int:
    """Delete all but the most recent `keep` rows. Returns number deleted."""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM solver_log").fetchone()["n"]
        if total <= keep:
            return 0
        # Keep the N newest; delete older rows by id.
        cutoff_id = conn.execute(
            "SELECT id FROM solver_log ORDER BY id DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        ).fetchone()
        if cutoff_id is None:
            return 0
        cur = conn.execute("DELETE FROM solver_log WHERE id < ?", (cutoff_id["id"],))
        return int(cur.rowcount or 0)


# ---------- Sessions ----------

def upsert_session(
    *,
    session_id: str,
    agent_key: str,
    title: Optional[str],
    status: str,
    started_at: str,
    finished_at: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Insert or update a session row. Called when a session is created, when
    the Anthropic-assigned id becomes known (status still 'running'), and on
    each status transition (done / error / cancelled)."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id, agent_key, title, status, started_at, finished_at, error) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  agent_key=excluded.agent_key, "
                "  title=COALESCE(excluded.title, sessions.title), "
                "  status=excluded.status, "
                "  finished_at=excluded.finished_at, "
                "  error=excluded.error",
                (session_id, agent_key, title, status, started_at, finished_at, error),
            )
    except Exception as ex:
        import sys
        print(f"[history] upsert_session failed: {ex}", file=sys.stderr)


def rename_session(old_id: str, new_id: str) -> None:
    """Move a session's row from pending_<uuid> to the real Anthropic session id.
    Copy-insert the new parent first, reparent the child rows, then delete the
    old parent — that way the FK from session_events never references a
    missing sessions row, even momentarily."""
    try:
        with connect() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "INSERT INTO sessions(id, agent_key, title, status, started_at, finished_at, error) "
                    "SELECT ?, agent_key, title, status, started_at, finished_at, error "
                    "FROM sessions WHERE id=?",
                    (new_id, old_id),
                )
                conn.execute(
                    "UPDATE session_events SET session_id=? WHERE session_id=?",
                    (new_id, old_id),
                )
                conn.execute("DELETE FROM sessions WHERE id=?", (old_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as ex:
        import sys
        print(f"[history] rename_session failed: {ex}", file=sys.stderr)


def list_sessions(limit: int = 20) -> list[dict]:
    """Most-recent-first session rows. Event counts joined in."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.id, s.agent_key, s.title, s.status, s.started_at,
                      s.finished_at, s.error,
                      (SELECT COUNT(*) FROM session_events e WHERE e.session_id=s.id) AS event_count
               FROM sessions s
               ORDER BY s.started_at DESC, s.id DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------- Session events ----------

def append_session_event(session_id: str, seq: int, ts: str, envelope: dict) -> None:
    try:
        payload = json.dumps(envelope, default=str)
    except Exception:
        payload = json.dumps({"type": "unserializable",
                              "original_type": type(envelope).__name__})
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO session_events(session_id, seq, ts, event_json) VALUES(?,?,?,?)",
                (session_id, int(seq), ts, payload),
            )
    except Exception as ex:
        import sys
        print(f"[history] append_session_event failed: {ex}", file=sys.stderr)


def get_session_events(session_id: str) -> list[dict]:
    """Ordered event list for a session."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_json FROM session_events "
            "WHERE session_id=? ORDER BY seq ASC, id ASC",
            (session_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["event_json"]))
        except Exception:
            continue
    return out
