"""
Task data-access layer. Plain sqlite3 — no ORM.

Tasks are the input to the solver. A Task has a title, estimated duration,
optional deadline, priority, min/max chunk size, preferred window, and a
(source, source_id) tuple pointing back to where it came from.

External task sources (Canvas, Notion, Linear, etc.) go through the
`upsert_from_source` path — they hand over a provider-neutral `SourceTask`
and this module owns the persistence, the Canvas-style "upstream says open →
reopen any locally-done row" invariant, and the history-informed duration
refinement.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from config import connect
from providers.task_source import SourceTask


# ---------- Internal helpers ----------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return dict(row)


# Canvas assignment name → heuristic duration (minutes).
# Solver uses duration_min directly; this is only the default when upserting
# from Canvas without a history-derived estimate.
def _title_tokens(s: str) -> set:
    import re
    out = set()
    for w in re.findall(r"[A-Za-z0-9]+", (s or "").lower()):
        if len(w) >= 3 or (len(w) >= 2 and w.isdigit()):
            out.add(w)
    return out - {"the", "and", "due", "hw", "for", "from", "with"}


def suggest_duration(title: str, course: Optional[str]) -> Optional[int]:
    """Return median actual_min from task_history for similar completed tasks.

    Similar = same course (case-insensitive) and ≥1 shared significant
    title token. Returns None if no history matches (caller falls back to
    the rule-based default).
    """
    toks = _title_tokens(title)
    if not toks:
        return None
    with connect() as conn:
        rows = conn.execute(
            """SELECT h.actual_min, t.title AS ttitle, t.course AS tcourse
               FROM task_history h JOIN tasks t ON t.id = h.task_id
               WHERE h.actual_min IS NOT NULL"""
        ).fetchall()
    course_up = (course or "").strip().upper()
    matches: list[int] = []
    for r in rows:
        rc = (r["tcourse"] or "").strip().upper()
        if course_up and rc != course_up:
            continue
        if not toks & _title_tokens(r["ttitle"]):
            continue
        matches.append(int(r["actual_min"]))
    if not matches:
        return None
    matches.sort()
    return matches[len(matches) // 2]


_DEFAULT_DURATION_MIN = 60
_DEFAULT_PRIORITY = "medium"


# ---------- Public API ----------

def create(
    *,
    title: str,
    duration_min: int,
    deadline_ts: Optional[str] = None,
    priority: str = "medium",
    course: Optional[str] = None,
    min_chunk_min: int = 30,
    max_chunk_min: int = 120,
    preferred_window: Optional[str] = None,
    source: str = "manual",
    source_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Insert a new task. Returns the full row dict."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO tasks
               (title, course, duration_min, deadline_ts, priority,
                min_chunk_min, max_chunk_min, preferred_window,
                source, source_id, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, ?)""",
            (title, course, duration_min, deadline_ts, priority,
             min_chunk_min, max_chunk_min, preferred_window,
             source, source_id, notes, _now_utc_iso()),
        )
        task_id = cur.lastrowid
    return get(task_id)


