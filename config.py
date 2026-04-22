"""
SQLite schema + config access for the Motion-style schedule-agent rebuild.

Owns:
- Database lifecycle (open + schema bootstrap on first import).
- Config seed data: working hours, fixed recurring blocks (classes).
- Readers consumed by the solver.

All timestamps in the DB are stored as ISO-8601 UTC strings with 'Z' suffix.
Local-time fields in `config_blocks` / `config_hours` are 'HH:MM' strings in
the user's home timezone (see schedule_config.TIMEZONE).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from paths import state_db_path

# Location of the SQLite DB. In dev (git checkout) this resolves to the
# repo dir. In a packaged / frozen build it lives under the OS's
# per-user Application Support path. See paths.py for the full rules.
# Tests monkeypatch this attribute directly, so it stays a module-level
# Path rather than a call-on-access property.
DB_PATH = state_db_path()

# Timezone is the user's home TZ. Default matches schedule_config.TIMEZONE's
# default; kept as an env-var read here so this module has no import-time
# dependency on schedule_config (which pulls in provider credentials).
TZ_NAME = os.environ.get("TIMEZONE", "America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    course TEXT,
    duration_min INTEGER NOT NULL,
    deadline_ts TEXT,                                       -- ISO 8601 UTC, nullable
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('asap','high','medium','low')),
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','in_progress','done','blocked','hidden','pending_review')),
    min_chunk_min INTEGER NOT NULL DEFAULT 30,
    max_chunk_min INTEGER NOT NULL DEFAULT 120,
    preferred_window TEXT
        CHECK (preferred_window IS NULL OR preferred_window IN ('morning','afternoon','evening')),
    source TEXT NOT NULL DEFAULT 'manual',                  -- 'canvas' | 'manual' | 'llm'
    source_id TEXT,                                          -- external id (e.g. canvas assignment id)
    notes TEXT,
    completed_at TEXT,
    duration_locked INTEGER NOT NULL DEFAULT 0,              -- 1 once user has edited duration_min
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source
    ON tasks (source, source_id) WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks (deadline_ts);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    estimated_min INTEGER NOT NULL,
    actual_min    INTEGER,
    completed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS config_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
        CHECK (kind IN ('class','sleep','shabbat','gym_typical','custom')),
    label TEXT NOT NULL,
    rrule TEXT,                                              -- e.g. 'FREQ=WEEKLY;BYDAY=MO,WE,FR'
    start_local TEXT NOT NULL,                               -- 'HH:MM' in user TZ
    end_local   TEXT NOT NULL,                               -- 'HH:MM' in user TZ
    tz TEXT NOT NULL DEFAULT 'America/New_York'
);

CREATE TABLE IF NOT EXISTS config_hours (
    dow INTEGER PRIMARY KEY CHECK (dow BETWEEN 0 AND 6),     -- Python weekday(): Mon=0..Sun=6
    start_local TEXT NOT NULL,                               -- 'HH:MM'
    end_local   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    calendar_event_uid TEXT NOT NULL,
    start_ts TEXT NOT NULL,                                  -- ISO 8601 UTC
    end_ts   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_window ON scheduled_chunks (start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_chunks_task ON scheduled_chunks (task_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Records that a specific completed reminder was once matched to a task and
-- used to close it. Prevents the reminder from re-closing the same task after
-- Canvas re-opens it (Canvas is authoritative for Canvas-sourced tasks).
CREATE TABLE IF NOT EXISTS reminder_matches (
    reminder_uid TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    matched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (reminder_uid, task_id)
);

-- Append-only log of solver runs, so the hub keeps history across restarts.
-- The orchestrator also caches the last N in memory for live UI updates.
CREATE TABLE IF NOT EXISTS solver_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                                        -- ISO-8601 (local tz ok; produced by orchestrator)
    trigger TEXT NOT NULL,                                   -- 'manual' | 'task_mutation' | 'hub_stale' | 'poll' | ...
    chunks INTEGER,
    at_risk INTEGER,
    error TEXT,
    entry_json TEXT NOT NULL                                 -- full entry dict for replay
);
CREATE INDEX IF NOT EXISTS idx_solver_log_ts ON solver_log (ts DESC);

-- Session metadata. One row per agent session launched via the orchestrator.
-- `id` is the Anthropic session id once known; before that, a pending_* id is used.
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_key TEXT NOT NULL,                                 -- 'schedule-agent' | 'email-digest' | ...
    title TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','done','error','cancelled')),
    started_at TEXT NOT NULL,                                -- ISO-8601
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions (started_at DESC);

-- Per-session event stream. Mirror of the in-memory TrackedSession.events
-- list so history survives orchestrator restarts.
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,                                    -- ordinal within the session
    ts TEXT NOT NULL,                                        -- ISO-8601
    event_json TEXT NOT NULL                                 -- full envelope
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events (session_id, seq);
"""

