"""Unified entry point.

On launch, decides whether to run the setup wizard or the main orchestrator
based on whether .env is populated. Designed to be the single entry point
that a packaged app / LaunchAgent / systemd unit invokes — so end users
never have to think about which script is the "first-run" one.

Decision:

    configured = .env exists AND has ENVIRONMENT_ID and AGENT_ID set
               → run the full orchestrator (port 8787, hub at /hub)

    else       → run the setup server (port 8787, wizard at /setup)

The setup server writes .env + creates the Anthropic agent. After the user
finishes the wizard, the app restarts (or the user closes and relaunches);
the next boot falls into the "configured" branch and serves the hub.
"""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

from paths import env_path

PROJECT_DIR = Path(__file__).resolve().parent


def _env_has(key: str) -> Optional[str]:
    """Read a value from .env without relying on python-dotenv having
    loaded it yet. Returns None if the file doesn't exist or the key is
    absent."""
    p = env_path()
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip("'").strip('"')
    return None


def _is_configured() -> bool:
    return bool(_env_has("ENVIRONMENT_ID") and _env_has("AGENT_ID"))


def _maybe_open_browser(url: str) -> None:
    """Open a browser on desktop launches. Honored by the packaged app; a
    server-side deployment can set SKIP_BROWSER=1."""
    if os.environ.get("SKIP_BROWSER"):
        return
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass


def main() -> int:
    # First-launch side effects (compile reminders_fetch if macOS + missing).
    # Idempotent; fast when everything is already in place.
    import bootstrap
    for step in bootstrap.run_all():
        if step.get("status") == "failed":
            print(f"[bootstrap] {step['step']}: {step['detail']}", file=sys.stderr)

    if _is_configured():
        # Hydrate os.environ from the resolved .env BEFORE importing
        # orchestrator — schedule_config runs at import time and reads
        # provider credentials off os.environ.
        from dotenv import load_dotenv
        load_dotenv(env_path())

        import orchestrator  # noqa: F401 (side-effects: creates app)
        from orchestrator import app
        import uvicorn
        token = _env_has("REPLAN_TOKEN") or ""
        if token:
            _maybe_open_browser(f"http://127.0.0.1:8787/hub?key={token}")
        uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")
        return 0
    else:
        import setup_server
        import uvicorn
        _maybe_open_browser("http://127.0.0.1:8787/setup")
        uvicorn.run(setup_server.app, host="127.0.0.1", port=8787,
                    log_level="info")
        return 0


if __name__ == "__main__":
    sys.exit(main())