def upsert_from_source(source_task: SourceTask,
                       *, require_approval: bool = False) -> dict:
    """Insert or update a task sourced from any external provider.

    Key is (source, source_id). On update, we refresh provider-owned fields
    (title, deadline, course) and — crucially — reopen any locally-done task
    if the upstream still considers it active. That's the "Canvas is
    authoritative" rule that prevents reminder-match toggle loops (ROADBLOCKS §D2).

    `require_approval=True` routes NEW tasks from this source to the
    `pending_review` queue instead of `scheduled`, so the solver won't
    place them on the calendar until a user explicitly approves. Existing
    rows are never demoted back to pending_review on update — once a task
    has been reviewed (or predates the flag), it stays in the live pool.

    Duration estimation: history-derived (via `suggest_duration`) wins when
    available, then the provider's hint, then a generic default.
    """
    deadline_str = (source_task.deadline_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if source_task.deadline_utc else None)
    duration = (
        suggest_duration(source_task.title, source_task.course)
        or source_task.duration_hint_min
        or _DEFAULT_DURATION_MIN
    )
    priority = source_task.priority_hint or _DEFAULT_PRIORITY
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE source=? AND source_id=?",
            (source_task.source, source_task.source_id),
        ).fetchone()
        now = _now_utc_iso()
        if existing is None:
            initial_status = "pending_review" if require_approval else "scheduled"
            cur = conn.execute(
                """INSERT INTO tasks
                   (title, course, duration_min, deadline_ts, priority,
                    status, source, source_id, notes, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (source_task.title, source_task.course, duration, deadline_str,
                 priority, initial_status, source_task.source, source_task.source_id,
                 source_task.notes, now),
            )
            return get(cur.lastrowid)
        # Upstream is authoritative. The caller only reaches here when
        # source_task.is_completed is False (completed items take the
        # mark_done_by_source path); if our DB flagged it done via a
        # reminder-match false positive, reopen it.
        new_status = existing["status"]
        if existing["status"] == "done":
            new_status = "scheduled"
        conn.execute(
            """UPDATE tasks SET
                 title = ?,
                 course = COALESCE(?, course),
                 deadline_ts = ?,
                 status = ?,
                 completed_at = CASE WHEN ? = 'scheduled' THEN NULL ELSE completed_at END,
                 updated_at = ?
               WHERE id = ?""",
            (source_task.title, source_task.course, deadline_str,
             new_status, new_status, now, existing["id"]),
        )
        return get(existing["id"])


def mark_done_by_source(source: str, source_id: str, *, actual_min: Optional[int] = None) -> Optional[dict]:
    """Mark a task done by its source pointer. Returns the updated row or None if not found."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if existing is None:
            return None
        if existing["status"] == "done":
            return dict(existing)
        now = _now_utc_iso()
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
            (now, now, existing["id"]),
        )
        if actual_min is not None:
            conn.execute(
                "INSERT INTO task_history(task_id, estimated_min, actual_min) VALUES(?,?,?)",
                (existing["id"], existing["duration_min"], actual_min),
            )
        return get(existing["id"])


def mark_complete(task_id: int, *, actual_min: Optional[int] = None) -> Optional[dict]:
    """Mark a task done by id. Records history if actual_min provided."""
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if existing is None:
            return None
        now = _now_utc_iso()
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
            (now, now, task_id),
        )
        if actual_min is not None:
            conn.execute(
                "INSERT INTO task_history(task_id, estimated_min, actual_min) VALUES(?,?,?)",
                (task_id, existing["duration_min"], actual_min),
            )
    return get(task_id)


def update(task_id: int, **fields) -> Optional[dict]:
    """Partial update. Only allowlisted columns.

    A write to `duration_min` flips `duration_locked=1`, which pins the
    value and prevents `relearn_durations()` from overriding it. This is
    the "the user knows how long this really takes" signal.
    """
    allowed = {
        "title", "course", "duration_min", "deadline_ts", "priority",
        "status", "min_chunk_min", "max_chunk_min", "preferred_window",
        "notes",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get(task_id)
    # If the user is editing duration explicitly, pin it.
    if "duration_min" in data:
        data["duration_locked"] = 1
    data["updated_at"] = _now_utc_iso()
    cols = ", ".join(f"{k}=?" for k in data)
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*data.values(), task_id))
    return get(task_id)


