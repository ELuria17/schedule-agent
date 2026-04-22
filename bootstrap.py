"""First-launch setup side effects.

Currently just one thing: compile `macos/reminders_fetch` from the Swift
source if the binary isn't there. Called from `run.py` on every launch
(fast fallthrough when the binary already exists) and from
`setup_server.py` on the POST that ends the wizard (so the success page
only renders after the binary is ready to use).

Keeping these in a dedicated module so it's easy to add more bootstrap
steps later — first-launch schema migrations, checksumming a bundled
asset, etc. — without tangling them into run.py.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from paths import reminders_fetch_binary_path, reminders_fetch_source_path


BootstrapStatus = Literal["ok", "skipped", "failed"]


def ensure_reminders_fetch_compiled() -> dict:
    """Compile `reminders_fetch` from its Swift source if missing.

    Returns a status dict so callers can surface failures to the user.
    Shape: `{"status": "ok"|"skipped"|"failed", "detail": "..."}`.

    Outcomes:
    - `ok`         binary exists (either already compiled, or we just built it)
    - `skipped`    not applicable (non-macOS, source missing from a trimmed
                   bundle, AppleRemindersTodoSource not in use)
    - `failed`     swiftc not available, or the compile returned non-zero

    Never raises — a failed compile shouldn't take down a launch. If the
    user's schedule_config.py doesn't wire AppleRemindersTodoSource they
    never touch this code path anyway.
    """
    if sys.platform != "darwin":
        return {"status": "skipped", "detail": "not macOS"}

    out = reminders_fetch_binary_path()
    if out.exists():
        return {"status": "ok", "detail": f"already compiled ({out})"}

    src = reminders_fetch_source_path()
    if not src.exists():
        return {"status": "skipped",
                "detail": f"Swift source missing at {src}"}

    if shutil.which("swiftc") is None:
        return {"status": "failed",
                "detail": "swiftc not found. Install Xcode Command Line "
                          "Tools: xcode-select --install"}

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["swiftc", "-O", "-o", str(out), str(src)],
            check=False, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": "swiftc timed out after 120s"}
    except Exception as ex:
        return {"status": "failed",
                "detail": f"{type(ex).__name__}: {ex}"}

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()[:300]
        return {"status": "failed",
                "detail": f"swiftc exit {r.returncode}: {stderr}"}

    return {"status": "ok", "detail": f"compiled → {out}"}


def run_all() -> list[dict]:
    """Run every bootstrap step and return their individual results."""
    return [
        {"step": "reminders_fetch", **ensure_reminders_fetch_compiled()},
    ]


if __name__ == "__main__":
    import json
    print(json.dumps(run_all(), indent=2))
