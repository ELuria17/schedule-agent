"""End-to-end test of the first-run flow, as a user would experience it.

Simulates what happens when someone downloads the app and launches it
for the first time — regardless of platform. The flow is identical on
Mac, Windows, and Linux; only the subprocess-detachment flags differ
internally. This script runs on macOS locally but exercises the same
code paths as a Windows install would.

Step by step, this script:

    1. Isolates itself from the user's real data by pointing
       SCHEDULE_AGENT_DATA_DIR at a temp directory.
    2. Skips the real Anthropic agent creation (no credits spent).
    3. Points the setup-server's orchestrator handoff at a minimal
       fake orchestrator that just answers /hub.
    4. Launches setup_server.py in a subprocess.
    5. Waits for it to bind :8787.
    6. POSTs a complete, valid form (as the wizard would on submit).
    7. Asserts the success HTML contains the handoff polling script.
    8. Waits for setup_server to exit (the BackgroundTask fires os._exit).
    9. Waits for the fake orchestrator to take over :8787.
   10. Polls /hub via HTTP to confirm it answers 200.
   11. Tears everything down.

Exit code 0 = every step passed. Exit code 1 = at least one step failed.
Prints a step-by-step log so failures are easy to diagnose.

Run directly:
    python scripts/e2e_setup_flow.py

Options:
    --verbose      print extra diagnostics (uvicorn stdout, etc.)
    --port PORT    override :8787 (default is the production port)

Intended to be invoked by the `schedule-agent-e2e` Claude Code agent
whenever the setup_server code or the handoff logic is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO / "venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)


# ---------- Result plumbing ----------

class StepFailed(Exception):
    """Raised when a step assertion fails; caller prints + exits nonzero."""


class Reporter:
    """Tiny stateful logger that numbers steps and prints clean output."""
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.step_num = 0
        self.failures: list[str] = []

    def step(self, title: str):
        self.step_num += 1
        print(f"\n[{self.step_num:02d}] {title}")

    def ok(self, msg: str):
        print(f"     OK  {msg}")

    def detail(self, msg: str):
        if self.verbose:
            print(f"         · {msg}")

    def fail(self, msg: str):
        print(f"     FAIL  {msg}")
        self.failures.append(f"step {self.step_num}: {msg}")
        raise StepFailed(msg)


# ---------- Helpers ----------

def wait_for_port_open(port: int, timeout: float = 15.0) -> bool:
    """Return True once `localhost:port` accepts a TCP connection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def wait_for_port_closed(port: int, timeout: float = 10.0) -> bool:
    """Return True once `localhost:port` refuses connections again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                time.sleep(0.15)
        except OSError:
            return True
    return False


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_post_form(url: str, fields: dict[str, str], timeout: float = 15.0) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def kill_quietly(proc: Optional[subprocess.Popen]):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def free_port(port: int):
    """Best-effort: kill anything listening on `port` so the test can use it."""
    if sys.platform == "win32":
        return  # netstat/taskkill dance is brittle; caller should handle.
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
        for pid in out.splitlines():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.3)
    except subprocess.CalledProcessError:
        pass  # nothing was listening — good


# ---------- The test ----------

def run(verbose: bool = False, port: int = 8787) -> int:
    r = Reporter(verbose=verbose)
    setup_proc: Optional[subprocess.Popen] = None
    tmpdir: Optional[tempfile.TemporaryDirectory] = None

    try:
        # -------- 01 --------
        r.step("Isolate test state (temp data dir + free port)")
        tmpdir = tempfile.TemporaryDirectory(prefix="schedule_agent_e2e_")
        tmp_path = Path(tmpdir.name)
        r.detail(f"temp data dir: {tmp_path}")
        free_port(port)
        if not wait_for_port_closed(port, timeout=3):
            r.fail(f"port {port} is busy after cleanup; can't test")
        r.ok(f"port {port} is free; temp dir ready")

        # -------- 02 --------
        r.step("Spawn setup_server in a subprocess (as run.py would on first launch)")
        fake_orch_cmd = f"{PYTHON} {REPO / 'tests' / 'fake_orchestrator.py'}"
        env = os.environ.copy()
        env.update({
            "SCHEDULE_AGENT_DATA_DIR": str(tmp_path),
            "SCHEDULE_AGENT_SKIP_ANTHROPIC": "1",
            "SCHEDULE_AGENT_ORCHESTRATOR_CMD": fake_orch_cmd,
            # Strip user's Anthropic key so nothing accidentally hits the API
            "ANTHROPIC_API_KEY": "",
        })
        env.pop("PYTEST_CURRENT_TEST", None)  # don't let the handoff guard trigger
        setup_proc = subprocess.Popen(
            [PYTHON, str(REPO / "setup_server.py")],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE if verbose else subprocess.DEVNULL,
            stderr=subprocess.PIPE if verbose else subprocess.DEVNULL,
        )
        r.detail(f"setup_server pid: {setup_proc.pid}")
        if not wait_for_port_open(port, timeout=15):
            r.fail("setup_server never bound port — probably a Python import error")
        r.ok("setup_server is listening")

        # -------- 03 --------
        r.step("Fetch the wizard and verify every provider option renders")
        status, html = http_get(f"http://127.0.0.1:{port}/setup")
        if status != 200:
            r.fail(f"GET /setup returned {status}")
        must_contain = [
            "Let's get you set up.",
            "How new tasks arrive",
            "Apple Calendar (iCloud)",
            "Google Calendar",
            "Outlook / Microsoft 365",
            "Canvas LMS",
            "Todoist",
            "Gmail inbox scanning",
            "Google Tasks",
            "Outlook Mail inbox scanning",
            "Microsoft To Do",
            "iMessage",
            "Pushover",
            "Slack",
            "Email (SMTP)",
            "ntfy.sh",
        ]
        missing = [p for p in must_contain if p not in html]
        if missing:
            r.fail(f"wizard missing these providers: {missing}")
        r.ok(f"all {len(must_contain)} expected providers visible")

        # -------- 04 --------
        r.step("POST a complete, valid form (the wizard's Submit action)")
        form = {
            "anthropic_api_key": "sk-ant-e2e-test",
            "timezone": "America/New_York",
            "replan_token": "e2e-replan-token",
            "approval_mode": "auto",
            "calendar": "icloud",
            "icloud_user": "test@example.com",
            "icloud_app_password": "xxxx-xxxx-xxxx-xxxx",
            "write_calendar_name": "Study Blocks",
            "notifier": "ntfy",
            "ntfy_topic": "e2e-test-topic",
        }
        status, success_html = http_post_form(
            f"http://127.0.0.1:{port}/setup", form, timeout=30,
        )
        if status != 200:
            r.fail(f"POST /setup returned {status}; body: {success_html[:400]}")
        if "Starting your hub" not in success_html:
            r.fail("success page missing 'Starting your hub' — handoff JS not rendered")
        if "hub?key=" not in success_html:
            r.fail("success page missing the hub URL")
        r.ok("POST succeeded; success page has handoff polling script")

        # -------- 05 --------
        r.step(".env was written with dummy ENVIRONMENT_ID / AGENT_ID")
        env_file = tmp_path / ".env"
        if not env_file.exists():
            r.fail(f"no .env file at {env_file}")
        env_text = env_file.read_text()
        for key in ("ENVIRONMENT_ID=", "AGENT_ID=",
                    "ICLOUD_USER=test@example.com",
                    "NTFY_TOPIC=e2e-test-topic"):
            if key not in env_text:
                r.fail(f"env missing {key!r}")
        r.ok(".env wired correctly")

        # -------- 06 --------
        r.step("setup_server exits on its own (BackgroundTask handoff)")
        try:
            setup_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            r.fail("setup_server did not exit after POST — os._exit(0) never fired")
        r.ok(f"setup_server exited (exit code {setup_proc.returncode})")

        # -------- 07 --------
        r.step("port briefly goes quiet, then the fake orchestrator takes over")
        # The port can race — fake orchestrator is sleeping 2s before binding.
        # We don't strictly need port-closed; we just need port-open with the
        # fake responding within the wrapper's wake-up + bind time.
        if not wait_for_port_open(port, timeout=15):
            r.fail("fake orchestrator never bound port :8787 — handoff spawn failed")
        r.ok(f"port :{port} is back up (something is listening)")

        # -------- 08 --------
        r.step("Confirm it's the fake orchestrator answering, not a zombie setup_server")
        status, marker_body = http_get(f"http://127.0.0.1:{port}/_fake_marker")
        if status != 200:
            r.fail(f"/_fake_marker returned {status} — wrong process on :{port}")
        try:
            marker = json.loads(marker_body)
        except json.JSONDecodeError:
            r.fail(f"/_fake_marker returned non-JSON: {marker_body[:200]}")
        if not marker.get("fake"):
            r.fail(f"/_fake_marker returned unexpected body: {marker}")
        r.ok(f"fake orchestrator is live (pid {marker.get('pid')})")

        # -------- 09 --------
        r.step("Final verification: /hub responds 200 (what the user's browser hits)")
        status, _ = http_get(f"http://127.0.0.1:{port}/hub?key=e2e-replan-token")
        if status != 200:
            r.fail(f"/hub returned {status} — orchestrator present but wrong routes")
        r.ok("/hub is live — the user would be redirected here")

        # -------- all passed --------
        print("\n" + "=" * 62)
        print("  E2E setup-flow test: ALL STEPS PASSED")
        print("=" * 62)
        return 0

    except StepFailed:
        print("\n" + "=" * 62)
        print("  E2E setup-flow test: FAILED")
        for f in r.failures:
            print(f"  · {f}")
        print("=" * 62)
        return 1

    finally:
        kill_quietly(setup_proc)
        free_port(port)  # clean up any fake orchestrator the handoff spawned
        if tmpdir is not None:
            try:
                tmpdir.cleanup()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show extra diagnostics")
    parser.add_argument("--port", type=int, default=8787,
                        help="port to use (default: 8787)")
    args = parser.parse_args()
    return run(verbose=args.verbose, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
