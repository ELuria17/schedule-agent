"""Data-directory resolution.

A single place that decides where mutable user state lives — `.env`,
`state.db`, `learning_state.json`, `last_morning_date.txt`. Every module
that reads or writes those files should route through here instead of
hardcoding a path next to `__file__`.

Resolution order (highest priority first):

1. `$SCHEDULE_AGENT_DATA_DIR` env var. Wins everywhere. Used by Docker
   (mount a host volume at `/app/data` and point this at it) and by
   anyone who wants to run multiple instances on one host.
2. PyInstaller-frozen bundle (`sys.frozen` is truthy) → OS default:
     macOS:   `~/Library/Application Support/schedule-agent`
     Linux:   `$XDG_DATA_HOME/schedule-agent` or `~/.local/share/schedule-agent`
     Windows: `%APPDATA%\\schedule-agent`
   This is required for signed/notarized `.app` bundles — the bundle
   itself is read-only on install, so state can't live inside it.
3. The repo directory — the dev flow. `git clone && python run.py`
   writes everything next to the source, the way it always has.

The data directory is created on first access (`mkdir parents=True,
exist_ok=True`). Callers can trust it exists by the time a path helper
returns.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_DIR = Path(__file__).resolve().parent
_ENV_OVERRIDE_NAME = "SCHEDULE_AGENT_DATA_DIR"


def data_dir() -> Path:
    """Return (and create) the directory that holds mutable user state."""
    override = os.environ.get(_ENV_OVERRIDE_NAME)
    if override:
        d = Path(override).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        d = _platform_default()
    else:
        d = _REPO_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _platform_default() -> Path:
    """OS-appropriate directory when running as a packaged (frozen) bundle."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "schedule-agent"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home()
        return base / "schedule-agent"
    # Linux + other Unix-like.
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "schedule-agent"


# ---- Named paths for the files we manage ----

def env_path() -> Path:
    return data_dir() / ".env"


def state_db_path() -> Path:
    return data_dir() / "state.db"


def learning_state_path() -> Path:
    return data_dir() / "learning_state.json"


def morning_marker_path() -> Path:
    return data_dir() / "last_morning_date.txt"


def reminders_fetch_source_path() -> Path:
    """Path to the Swift source shipped with the repo / bundle. Always
    read-only: next to __file__, i.e. inside the PyInstaller bundle when
    frozen, inside the repo in dev."""
    return _REPO_DIR / "macos" / "reminders_fetch.swift"


def reminders_fetch_binary_path() -> Path:
    """Where the compiled `reminders_fetch` binary lives.

    In dev, we keep the long-standing behavior (next to the .swift source
    inside `macos/`). In a frozen bundle the bundle is read-only, so the
    binary has to live in the writable data dir.
    """
    if getattr(sys, "frozen", False):
        return data_dir() / "reminders_fetch"
    return _REPO_DIR / "macos" / "reminders_fetch"


if __name__ == "__main__":
    print(f"data_dir:              {data_dir()}")
    print(f"env_path:              {env_path()}")
    print(f"state_db_path:         {state_db_path()}")
    print(f"learning_state_path:   {learning_state_path()}")
    print(f"morning_marker_path:   {morning_marker_path()}")
    print(f"frozen:                {getattr(sys, 'frozen', False)}")
    print(f"{_ENV_OVERRIDE_NAME}:  {os.environ.get(_ENV_OVERRIDE_NAME) or '(unset)'}")