def relearn_durations(*, min_change_pct: float = 0.10) -> int:
    """Refresh duration estimates for source-originated tasks from history.

    Walks every active task whose `duration_locked=0` and `source != 'manual'`,
    re-runs `suggest_duration()` against the current `task_history`, and
    writes back any estimate that differs by at least `min_change_pct`.

    Called at the top of each solver cycle so the plan reflects what the
    user actually takes on similar work, not just the original provider
    hint. Returns the number of rows updated (useful for logging).
    """
    updated = 0
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, title, course, duration_min, source
               FROM tasks
               WHERE status IN ('scheduled','in_progress')
                 AND duration_locked = 0
                 AND source != 'manual'"""
        ).fetchall()
    for r in rows:
        s = suggest_duration(r["title"], r["course"])
        if s is None:
            continue
        current = int(r["duration_min"])
        if current <= 0:
            continue
        if abs(s - current) / current < min_change_pct:
            continue
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET duration_min=?, updated_at=? WHERE id=?",
                (s, _now_utc_iso(), r["id"]),
            )
        updated += 1
    return updated


def delete(task_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return cur.rowcount > 0


def get(task_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_dict(row)


def list_all(status_filter: Optional[Iterable[str]] = None) -> list[dict]:
    with connect() as conn:
        if status_filter:
            status_list = list(status_filter)
            placeholders = ",".join("?" * len(status_list))
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY (deadline_ts IS NULL), deadline_ts ASC, id ASC",
                tuple(status_list),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY (deadline_ts IS NULL), deadline_ts ASC, id ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def list_active() -> list[dict]:
    """All tasks the solver should consider (not done, not hidden, not pending review)."""
    return list_all(status_filter=("scheduled", "in_progress", "blocked"))


def list_pending_review() -> list[dict]:
    """Tasks awaiting user approval before entering the scheduling pool.

    Populated when a TaskSource is configured with require_approval=True.
    The solver ignores these; the hub renders them for approve/reject.
    """
    return list_all(status_filter=("pending_review",))


def approve(task_id: int) -> Optional[dict]:
    """Move a pending-review task into the scheduling pool. No-op if the
    task isn't in pending_review. Returns the updated row or None if missing."""
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if existing is None:
            return None
        if existing["status"] != "pending_review":
            return dict(existing)
        conn.execute(
            "UPDATE tasks SET status='scheduled', updated_at=? WHERE id=?",
            (_now_utc_iso(), task_id),
        )
    return get(task_id)


def reject(task_id: int) -> Optional[dict]:
    """Decline a pending-review task. Moves it to 'hidden' so it stays in the
    source-upsert key (preventing re-creation on every sync) but doesn't clutter
    active views. Returns the updated row or None if missing."""
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if existing is None:
            return None
        if existing["status"] != "pending_review":
            return dict(existing)
        conn.execute(
            "UPDATE tasks SET status='hidden', updated_at=? WHERE id=?",
            (_now_utc_iso(), task_id),
        )
    return get(task_id)


def list_by_source(source: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE source=? ORDER BY id", (source,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Scheduled chunks (what the solver wrote to iCloud) ----------

def record_chunks(task_id: int, chunks: list[tuple[str, str, str]]) -> None:
    """chunks: list of (calendar_event_uid, start_ts_utc, end_ts_utc)."""
    with connect() as conn:
        conn.executemany(
            "INSERT INTO scheduled_chunks(task_id, calendar_event_uid, start_ts, end_ts) VALUES(?,?,?,?)",
            [(task_id, uid, s, e) for (uid, s, e) in chunks],
        )


def clear_chunks_in_window(start_utc: str, end_utc: str) -> list[dict]:
    """Return (and delete) all scheduled_chunks with start_ts in [start_utc, end_utc).
    Used by the clear-and-replace loop so the solver can re-place fresh.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_chunks WHERE start_ts >= ? AND start_ts < ?",
            (start_utc, end_utc),
        ).fetchall()
        conn.execute(
            "DELETE FROM scheduled_chunks WHERE start_ts >= ? AND start_ts < ?",
            (start_utc, end_utc),
        )
    return [dict(r) for r in rows]


def list_chunks(task_id: Optional[int] = None) -> list[dict]:
    with connect() as conn:
        if task_id is None:
            rows = conn.execute("SELECT * FROM scheduled_chunks ORDER BY start_ts").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_chunks WHERE task_id=? ORDER BY start_ts",
                (task_id,),
            ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # Smoke test
    from config import connect as _c
    _c()  # bootstrap
    print(f"Active tasks: {len(list_active())}")
    print(f"All tasks:    {len(list_all())}")
