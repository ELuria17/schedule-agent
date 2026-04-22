"""Test fixtures.

Each test gets a fresh SQLite DB in a temp directory. We deliberately avoid
importing `schedule_config` here — schedule_config instantiates the user's
configured providers at module load and needs their credentials. The DB
fixture seeds only what the test needs, using config.py's schema + direct
inserts into config_hours / config_blocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable so `import solver`, `import config`, etc. work
# when pytest is invoked from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Bootstrap a fresh SQLite DB at a temp path. Yields the config module
    with `DB_PATH` pointing at the temp DB. Working hours default to 9-22
    every day; no class blocks seeded — tests add blocks as needed."""
    import config

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)

    # Fresh connection: force the meta-row path so seed_defaults doesn't
    # try to import schedule_config (which would need provider creds).
    # We supply our own minimal seed directly below.
    import sqlite3
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(config.SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
        (config._SCHEMA_VERSION,),
    )
    # Default working hours: 09:00-22:00 every day (Mon=0..Sun=6)
    for dow in range(7):
        conn.execute(
            "INSERT INTO config_hours(dow, start_local, end_local) VALUES(?,?,?)",
            (dow, "09:00", "22:00"),
        )
    conn.close()
    return config


@pytest.fixture
def add_block(db):
    """Helper to add a config_blocks row. Returns a function."""
    import sqlite3

    def _add(kind: str, label: str, rrule: str, start_local: str, end_local: str,
             tz: str = "America/New_York"):
        conn = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        conn.execute(
            "INSERT INTO config_blocks(kind, label, rrule, start_local, end_local, tz) "
            "VALUES(?,?,?,?,?,?)",
            (kind, label, rrule, start_local, end_local, tz),
        )
        conn.close()

    return _add


@pytest.fixture
def add_task(db):
    """Helper to insert a task row and return its id."""
    import sqlite3

    def _add(title: str, duration_min: int, *, deadline_ts=None, priority="medium",
             min_chunk_min=30, max_chunk_min=120, status="scheduled", course=None,
             source="manual", source_id=None):
        conn = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        cur = conn.execute(
            "INSERT INTO tasks(title, duration_min, deadline_ts, priority, "
            "min_chunk_min, max_chunk_min, status, course, source, source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (title, duration_min, deadline_ts, priority, min_chunk_min,
             max_chunk_min, status, course, source, source_id),
        )
        task_id = cur.lastrowid
        conn.close()
        return task_id

    return _add