_SCHEMA_VERSION = "1"


def _ensure_columns(conn: sqlite3.Connection,
                    adds: Iterable[tuple[str, str, str]]) -> None:
    """Run `ALTER TABLE ADD COLUMN` for any column missing from the live DB."""
    for table, col, col_def in adds:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")


def _connect(readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we manage transactions explicitly when needed
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect() -> sqlite3.Connection:
    """Get a DB connection; bootstraps schema + seeds on first call."""
    first_boot = not DB_PATH.exists()
    conn = _connect()
    conn.executescript(SCHEMA)
    # --- Forward-compatible column additions for existing installs. ---
    # SQLite has no "ADD COLUMN IF NOT EXISTS"; we probe table_info and
    # add any missing column explicitly. Keep this block terse: new entries
    # are (table_name, column_name, column_def).
    _ensure_columns(conn, [
        ("tasks", "duration_locked", "INTEGER NOT NULL DEFAULT 0"),
    ])
    # Seed once
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                     (_SCHEMA_VERSION,))
        seed_defaults(conn)
    return conn


# ---------- Seed data ----------

def seed_defaults(conn: sqlite3.Connection) -> None:
    """Populate working hours + fixed blocks from schedule_config. Idempotent.

    Reads WORKING_HOURS, CLASS_BLOCKS, and TIMEZONE lazily so that schema-only
    callers (tests, `python config.py`) don't need provider credentials.
    If schedule_config isn't importable, seeds an empty config — the user can
    populate via the hub or by editing schedule_config later.
    """
    try:
        import schedule_config as _sc
        hours = _sc.WORKING_HOURS
        blocks = _sc.CLASS_BLOCKS
        tz = _sc.TIMEZONE
    except Exception:
        hours = {}
        blocks = []
        tz = TZ_NAME

    with conn:  # single transaction
        for dow, (start, end) in hours.items():
            conn.execute(
                "INSERT OR REPLACE INTO config_hours(dow, start_local, end_local) VALUES(?,?,?)",
                (dow, start, end),
            )
        # Seed class blocks only if the class table has none (don't clobber user edits)
        row = conn.execute("SELECT COUNT(*) AS n FROM config_blocks WHERE kind='class'").fetchone()
        if row["n"] == 0:
            for kind, label, rrule, s, e in blocks:
                conn.execute(
                    "INSERT INTO config_blocks(kind, label, rrule, start_local, end_local, tz) "
                    "VALUES(?,?,?,?,?,?)",
                    (kind, label, rrule, s, e, tz),
                )


# ---------- Readers ----------

def get_working_hours() -> dict[int, tuple[str, str]]:
    """{dow: (start_local, end_local)} where dow follows Python's weekday() (Mon=0)."""
    with connect() as conn:
        rows = conn.execute("SELECT dow, start_local, end_local FROM config_hours").fetchall()
    return {r["dow"]: (r["start_local"], r["end_local"]) for r in rows}


def get_blocks(kinds: Optional[Iterable[str]] = None) -> list[dict]:
    """All config_blocks rows as dicts; optional filter by kind."""
    with connect() as conn:
        if kinds:
            placeholders = ",".join("?" * len(list(kinds)))
            rows = conn.execute(
                f"SELECT * FROM config_blocks WHERE kind IN ({placeholders})",
                tuple(kinds),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM config_blocks").fetchall()
    return [dict(r) for r in rows]


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


if __name__ == "__main__":
    # `python config.py` bootstraps the DB and prints what's in it.
    conn = connect()
    print(f"DB: {DB_PATH}")
    print("Schema version:", get_meta("schema_version"))
    print()
    print("Working hours (dow: Mon=0..Sun=6):")
    for dow, (s, e) in sorted(get_working_hours().items()):
        print(f"  {dow}  {s}-{e}")
    print()
    print("Config blocks:")
    for b in get_blocks():
        print(f"  [{b['kind']}] {b['label']}  {b['start_local']}-{b['end_local']}  {b['rrule'] or ''}")
