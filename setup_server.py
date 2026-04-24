"""First-run setup server.

Serves the GUI setup wizard at `http://127.0.0.1:8787/`. Covers every
provider category the app supports:

Calendar
    iCloud CalDAV · Google Calendar (in-app OAuth wizard) · Generic CalDAV
    (Fastmail, Posteo, Mailbox.org, Nextcloud, self-hosted Radicale) · Skip

Task sources (any combination)
    Canvas LMS · Todoist

Notifier (pick one)
    iMessage (macOS) · Pushover · Slack · Email (SMTP) · ntfy.sh

The wizard writes `.env` and calls Anthropic to create the agent. It never
imports `schedule_config.py` — which would crash at import time when env
vars are still missing.

For Google Calendar, a dedicated three-step sub-wizard at `/setup/google`
walks the user through the Google Cloud Console steps, accepts their
downloaded `credentials.json` file via upload, and runs the OAuth loopback
flow (on a secondary port) in a background thread so the browser stays
responsive.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import install as install_mod
from paths import data_dir, google_token_path, microsoft_token_path

app = FastAPI()
PROJECT_DIR = Path(__file__).resolve().parent


# ---------- Automatic handoff to orchestrator ----------
# After a successful setup, the user expects `/hub?key=...` to work
# immediately. The orchestrator (which owns `/hub`) needs to replace
# this setup_server on port 8787. Manually-relaunching the app is a
# friction point — especially for non-technical users on Windows where
# "relaunch" means finding and re-running a Terminal command.
#
# We solve it by spawning a detached child process that waits ~2s (for
# this server to release the port), then execs the orchestrator. Then
# we kill ourselves. The success page JS polls the orchestrator and
# redirects to the hub once it's up.

def _spawn_orchestrator_detached() -> None:
    """Spawn an orchestrator process that survives our exit."""
    if getattr(sys, "frozen", False):
        # In a PyInstaller bundle, sys.executable is the app binary
        # itself; re-launching routes through run.py which detects the
        # now-populated .env and hands off to orchestrator automatically.
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, str(PROJECT_DIR / "orchestrator.py")]

    # Wrap in a tiny python helper that sleeps first, then execs the
    # target. The sleep gives this setup_server time to die and release
    # :8787 before orchestrator tries to bind it.
    wrapper = (
        "import time, os; "
        "time.sleep(2); "
        f"os.execvp({cmd[0]!r}, {cmd!r})"
    )
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, "-c", wrapper], **popen_kwargs)


def _handoff_to_orchestrator() -> None:
    """Detach the orchestrator, give the response time to flush, exit."""
    import time
    # Safety: pytest sets PYTEST_CURRENT_TEST for every test run. If it's
    # set, we're inside a test — don't spawn children and don't kill the
    # process, or we'll take the test runner down with us.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        _spawn_orchestrator_detached()
    except Exception:
        # If spawning fails, don't kill ourselves — the user can still
        # manually relaunch. The success-page JS will fall back to a
        # "please relaunch" message when its polling times out.
        traceback.print_exc()
        return
    time.sleep(0.5)   # uvicorn flushes the response in the meantime
    os._exit(0)       # hard exit — uvicorn won't release the port on SIGINT cleanly here


# ---------- Shared helpers ----------

def _env_snapshot() -> dict[str, str]:
    return install_mod._read_env()


def _is_configured(env: dict[str, str]) -> bool:
    return bool(env.get("ENVIRONMENT_ID") and env.get("AGENT_ID"))


def _google_connection_status(env: dict[str, str]) -> str:
    """'connected' if both credentials + token exist; 'credentials_only' if
    credentials are saved but OAuth hasn't been completed; 'none' otherwise."""
    creds = env.get("GOOGLE_CALENDAR_CREDENTIALS", "")
    if creds and Path(creds).exists():
        if google_token_path().exists():
            return "connected"
        return "credentials_only"
    return "none"


def _microsoft_connection_status(env: dict[str, str]) -> str:
    """'connected' if client_id + token file exist; 'client_id_only' if
    MICROSOFT_CLIENT_ID is saved but OAuth hasn't been completed yet."""
    if env.get("MICROSOFT_CLIENT_ID"):
        if microsoft_token_path().exists():
            return "connected"
        return "client_id_only"
    return "none"


# ---------- Main setup form ----------

@app.get("/", response_class=HTMLResponse)
@app.get("/setup", response_class=HTMLResponse)
async def setup_form():
    env = _env_snapshot()
    defaults = {
        "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", ""),
        "REPLAN_TOKEN": env.get("REPLAN_TOKEN") or secrets.token_urlsafe(32),
        "TIMEZONE": env.get("TIMEZONE") or "America/New_York",
        "DEFAULT_REQUIRE_APPROVAL": env.get("DEFAULT_REQUIRE_APPROVAL", "false"),
        "ICLOUD_USER": env.get("ICLOUD_USER", ""),
        "ICLOUD_APP_PASSWORD": env.get("ICLOUD_APP_PASSWORD", ""),
        "WRITE_CALENDAR_NAME": env.get("WRITE_CALENDAR_NAME") or "Study Blocks",
        "CALDAV_URL": env.get("CALDAV_URL", ""),
        "CALDAV_USER": env.get("CALDAV_USER", ""),
        "CALDAV_PASSWORD": env.get("CALDAV_PASSWORD", ""),
        "MICROSOFT_CLIENT_ID": env.get("MICROSOFT_CLIENT_ID", ""),
        "MICROSOFT_TENANT": env.get("MICROSOFT_TENANT") or "common",
        "CANVAS_BASE_URL": env.get("CANVAS_BASE_URL") or "https://psu.instructure.com/api/v1",
        "CANVAS_TOKEN": env.get("CANVAS_TOKEN", ""),
        "TODOIST_TOKEN": env.get("TODOIST_TOKEN", ""),
        "GMAIL_MAX_MESSAGES": env.get("GMAIL_MAX_MESSAGES") or "25",
        "GMAIL_LABEL_FILTER": env.get("GMAIL_LABEL_FILTER", ""),
        "OUTLOOK_MAIL_FOLDER": env.get("OUTLOOK_MAIL_FOLDER") or "inbox",
        "OUTLOOK_CATEGORY_FILTER": env.get("OUTLOOK_CATEGORY_FILTER", ""),
        "OUTLOOK_MAX_MESSAGES": env.get("OUTLOOK_MAX_MESSAGES") or "25",
        "USER_PHONE": env.get("USER_PHONE", ""),
        "PUSHOVER_USER_KEY": env.get("PUSHOVER_USER_KEY", ""),
        "PUSHOVER_APP_TOKEN": env.get("PUSHOVER_APP_TOKEN", ""),
        "SLACK_WEBHOOK_URL": env.get("SLACK_WEBHOOK_URL", ""),
        "SMTP_HOST": env.get("SMTP_HOST", ""),
        "SMTP_PORT": env.get("SMTP_PORT") or "587",
        "SMTP_USERNAME": env.get("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": env.get("SMTP_PASSWORD", ""),
        "SMTP_TO": env.get("SMTP_TO", ""),
        "NTFY_TOPIC": env.get("NTFY_TOPIC") or f"schedule-agent-{secrets.token_urlsafe(16)}",
    }

    # Calendar default: whatever's already wired.
    if _google_connection_status(env) in ("connected", "credentials_only"):
        cal_default = "google"
    elif (_microsoft_connection_status(env) != "none"
          and env.get("ENABLE_OUTLOOK_CALENDAR", "false").lower() in ("1", "true", "yes")):
        cal_default = "outlook"
    elif env.get("CALDAV_URL"):
        cal_default = "caldav"
    else:
        cal_default = "icloud"

    # Notifier default: first one with credentials set, else iMessage on mac / ntfy elsewhere.
    if env.get("USER_PHONE"):
        notifier_default = "imessage"
    elif env.get("PUSHOVER_USER_KEY"):
        notifier_default = "pushover"
    elif env.get("SLACK_WEBHOOK_URL"):
        notifier_default = "slack"
    elif env.get("SMTP_HOST"):
        notifier_default = "email"
    elif env.get("NTFY_TOPIC"):
        notifier_default = "ntfy"
    else:
        notifier_default = "imessage" if sys.platform == "darwin" else "ntfy"

    task_canvas = bool(env.get("CANVAS_TOKEN"))
    task_todoist = bool(env.get("TODOIST_TOKEN"))
    task_gmail = env.get("ENABLE_GMAIL_SCAN", "false").lower() in ("1", "true", "yes")
    task_gtasks = env.get("ENABLE_GOOGLE_TASKS", "false").lower() in ("1", "true", "yes")
    task_outlook_mail = env.get("ENABLE_OUTLOOK_MAIL_SCAN", "false").lower() in ("1", "true", "yes")
    task_mstodo = env.get("ENABLE_MICROSOFT_TODO", "false").lower() in ("1", "true", "yes")
    google_status = _google_connection_status(env)
    microsoft_status = _microsoft_connection_status(env)

    return HTMLResponse(_render_form(
        defaults,
        cal_default=cal_default,
        notifier_default=notifier_default,
        task_canvas=task_canvas,
        task_todoist=task_todoist,
        task_gmail=task_gmail,
        task_gtasks=task_gtasks,
        task_outlook_mail=task_outlook_mail,
        task_mstodo=task_mstodo,
        google_status=google_status,
        microsoft_status=microsoft_status,
        is_macos=(sys.platform == "darwin"),
    ))


@app.post("/setup")
async def setup_submit(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    data = {k: str(v).strip() for k, v in form.items()}

    env = _env_snapshot()

    # Core.
    env["ANTHROPIC_API_KEY"] = data.get("anthropic_api_key", "")
    env["REPLAN_TOKEN"] = data.get("replan_token", "") or secrets.token_urlsafe(32)
    env["TIMEZONE"] = data.get("timezone", "") or "America/New_York"
    env["DEFAULT_REQUIRE_APPROVAL"] = (
        "true" if data.get("approval_mode") == "review" else "false"
    )

    # Calendar — exactly one backend wins.
    calendar_choice = data.get("calendar", "icloud")
    # Clear all calendar-specific env vars; we'll set only the chosen backend's.
    # MICROSOFT_CLIENT_ID and MICROSOFT_TENANT are NOT cleared — they can be
    # reused by Outlook-mail / MS-To-Do task sources even if the user picks a
    # different calendar.
    for k in ("ICLOUD_USER", "ICLOUD_APP_PASSWORD", "GOOGLE_CALENDAR_CREDENTIALS",
              "ENABLE_OUTLOOK_CALENDAR",
              "CALDAV_URL", "CALDAV_USER", "CALDAV_PASSWORD"):
        env.pop(k, None)

    if calendar_choice == "icloud":
        env["ICLOUD_USER"] = data.get("icloud_user", "")
        env["ICLOUD_APP_PASSWORD"] = data.get("icloud_app_password", "")
        env["WRITE_CALENDAR_NAME"] = data.get("write_calendar_name", "") or "Study Blocks"
    elif calendar_choice == "google":
        # The wizard already wrote GOOGLE_CALENDAR_CREDENTIALS on upload —
        # preserve whatever's there.
        prior = _env_snapshot().get("GOOGLE_CALENDAR_CREDENTIALS", "")
        if prior:
            env["GOOGLE_CALENDAR_CREDENTIALS"] = prior
        env["WRITE_CALENDAR_NAME"] = data.get("write_calendar_name", "") or "Study Blocks"
    elif calendar_choice == "outlook":
        # Microsoft client_id is collected in its own sub-wizard; preserve
        # whatever's there, and flip the enable flag so schedule_config picks
        # the Outlook provider over CalDAV.
        prior_cid = data.get("microsoft_client_id", "") or _env_snapshot().get("MICROSOFT_CLIENT_ID", "")
        prior_tenant = data.get("microsoft_tenant", "") or _env_snapshot().get("MICROSOFT_TENANT", "common")
        env["MICROSOFT_CLIENT_ID"] = prior_cid
        env["MICROSOFT_TENANT"] = prior_tenant or "common"
        env["ENABLE_OUTLOOK_CALENDAR"] = "true"
        env["WRITE_CALENDAR_NAME"] = data.get("write_calendar_name", "") or "Study Blocks"
    elif calendar_choice == "caldav":
        env["CALDAV_URL"] = data.get("caldav_url", "")
        env["CALDAV_USER"] = data.get("caldav_user", "")
        env["CALDAV_PASSWORD"] = data.get("caldav_password", "")
        env["WRITE_CALENDAR_NAME"] = data.get("write_calendar_name", "") or "Study Blocks"

    # Task sources — checkboxes, any combination.
    for k in ("CANVAS_TOKEN", "CANVAS_BASE_URL", "TODOIST_TOKEN",
              "ENABLE_GMAIL_SCAN", "GMAIL_LABEL_FILTER", "GMAIL_MAX_MESSAGES",
              "ENABLE_GOOGLE_TASKS",
              "ENABLE_OUTLOOK_MAIL_SCAN", "OUTLOOK_MAIL_FOLDER",
              "OUTLOOK_CATEGORY_FILTER", "OUTLOOK_MAX_MESSAGES",
              "ENABLE_MICROSOFT_TODO"):
        env.pop(k, None)

    if data.get("src_canvas"):
        env["CANVAS_BASE_URL"] = data.get("canvas_base_url", "") or \
            "https://psu.instructure.com/api/v1"
        env["CANVAS_TOKEN"] = data.get("canvas_token", "")
    if data.get("src_todoist"):
        env["TODOIST_TOKEN"] = data.get("todoist_token", "")
    if data.get("src_gmail"):
        env["ENABLE_GMAIL_SCAN"] = "true"
        if data.get("gmail_label_filter"):
            env["GMAIL_LABEL_FILTER"] = data["gmail_label_filter"]
        if data.get("gmail_max_messages"):
            env["GMAIL_MAX_MESSAGES"] = data["gmail_max_messages"]
    if data.get("src_gtasks"):
        env["ENABLE_GOOGLE_TASKS"] = "true"
    if data.get("src_outlook_mail"):
        env["ENABLE_OUTLOOK_MAIL_SCAN"] = "true"
        if data.get("outlook_mail_folder"):
            env["OUTLOOK_MAIL_FOLDER"] = data["outlook_mail_folder"]
        if data.get("outlook_category_filter"):
            env["OUTLOOK_CATEGORY_FILTER"] = data["outlook_category_filter"]
        if data.get("outlook_max_messages"):
            env["OUTLOOK_MAX_MESSAGES"] = data["outlook_max_messages"]
    if data.get("src_mstodo"):
        env["ENABLE_MICROSOFT_TODO"] = "true"
    # Any task source that rides on Microsoft Graph requires MICROSOFT_CLIENT_ID.
    if (data.get("src_outlook_mail") or data.get("src_mstodo")):
        cid = data.get("microsoft_client_id", "") or _env_snapshot().get("MICROSOFT_CLIENT_ID", "")
        tenant = data.get("microsoft_tenant", "") or _env_snapshot().get("MICROSOFT_TENANT", "common")
        if cid:
            env["MICROSOFT_CLIENT_ID"] = cid
            env["MICROSOFT_TENANT"] = tenant or "common"

    # Notifier — pick one; wipe others so the cascade in schedule_config
    # picks up the intended backend.
    notifier = data.get("notifier", "ntfy")
    for k in ("USER_PHONE", "PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN",
              "SLACK_WEBHOOK_URL", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
              "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "NTFY_TOPIC"):
        env.pop(k, None)
    if notifier == "imessage":
        env["USER_PHONE"] = data.get("user_phone", "")
    elif notifier == "pushover":
        env["PUSHOVER_USER_KEY"] = data.get("pushover_user_key", "")
        env["PUSHOVER_APP_TOKEN"] = data.get("pushover_app_token", "")
    elif notifier == "slack":
        env["SLACK_WEBHOOK_URL"] = data.get("slack_webhook_url", "")
    elif notifier == "email":
        env["SMTP_HOST"] = data.get("smtp_host", "")
        env["SMTP_PORT"] = data.get("smtp_port", "") or "587"
        env["SMTP_USERNAME"] = data.get("smtp_username", "")
        env["SMTP_PASSWORD"] = data.get("smtp_password", "")
        env["SMTP_TO"] = data.get("smtp_to", "") or data.get("smtp_username", "")
    else:
        env["NTFY_TOPIC"] = data.get("ntfy_topic", "") or \
            f"schedule-agent-{secrets.token_urlsafe(16)}"

    # Validate required fields.
    missing = []
    if not env.get("ANTHROPIC_API_KEY"):
        missing.append("Anthropic API key")
    if calendar_choice == "icloud" and not (env.get("ICLOUD_USER") and env.get("ICLOUD_APP_PASSWORD")):
        missing.append("iCloud email + app-specific password")
    if calendar_choice == "google" and not env.get("GOOGLE_CALENDAR_CREDENTIALS"):
        missing.append("Google Calendar — click 'Connect Google Calendar' to finish OAuth")
    if calendar_choice == "outlook" and not env.get("MICROSOFT_CLIENT_ID"):
        missing.append("Outlook — click 'Connect Microsoft 365' to finish setup")
    if calendar_choice == "caldav" and not (env.get("CALDAV_URL") and env.get("CALDAV_USER")
                                            and env.get("CALDAV_PASSWORD")):
        missing.append("CalDAV server URL, username, and password")
    if data.get("src_canvas") and not env.get("CANVAS_TOKEN"):
        missing.append("Canvas access token")
    if data.get("src_todoist") and not env.get("TODOIST_TOKEN"):
        missing.append("Todoist API token")
    if (data.get("src_gmail") or data.get("src_gtasks")) and not env.get("GOOGLE_CALENDAR_CREDENTIALS"):
        missing.append("Gmail / Google Tasks needs the Google Calendar connection — pick Google above or connect via that sub-wizard")
    if (data.get("src_outlook_mail") or data.get("src_mstodo")) and not env.get("MICROSOFT_CLIENT_ID"):
        missing.append("Outlook Mail / Microsoft To Do needs a Microsoft 365 client ID — click 'Connect Microsoft 365'")
    if notifier == "imessage" and not env.get("USER_PHONE"):
        missing.append("phone number")
    if notifier == "pushover" and not (env.get("PUSHOVER_USER_KEY") and env.get("PUSHOVER_APP_TOKEN")):
        missing.append("Pushover user key and application token")
    if notifier == "slack" and not env.get("SLACK_WEBHOOK_URL"):
        missing.append("Slack incoming-webhook URL")
    if notifier == "email" and not (env.get("SMTP_HOST") and env.get("SMTP_USERNAME")
                                    and env.get("SMTP_PASSWORD")):
        missing.append("SMTP host, username, and password")

    if missing:
        return HTMLResponse(_render_error(f"Missing: {', '.join(missing)}."), status_code=400)

    install_mod._write_env(env)

    for k, v in env.items():
        if v:
            os.environ[k] = v
    try:
        import importlib
        import setup as setup_mod
        importlib.reload(setup_mod)
        setup_mod.main()
    except Exception as ex:
        return HTMLResponse(
            _render_error(f"Saved .env, but Anthropic setup failed: "
                          f"{type(ex).__name__}: {ex}. "
                          f"Check your API key and retry."),
            status_code=500,
        )

    import bootstrap
    bootstrap_results = bootstrap.run_all()

    final_env = _env_snapshot()
    token = final_env.get("REPLAN_TOKEN", "")
    # Schedule the handoff to run AFTER this response is flushed to the
    # browser. Spawns a detached orchestrator and exits this process so
    # the port is freed for the new server.
    background_tasks.add_task(_handoff_to_orchestrator)
    return HTMLResponse(_render_success(token, bootstrap_results))


# ---------- Google Calendar sub-wizard ----------
# State for the (single-user) OAuth run. The wizard uses a background thread
# because `run_local_server` blocks waiting for the user to sign in.

_google_auth_state: dict = {"status": "idle", "detail": ""}
_google_auth_lock = threading.Lock()


@app.get("/setup/google", response_class=HTMLResponse)
async def google_wizard():
    env = _env_snapshot()
    status = _google_connection_status(env)
    return HTMLResponse(_render_google_wizard(
        status=status,
        write_calendar_name=env.get("WRITE_CALENDAR_NAME") or "Study Blocks",
    ))


@app.post("/setup/google/upload")
async def google_upload(credentials: UploadFile = File(...)):
    """Save the uploaded credentials.json into data_dir and record the path
    in .env. The file is validated as JSON but not parsed further."""
    dest = data_dir() / "credentials.json"
    try:
        with dest.open("wb") as fh:
            shutil.copyfileobj(credentials.file, fh)
    finally:
        credentials.file.close()

    # Minimal sanity check: parseable JSON, and has either "installed" or
    # "web" key at top level (Google's two desktop/web variants).
    try:
        import json
        obj = json.loads(dest.read_text())
        if not ("installed" in obj or "web" in obj):
            dest.unlink(missing_ok=True)
            return JSONResponse(
                {"ok": False, "error": "That file doesn't look like a "
                 "Google OAuth credentials.json. Make sure you downloaded "
                 "the JSON from the OAuth 2.0 Client IDs page (not the "
                 "API key page)."},
                status_code=400,
            )
    except Exception:
        dest.unlink(missing_ok=True)
        return JSONResponse(
            {"ok": False, "error": "Uploaded file is not valid JSON."},
            status_code=400,
        )

    env = _env_snapshot()
    env["GOOGLE_CALENDAR_CREDENTIALS"] = str(dest)
    install_mod._write_env(env)
    return JSONResponse({"ok": True, "path": str(dest)})


@app.post("/setup/google/authorize")
async def google_authorize():
    """Kick off the OAuth loopback flow in a background thread. Returns
    immediately; the client polls /setup/google/status until it flips."""
    env = _env_snapshot()
    creds_path = env.get("GOOGLE_CALENDAR_CREDENTIALS", "")
    if not creds_path or not Path(creds_path).exists():
        return JSONResponse(
            {"ok": False, "error": "Upload credentials.json first (Step 7)."},
            status_code=400,
        )

    with _google_auth_lock:
        if _google_auth_state["status"] == "pending":
            return JSONResponse(
                {"ok": False, "error": "Authorization already in progress. "
                 "Check the browser tab that just opened."},
                status_code=409,
            )
        _google_auth_state["status"] = "pending"
        _google_auth_state["detail"] = ""

    def _worker():
        try:
            from providers.google_calendar import GoogleCalendarProvider
            provider = GoogleCalendarProvider(
                credentials_path=creds_path,
                token_path=str(google_token_path()),
            )
            provider.authorize_interactive(port=8765)
            with _google_auth_lock:
                _google_auth_state["status"] = "success"
                _google_auth_state["detail"] = str(google_token_path())
        except Exception as ex:
            with _google_auth_lock:
                _google_auth_state["status"] = "error"
                _google_auth_state["detail"] = f"{type(ex).__name__}: {ex}"

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/setup/google/status")
async def google_status():
    with _google_auth_lock:
        return JSONResponse(dict(_google_auth_state))


# ---------- Microsoft 365 sub-wizard ----------
# Same shape as the Google wizard, but Microsoft's public-client flow runs
# on a different loopback port (8766) so both integrations can coexist.

_ms_auth_state: dict = {"status": "idle", "detail": ""}
_ms_auth_lock = threading.Lock()


@app.get("/setup/microsoft", response_class=HTMLResponse)
async def microsoft_wizard():
    env = _env_snapshot()
    return HTMLResponse(_render_microsoft_wizard(
        status=_microsoft_connection_status(env),
        client_id=env.get("MICROSOFT_CLIENT_ID", ""),
        tenant=env.get("MICROSOFT_TENANT") or "common",
        write_calendar_name=env.get("WRITE_CALENDAR_NAME") or "Study Blocks",
    ))


@app.post("/setup/microsoft/save")
async def microsoft_save(request: Request):
    """Persist the Azure AD app details (client_id + tenant) to .env so the
    authorize step below can pick them up."""
    form = await request.form()
    client_id = str(form.get("client_id", "")).strip()
    tenant = str(form.get("tenant", "")).strip() or "common"
    if not client_id:
        return JSONResponse(
            {"ok": False, "error": "Client ID is required."},
            status_code=400,
        )
    env = _env_snapshot()
    env["MICROSOFT_CLIENT_ID"] = client_id
    env["MICROSOFT_TENANT"] = tenant
    install_mod._write_env(env)
    return JSONResponse({"ok": True})


@app.post("/setup/microsoft/authorize")
async def microsoft_authorize():
    env = _env_snapshot()
    client_id = env.get("MICROSOFT_CLIENT_ID", "")
    tenant = env.get("MICROSOFT_TENANT") or "common"
    if not client_id:
        return JSONResponse(
            {"ok": False, "error": "Save the Client ID first (Step 6)."},
            status_code=400,
        )
    with _ms_auth_lock:
        if _ms_auth_state["status"] == "pending":
            return JSONResponse(
                {"ok": False, "error": "Authorization already in progress. "
                 "Check the browser tab that just opened."},
                status_code=409,
            )
        _ms_auth_state["status"] = "pending"
        _ms_auth_state["detail"] = ""

    def _worker():
        try:
            from providers.microsoft_graph_auth import authorize_interactive
            authorize_interactive(
                client_id=client_id,
                tenant=tenant,
                token_path=microsoft_token_path(),
            )
            with _ms_auth_lock:
                _ms_auth_state["status"] = "success"
                _ms_auth_state["detail"] = str(microsoft_token_path())
        except Exception as ex:
            with _ms_auth_lock:
                _ms_auth_state["status"] = "error"
                _ms_auth_state["detail"] = f"{type(ex).__name__}: {ex}"

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/setup/microsoft/status")
async def microsoft_status():
    with _ms_auth_lock:
        return JSONResponse(dict(_ms_auth_state))


# ---------- HTML templates (inline, no template engine) ----------

_PAGE_CSS = """
  :root {
    --canvas:      oklch(0.12 0.006 60);
    --canvas-2:    oklch(0.14 0.006 60);
    --surface:     oklch(0.17 0.008 60);
    --border:      oklch(0.26 0.010 60);
    --border-soft: oklch(0.20 0.008 60);
    --ink-1:       oklch(0.96 0.010 80);
    --ink-2:       oklch(0.70 0.015 70);
    --ink-3:       oklch(0.50 0.015 70);
    --accent:      oklch(0.82 0.14 75);
    --accent-quiet: oklch(0.30 0.06 75);
    --accent-on:   oklch(0.14 0.025 75);
    --error:       oklch(0.70 0.18 30);
    --success:     oklch(0.78 0.16 145);
    --ease-out:    cubic-bezier(0.23, 1, 0.32, 1);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
  body {
    background: var(--canvas); color: var(--ink-1);
    font-family: "Bricolage Grotesque", -apple-system, system-ui, sans-serif;
    font-size: 15px; line-height: 1.55; font-weight: 400;
  }
  ::selection { background: var(--accent); color: var(--accent-on); }

  .wrap { max-width: 640px; margin: 0 auto; padding: 56px 24px 96px; }
  .wrap-wide { max-width: 760px; margin: 0 auto; padding: 56px 24px 96px; }

  .brand {
    display: inline-flex; align-items: center; gap: 8px;
    font-weight: 500; font-size: 14px; color: var(--ink-2);
    margin-bottom: 48px; letter-spacing: -0.01em;
  }
  .brand .dot {
    width: 6px; height: 6px; border-radius: 999px; background: var(--accent);
  }

  h1 {
    font-size: clamp(28px, 4vw, 40px); font-weight: 500;
    letter-spacing: -0.025em; line-height: 1.1;
    margin-bottom: 14px; color: var(--ink-1);
  }
  h2 {
    font-size: 18px; font-weight: 500;
    color: var(--ink-1); margin-bottom: 10px;
    letter-spacing: -0.01em;
  }
  .lede {
    color: var(--ink-2); font-size: 16px; line-height: 1.55;
    max-width: 58ch; margin-bottom: 48px;
  }

  .section-label {
    display: block;
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 11px; font-weight: 500;
    color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.12em;
    margin-top: 48px; margin-bottom: 20px;
    padding-bottom: 10px; border-bottom: 1px solid var(--border-soft);
  }
  .section-label:first-of-type { margin-top: 0; }

  .field { margin-bottom: 18px; }
  .field > label {
    display: block; font-weight: 500; font-size: 14px;
    color: var(--ink-1); margin-bottom: 6px;
  }
  .field .hint {
    color: var(--ink-3); font-weight: 400; margin-left: 6px; font-size: 13px;
  }
  .field .note {
    color: var(--ink-3); font-size: 13px; line-height: 1.5;
    margin-top: 6px;
  }

  input[type=text], input[type=email], input[type=password],
  input[type=url], input[type=number], select {
    width: 100%; font: inherit; color: var(--ink-1);
    background: var(--canvas-2);
    border: 1px solid var(--border-soft); border-radius: 6px;
    padding: 10px 12px;
    transition: border-color 160ms var(--ease-out),
                background 160ms var(--ease-out);
  }
  input::placeholder { color: var(--ink-3); }
  input:hover, select:hover { border-color: var(--border); }
  input:focus, select:focus {
    outline: 0; border-color: var(--accent);
    background: color-mix(in oklch, var(--accent-quiet) 8%, var(--canvas-2));
  }

  .radios, .checks { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
  .radios label, .checks label {
    display: flex; align-items: flex-start; gap: 10px;
    font-weight: 400; cursor: pointer; color: var(--ink-1);
    padding: 8px 12px; border-radius: 8px;
    border: 1px solid var(--border-soft);
    background: var(--canvas-2);
    transition: border-color 160ms var(--ease-out);
  }
  .radios label:hover, .checks label:hover { border-color: var(--border); }
  .radios label.active, .checks label.active {
    border-color: var(--accent);
    background: color-mix(in oklch, var(--accent-quiet) 10%, var(--canvas-2));
  }
  .radios input[type=radio], .checks input[type=checkbox] {
    appearance: none; width: 16px; height: 16px; border-radius: 999px;
    border: 1.5px solid var(--border); background: transparent;
    cursor: pointer; transition: border-color 160ms var(--ease-out);
    position: relative; flex-shrink: 0; margin-top: 3px;
  }
  .checks input[type=checkbox] { border-radius: 4px; }
  .radios input[type=radio]:checked, .checks input[type=checkbox]:checked {
    border-color: var(--accent);
  }
  .radios input[type=radio]:checked::after {
    content: ""; position: absolute; inset: 3px; border-radius: 999px;
    background: var(--accent);
  }
  .checks input[type=checkbox]:checked::after {
    content: "✓"; position: absolute; inset: 0; display: grid; place-items: center;
    color: var(--accent); font-size: 12px; font-weight: 600;
  }
  .radio-main {
    display: flex; flex-direction: column; flex: 1;
  }
  .radio-main strong { font-weight: 500; font-size: 14px; color: var(--ink-1); }
  .radio-main span.sub {
    color: var(--ink-3); font-size: 13px; line-height: 1.45; margin-top: 2px;
  }

  .pill {
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 10px; font-weight: 500;
    color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.1em;
    padding: 2px 7px; border: 1px solid var(--border);
    border-radius: 3px; margin-left: 6px;
  }
  .pill-ok {
    color: var(--success); border-color: color-mix(in oklch, var(--success) 50%, transparent);
  }
  .pill-warn {
    color: var(--error); border-color: color-mix(in oklch, var(--error) 50%, transparent);
  }

  /* Conditional sub-forms that appear when a radio/check is selected */
  .subform {
    margin: 8px 0 14px 28px;
    padding: 14px 16px;
    background: var(--surface);
    border-left: 2px solid var(--accent-quiet);
    border-radius: 0 8px 8px 0;
    display: none;
  }
  .subform.visible { display: block; }
  .subform .field:last-child { margin-bottom: 0; }

  button.submit, button.primary {
    background: var(--accent); color: var(--accent-on);
    font: inherit; font-weight: 500; font-size: 15px;
    border: 0; border-radius: 8px; padding: 14px 22px;
    cursor: pointer;
    transition: transform 160ms var(--ease-out),
                filter 200ms var(--ease-out);
  }
  button.submit { margin-top: 48px; width: 100%; }
  button.primary:hover, button.submit:hover { filter: brightness(1.06); }
  button.primary:active, button.submit:active { transform: scale(0.98); }
  button.primary:disabled { opacity: 0.5; cursor: not-allowed; filter: none; }

  button.secondary {
    background: var(--canvas-2); color: var(--ink-1);
    font: inherit; font-weight: 500; font-size: 14px;
    border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px;
    cursor: pointer;
    transition: border-color 160ms var(--ease-out), background 160ms var(--ease-out);
  }
  button.secondary:hover { border-color: var(--accent); }

  code {
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    background: var(--surface); color: var(--ink-1);
    padding: 2px 6px; border-radius: 3px; font-size: 0.92em;
  }

  a { color: var(--accent); text-decoration: none;
      border-bottom: 1px solid var(--accent-quiet);
      transition: border-color 160ms var(--ease-out); }
  a:hover { border-bottom-color: var(--accent); }

  .notice-error {
    padding: 14px 18px; border-radius: 8px;
    background: color-mix(in oklch, var(--error) 10%, var(--canvas-2));
    border: 1px solid color-mix(in oklch, var(--error) 40%, transparent);
    color: var(--ink-1); margin-bottom: 24px;
  }
  .notice-ok {
    padding: 14px 18px; border-radius: 8px;
    background: color-mix(in oklch, var(--success) 10%, var(--canvas-2));
    border: 1px solid color-mix(in oklch, var(--success) 40%, transparent);
    color: var(--ink-1); margin-bottom: 24px;
  }

  .done-card {
    padding: 28px 32px;
    background: var(--canvas-2);
    border: 1px solid var(--border-soft); border-radius: 10px;
  }
  .done-card h3 {
    font-size: 14px; font-weight: 500; color: var(--ink-2);
    text-transform: uppercase; letter-spacing: 0.08em;
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    margin-bottom: 18px;
  }
  .done-card ol {
    padding-left: 0; list-style: none; counter-reset: step;
  }
  .done-card li {
    counter-increment: step;
    padding: 12px 0 12px 36px; position: relative;
    border-bottom: 1px solid var(--border-soft);
    color: var(--ink-1); font-size: 15px; line-height: 1.55;
  }
  .done-card li:last-child { border-bottom: 0; }
  .done-card li::before {
    content: counter(step); position: absolute; left: 0; top: 12px;
    width: 22px; height: 22px; border-radius: 999px;
    background: var(--accent); color: var(--accent-on);
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 11px; font-weight: 500;
    display: grid; place-items: center;
  }

  .wizard-step {
    padding: 20px 24px 20px 60px;
    margin-bottom: 14px;
    position: relative;
    background: var(--canvas-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
  }
  .wizard-step .step-num {
    position: absolute; left: 18px; top: 22px;
    width: 26px; height: 26px; border-radius: 999px;
    background: var(--accent); color: var(--accent-on);
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 12px; font-weight: 500;
    display: grid; place-items: center;
  }
  .wizard-step h3 {
    font-size: 16px; font-weight: 500; color: var(--ink-1);
    margin-bottom: 8px;
  }
  .wizard-step p {
    color: var(--ink-2); font-size: 14px; line-height: 1.55;
    margin-bottom: 10px;
  }
  .wizard-step p:last-child { margin-bottom: 0; }
  .wizard-step ul { padding-left: 20px; color: var(--ink-2); font-size: 14px; }
  .wizard-step ul li { margin-bottom: 4px; }
  .wizard-step .inputs { margin-top: 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .wizard-step input[type=file] {
    color: var(--ink-2); font: inherit; font-size: 13px;
    padding: 6px 0;
  }
  .wizard-step input[type=file]::file-selector-button {
    background: var(--canvas-2); color: var(--ink-1);
    font: inherit; font-weight: 500; font-size: 13px;
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px;
    cursor: pointer; margin-right: 10px;
    transition: border-color 160ms var(--ease-out);
  }
  .wizard-step input[type=file]::file-selector-button:hover { border-color: var(--accent); }

  .status-line {
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 13px; color: var(--ink-2); margin-top: 10px;
  }
  .status-line.ok { color: var(--success); }
  .status-line.err { color: var(--error); }

  /* Expandable "where do I find this?" help drawers */
  .help-drawer {
    margin-top: 8px; border-left: 2px solid var(--border-soft);
    padding-left: 14px;
  }
  .help-drawer[open] { border-left-color: var(--accent-quiet); }
  .help-drawer summary {
    color: var(--ink-3); font-size: 13px; cursor: pointer;
    padding: 4px 0;
    list-style: none; transition: color 160ms var(--ease-out);
  }
  .help-drawer summary::-webkit-details-marker { display: none; }
  .help-drawer summary::before {
    content: "▸ "; font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 10px; color: var(--ink-3);
    transition: transform 160ms var(--ease-out); display: inline-block;
  }
  .help-drawer[open] summary::before { content: "▾ "; color: var(--accent); }
  .help-drawer[open] summary { color: var(--ink-2); }
  .help-drawer summary:hover { color: var(--ink-1); }
  .help-drawer .steps {
    margin: 8px 0 4px; padding-left: 0; list-style: none;
    counter-reset: helpstep; color: var(--ink-2); font-size: 13px;
    line-height: 1.55;
  }
  .help-drawer .steps li {
    counter-increment: helpstep; padding: 4px 0 4px 24px; position: relative;
  }
  .help-drawer .steps li::before {
    content: counter(helpstep); position: absolute; left: 0; top: 4px;
    width: 18px; height: 18px; border-radius: 999px;
    background: var(--surface); color: var(--ink-2);
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 10px; font-weight: 500;
    display: grid; place-items: center;
  }

  @media (prefers-reduced-motion: reduce) {
    *, button, input, .subform { transition: none !important; }
  }
"""

_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Bricolage+Grotesque:opsz,wght@12..96,300..600&'
    'family=JetBrains+Mono:wght@400;500&display=swap">'
)


# A tiny script that: (1) keeps subforms visible for the active radio/check;
# (2) highlights the active label; (3) is idempotent (re-run on every
# DOMContentLoaded — safe on back/forward).
_FORM_JS = """
(function() {
  function syncGroup(name) {
    var els = document.querySelectorAll('input[name="' + name + '"]');
    els.forEach(function(el) {
      var label = el.closest('label');
      if (!label) return;
      if (el.checked) label.classList.add('active'); else label.classList.remove('active');
      // Radios share a name, so key subform id by VALUE.
      // Checkboxes don't — key subform id by NAME.
      var subId = (el.type === 'radio') ? ('sub-' + el.value) : ('sub-' + name);
      var sub = document.getElementById(subId);
      if (sub) sub.classList.toggle('visible', el.checked);
    });
  }
  function setup(name) {
    document.querySelectorAll('input[name="' + name + '"]').forEach(function(el) {
      el.addEventListener('change', function() { syncGroup(name); });
    });
    syncGroup(name);
  }
  document.addEventListener('DOMContentLoaded', function() {
    setup('approval_mode');
    setup('calendar');
    setup('notifier');
    setup('src_canvas');
    setup('src_todoist');
    setup('src_gmail');
    setup('src_gtasks');
    setup('src_outlook_mail');
    setup('src_mstodo');
  });
})();
"""


# ---------- Main form ----------

def _render_form(d: dict, *, cal_default: str, notifier_default: str,
                 task_canvas: bool, task_todoist: bool,
                 task_gmail: bool, task_gtasks: bool,
                 task_outlook_mail: bool, task_mstodo: bool,
                 google_status: str, microsoft_status: str,
                 is_macos: bool) -> str:

    def rad(name, value, default, label_main, label_sub=""):
        checked = "checked" if default == value else ""
        sub = f'<span class="sub">{label_sub}</span>' if label_sub else ""
        return (
            f'<label><input type="radio" name="{name}" value="{value}" {checked}>'
            f'<span class="radio-main"><strong>{label_main}</strong>{sub}</span></label>'
        )

    def chk(name, value, checked, label_main, label_sub=""):
        c = "checked" if checked else ""
        sub = f'<span class="sub">{label_sub}</span>' if label_sub else ""
        return (
            f'<label><input type="checkbox" name="{name}" value="{value}" {c}>'
            f'<span class="radio-main"><strong>{label_main}</strong>{sub}</span></label>'
        )

    # Connector button helper — a small link styled as either primary CTA
    # or a subtle "reconnect" link based on the current status.
    def _connect_btn(url: str, status: str, label: str) -> str:
        if status == "connected":
            return (
                '<span class="pill pill-ok">✓ Connected</span> '
                f'<a href="{url}" target="_blank" style="font-size:13px">'
                'Reconnect or switch account →</a>'
            )
        if status in ("credentials_only", "client_id_only"):
            return (
                '<span class="pill pill-warn">Needs sign-in</span> '
                f'<a class="primary" href="{url}" target="_blank" '
                'style="display:inline-block; padding:8px 14px; border-radius:8px; '
                'color: var(--accent-on); background: var(--accent); '
                f'border-bottom:0; text-decoration:none; font-size:13px">Finish {label} sign-in →</a>'
            )
        return (
            f'<a class="primary" href="{url}" target="_blank" '
            'style="display:inline-block; padding:8px 14px; border-radius:8px; '
            'color: var(--accent-on); background: var(--accent); '
            f'border-bottom:0; text-decoration:none; font-size:13px">Connect {label} →</a>'
            ' <span style="color: var(--ink-3); font-size: 13px;">'
            'Opens a step-by-step guide in a new tab.</span>'
        )

    google_btn = _connect_btn("/setup/google", google_status, "Google Calendar")
    microsoft_btn = _connect_btn("/setup/microsoft", microsoft_status, "Microsoft 365")

    mac_pill = '<span class="pill">macOS</span>' if is_macos else '<span class="pill pill-warn">macOS only</span>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPlan · setup</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap-wide">
  <span class="brand"><span class="dot"></span>AutoPlan</span>

  <h1>Let's get you set up.</h1>
  <p class="lede">
    Pick the pieces you want to use. Only four sections are required:
    your Anthropic key, approval mode, a calendar, and a notifier. Task
    sources and email scanning are optional — leave them unchecked if
    you'd rather add tasks by hand or by talking to the AI. Every
    credential field has a "Where do I find this?" expander with
    step-by-step instructions. You can re-run this wizard anytime to
    change anything.
  </p>

  <form method="POST" action="/setup" autocomplete="off">

    <span class="section-label">Core</span>
    <div class="field">
      <label>Anthropic API key
        <span class="hint">powers the AI layer: morning summary, natural-language task input, and email scanning</span>
      </label>
      <input type="password" name="anthropic_api_key"
             value="{d['ANTHROPIC_API_KEY']}" required>
      <details class="help-drawer">
        <summary>Where do I get this?</summary>
        <ol class="steps">
          <li>Go to <a href="https://console.anthropic.com/settings/keys" target="_blank">console.anthropic.com/settings/keys</a>.</li>
          <li>Sign in. First-time: create an account; add $5 of credit at <em>Billing → Credits</em>. Typical usage stays well under $5/month unless you turn on heavy email scanning.</li>
          <li>Click <strong>Create Key</strong>. Name it <code>AutoPlan</code>.</li>
          <li>Copy the key that starts with <code>sk-ant-</code> and paste it here.</li>
          <li>You'll only see the key once. If you lose it, revoke the old one and create a new key — the old key stops working immediately.</li>
        </ol>
      </details>
    </div>
    <div class="field">
      <label>Timezone</label>
      <input type="text" name="timezone" value="{d['TIMEZONE']}"
             placeholder="America/New_York">
      <p class="note">Any IANA zone name (America/New_York, Europe/Berlin, Asia/Tokyo, …).</p>
    </div>
    <div class="field">
      <label>Hub auth token
        <span class="hint">generated · save the hub URL</span>
      </label>
      <input type="text" name="replan_token" value="{d['REPLAN_TOKEN']}">
    </div>

    <span class="section-label">How new tasks arrive</span>
    <p class="note" style="margin-bottom: 14px">
      When AutoPlan pulls in a new task from your email, to-do app, or
      another source, you decide what happens next.
    </p>
    <div class="radios">
      <label><input type="radio" name="approval_mode" value="auto"
        {('checked' if d['DEFAULT_REQUIRE_APPROVAL'] not in ('true','1','yes') else '')}>
        <span class="radio-main"><strong>Auto &mdash; schedule immediately</strong>
          <span class="sub">AutoPlan estimates a duration, picks a time, and blocks the calendar.
            Good for individuals who trust the agent and want zero friction.</span></span></label>
      <label><input type="radio" name="approval_mode" value="review"
        {('checked' if d['DEFAULT_REQUIRE_APPROVAL'] in ('true','1','yes') else '')}>
        <span class="radio-main"><strong>Review first &mdash; send to hub for approval</strong>
          <span class="sub">New tasks land in the hub's Pending Review queue. You can
            approve, edit, or remove them, then send them through to the scheduler.
            Good for teams, client-facing work, or any workflow where "the AI
            decided" isn't a good answer.</span></span></label>
    </div>

    <span class="section-label">Calendar · where AutoPlan writes your schedule</span>
    <div class="radios">
      {rad('calendar', 'icloud', cal_default, 'Apple Calendar (iCloud)', 'Built into every Mac. Uses CalDAV + an app-specific password.')}
      {rad('calendar', 'google', cal_default, 'Google Calendar', 'Signs you in with Google in the browser. 10-minute first-time setup via guided wizard.')}
      {rad('calendar', 'outlook', cal_default, 'Outlook / Microsoft 365', 'Signs you in with Microsoft in the browser. Requires a one-time Azure AD app registration — guided wizard walks you through it.')}
      {rad('calendar', 'caldav', cal_default, 'Fastmail, Posteo, Nextcloud, or other CalDAV server', 'Anything that speaks the CalDAV protocol. Bring your own server URL.')}
      {rad('calendar', 'skip', cal_default, 'Skip for now', "Don't connect a calendar yet. AutoPlan can still read tasks but won't place events.")}
    </div>

    <div id="sub-icloud" class="subform">
      <div class="field">
        <label>Apple ID email
          <span class="hint">primary Apple ID, not a @icloud.com alias</span>
        </label>
        <input type="email" name="icloud_user" value="{d['ICLOUD_USER']}">
        <details class="help-drawer">
          <summary>Which email do I use?</summary>
          <ol class="steps">
            <li>Go to <a href="https://appleid.apple.com" target="_blank">appleid.apple.com</a> and sign in.</li>
            <li>At the top of the page under <em>Sign-In and Security</em>, look for your <em>Apple Account</em> email.</li>
            <li>Use that one — it's usually your original email, not a <code>@icloud.com</code> alias.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>App-specific password
          <span class="hint">NOT your regular Apple password</span>
        </label>
        <input type="password" name="icloud_app_password"
               value="{d['ICLOUD_APP_PASSWORD']}"
               placeholder="xxxx-xxxx-xxxx-xxxx">
        <details class="help-drawer">
          <summary>How do I generate one?</summary>
          <ol class="steps">
            <li>Go to <a href="https://appleid.apple.com" target="_blank">appleid.apple.com</a> and sign in.</li>
            <li>Click <strong>Sign-In and Security → App-Specific Passwords</strong>.</li>
            <li>Click <strong>Generate an app-specific password</strong>.</li>
            <li>Label it <code>AutoPlan</code>. Confirm with Face ID / Touch ID / password.</li>
            <li>Copy the four-group code (format <code>xxxx-xxxx-xxxx-xxxx</code>) and paste it here. You can't see it again after closing the page.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-google" class="subform">
      <div style="margin-bottom: 14px">{google_btn}</div>
      <p class="note">
        Google requires each user to register AutoPlan with their own Google Cloud
        account (this keeps your data private to you). The wizard walks through
        every click and takes about 10 minutes the first time. The same connection
        also powers Gmail scanning and Google Tasks below.
      </p>
    </div>

    <div id="sub-outlook" class="subform">
      <div style="margin-bottom: 14px">{microsoft_btn}</div>
      <p class="note">
        Microsoft requires each user to register AutoPlan with their own Azure AD
        tenant (this keeps your data private to you). The wizard walks through
        every click and takes about 10 minutes the first time. The same connection
        also powers Outlook Mail scanning and Microsoft To Do below.
      </p>
    </div>

    <div id="sub-caldav" class="subform">
      <div class="field">
        <label>Server URL</label>
        <input type="url" name="caldav_url" value="{d['CALDAV_URL']}"
               placeholder="https://caldav.fastmail.com/">
        <details class="help-drawer">
          <summary>Common server URLs</summary>
          <ol class="steps">
            <li>Fastmail: <code>https://caldav.fastmail.com/</code></li>
            <li>Posteo: <code>https://posteo.de:8443/</code></li>
            <li>Mailbox.org: <code>https://dav.mailbox.org/</code></li>
            <li>Nextcloud: <code>https://your-host/remote.php/dav/</code> (replace <code>your-host</code>)</li>
            <li>Self-hosted Radicale / Baikal: whatever URL your server exposes.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Username</label>
        <input type="text" name="caldav_user" value="{d['CALDAV_USER']}">
        <details class="help-drawer">
          <summary>What username?</summary>
          <ol class="steps">
            <li>Fastmail: your full Fastmail email (e.g. <code>you@fastmail.com</code>).</li>
            <li>Posteo: your Posteo email.</li>
            <li>Nextcloud / Mailbox: your account login.</li>
            <li>Self-hosted: whatever username you configured on the server.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Password
          <span class="hint">service password / CalDAV-specific password</span>
        </label>
        <input type="password" name="caldav_password" value="{d['CALDAV_PASSWORD']}">
        <details class="help-drawer">
          <summary>How do I get a CalDAV password?</summary>
          <ol class="steps">
            <li>Fastmail: Settings → Privacy &amp; Security → App passwords → <strong>New app password</strong>. Pick access to "Calendar" and "Contacts".</li>
            <li>Posteo: Settings → My data → Create app-specific password.</li>
            <li>Nextcloud: Personal settings → Security → Devices &amp; sessions → <strong>Create new app password</strong>.</li>
            <li>Self-hosted: whatever password you use to sign into the server directly.</li>
          </ol>
        </details>
      </div>
    </div>

    <div class="field" style="margin-top: 14px">
      <label>Write-calendar name
        <span class="hint">the calendar AutoPlan blocks time on</span>
      </label>
      <input type="text" name="write_calendar_name" value="{d['WRITE_CALENDAR_NAME']}">
      <p class="note">
        Create this calendar in your calendar app first. For Apple Calendar:
        File → New Calendar → iCloud. For Google: calendar.google.com →
        + next to "Other calendars" → Create new calendar.
      </p>
    </div>

    <span class="section-label">Task sources · where tasks come from</span>
    <p class="note" style="margin-bottom: 14px">
      Turn on any combination. You can also add tasks manually from the
      hub or by chatting with the AI.
    </p>
    <div class="checks">
      {chk('src_canvas', '1', task_canvas, 'Canvas LMS', 'Pull assignments from your Canvas account. Needs a personal access token.')}
      {chk('src_todoist', '1', task_todoist, 'Todoist', 'Pull tasks from your Todoist inbox / projects. Works on every platform.')}
      {chk('src_gmail', '1', task_gmail, 'Gmail inbox scanning', 'Claude reads your recent unread emails and extracts tasks from them. Uses the Google connection you configured above. Costs Anthropic tokens per scan.')}
      {chk('src_gtasks', '1', task_gtasks, 'Google Tasks', 'Pull open items from tasks.google.com. Uses the Google connection above. Free.')}
      {chk('src_outlook_mail', '1', task_outlook_mail, 'Outlook Mail inbox scanning', 'Claude reads your recent unread Outlook/Microsoft 365 emails and extracts tasks. Uses the Microsoft connection above. Costs Anthropic tokens per scan.')}
      {chk('src_mstodo', '1', task_mstodo, 'Microsoft To Do', 'Pull tasks from Microsoft To Do. Uses the Microsoft connection above. Free.')}
    </div>

    <div id="sub-src_canvas" class="subform">
      <div class="field">
        <label>Canvas API base URL</label>
        <input type="text" name="canvas_base_url" value="{d['CANVAS_BASE_URL']}"
               placeholder="https://your-school.instructure.com/api/v1">
        <details class="help-drawer">
          <summary>What URL do I put here?</summary>
          <ol class="steps">
            <li>Open your normal Canvas login page in a browser.</li>
            <li>Look at the address bar. The part before the first <code>/</code> after <code>https://</code> is your school's Canvas hostname (e.g. <code>canvas.harvard.edu</code>).</li>
            <li>Put <code>https://that-hostname/api/v1</code> in this field.</li>
            <li>Example: if your Canvas is at <code>psu.instructure.com</code>, use <code>https://psu.instructure.com/api/v1</code>.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Canvas access token</label>
        <input type="password" name="canvas_token" value="{d['CANVAS_TOKEN']}">
        <details class="help-drawer">
          <summary>How do I get the token?</summary>
          <ol class="steps">
            <li>Sign in to your Canvas account in a browser.</li>
            <li>Click your profile icon → <strong>Settings</strong>.</li>
            <li>Scroll to <strong>Approved Integrations</strong> → <strong>+ New Access Token</strong>.</li>
            <li>Purpose: <code>AutoPlan</code>. Leave the expiry blank (or far in the future).</li>
            <li>Click <strong>Generate Token</strong>, copy the long string, paste it here.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-src_todoist" class="subform" style="margin-top:-8px">
      <div class="field">
        <label>Todoist API token</label>
        <input type="password" name="todoist_token" value="{d['TODOIST_TOKEN']}"
               placeholder="pasted from Todoist Developer settings">
        <details class="help-drawer">
          <summary>How do I get this?</summary>
          <ol class="steps">
            <li>Sign in to <a href="https://todoist.com" target="_blank">todoist.com</a>.</li>
            <li>Click your profile icon → <strong>Settings</strong>.</li>
            <li>Open the <strong>Integrations</strong> tab, then switch to the <strong>Developer</strong> sub-tab.</li>
            <li>Copy the <strong>API token</strong> and paste it here.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-src_gmail" class="subform" style="margin-top:-8px">
      <p class="note">Requires the Google Calendar connection above. If you
        haven't connected Google yet, do that first — the same credentials
        cover Gmail with no extra setup.</p>
      <div class="field">
        <label>Label filter <span class="hint">optional, comma-separated</span></label>
        <input type="text" name="gmail_label_filter" value="{d['GMAIL_LABEL_FILTER']}"
               placeholder="IMPORTANT, TODO">
        <p class="note">Leave blank to scan unread mail from the last 3 days.
          If set, only messages with one of these Gmail labels are scanned.</p>
      </div>
      <div class="field">
        <label>Max messages per scan</label>
        <input type="number" name="gmail_max_messages" value="{d['GMAIL_MAX_MESSAGES']}">
        <p class="note">Anthropic charges per email scanned, so this caps
          the per-run cost. 25 is a safe default.</p>
      </div>
    </div>

    <div id="sub-src_gtasks" class="subform" style="margin-top:-8px">
      <p class="note">Requires the Google Calendar connection above. No
        additional settings — all of your task lists are pulled.</p>
    </div>

    <div id="sub-src_outlook_mail" class="subform" style="margin-top:-8px">
      <p class="note">Requires the Microsoft 365 connection above. If you
        haven't connected Microsoft yet, do that first — the same app
        registration covers Outlook Mail with no extra setup.</p>
      <div class="field">
        <label>Folder</label>
        <input type="text" name="outlook_mail_folder" value="{d['OUTLOOK_MAIL_FOLDER']}"
               placeholder="inbox">
        <p class="note">The Outlook mailFolder to scan. "inbox" is the default;
          use another folder name to scan only that one.</p>
      </div>
      <div class="field">
        <label>Category filter <span class="hint">optional, comma-separated</span></label>
        <input type="text" name="outlook_category_filter" value="{d['OUTLOOK_CATEGORY_FILTER']}"
               placeholder="Action, Follow-up">
        <p class="note">If set, only messages with one of these Outlook
          categories are scanned.</p>
      </div>
      <div class="field">
        <label>Max messages per scan</label>
        <input type="number" name="outlook_max_messages" value="{d['OUTLOOK_MAX_MESSAGES']}">
      </div>
    </div>

    <div id="sub-src_mstodo" class="subform" style="margin-top:-8px">
      <p class="note">Requires the Microsoft 365 connection above. All of
        your Microsoft To Do lists are pulled automatically.</p>
    </div>

    <span class="section-label">Notifications · how AutoPlan pings you</span>
    <p class="note" style="margin-bottom: 14px">
      Pick one. This is where the daily morning summary and at-risk
      alerts get sent.
    </p>
    <div class="radios">
      {rad('notifier', 'imessage', notifier_default, 'iMessage' + (' ' + mac_pill if not is_macos else ''), 'Sends a text to your own Apple ID. Mac-only; needs Messages.app signed in.')}
      {rad('notifier', 'pushover', notifier_default, 'Pushover', 'Most reliable mobile push. One-time $5 per platform.')}
      {rad('notifier', 'slack', notifier_default, 'Slack DM or channel', 'Free. Uses an Incoming Webhook — any workspace works.')}
      {rad('notifier', 'email', notifier_default, 'Email (SMTP)', 'Gmail, Fastmail, Outlook, SES, Mailgun, your company mail — anything that speaks SMTP.')}
      {rad('notifier', 'ntfy', notifier_default, 'ntfy.sh push', 'Free, cross-platform, no account. Install the ntfy app on your phone and subscribe to your topic.')}
    </div>

    <div id="sub-imessage" class="subform">
      <div class="field">
        <label>Phone number
          <span class="hint">E.164 format, e.g. +15551234567</span>
        </label>
        <input type="text" name="user_phone" value="{d['USER_PHONE']}"
               placeholder="+15551234567">
        <details class="help-drawer">
          <summary>Which number do I use?</summary>
          <ol class="steps">
            <li>Use the phone number associated with your iMessage (the number on your iPhone signed into the same Apple ID).</li>
            <li>Start with <code>+</code> and your country code. US numbers look like <code>+15551234567</code>.</li>
            <li>AutoPlan runs <code>Messages.app</code> on your Mac to send. Make sure iMessage is enabled and signed in.</li>
            <li>iMessage to yourself is silent on iPhone (Apple blocks same-ID push). Pick Pushover/Slack/email/ntfy if you need a real push.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-pushover" class="subform">
      <div class="field">
        <label>User key</label>
        <input type="password" name="pushover_user_key" value="{d['PUSHOVER_USER_KEY']}">
        <details class="help-drawer">
          <summary>Where is my user key?</summary>
          <ol class="steps">
            <li>Sign up (or sign in) at <a href="https://pushover.net" target="_blank">pushover.net</a>. One-time $5 per platform (iOS / Android / desktop).</li>
            <li>Install the Pushover app on your phone and sign in there too.</li>
            <li>On <a href="https://pushover.net" target="_blank">pushover.net</a>, look at the top-right: a 30-character <strong>User Key</strong> is shown.</li>
            <li>Copy it and paste it here.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Application token</label>
        <input type="password" name="pushover_app_token" value="{d['PUSHOVER_APP_TOKEN']}">
        <details class="help-drawer">
          <summary>How do I get the application token?</summary>
          <ol class="steps">
            <li>On <a href="https://pushover.net" target="_blank">pushover.net</a>, click <strong>Your Applications</strong> (top menu) or go to <a href="https://pushover.net/apps/build" target="_blank">pushover.net/apps/build</a>.</li>
            <li>Name: <code>AutoPlan</code>. Type: <em>Application</em>. Description: <em>Personal planning assistant</em>.</li>
            <li>Click <strong>Create Application</strong>.</li>
            <li>On the resulting page, copy the <strong>API Token/Key</strong> and paste it here.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-slack" class="subform">
      <div class="field">
        <label>Incoming webhook URL</label>
        <input type="url" name="slack_webhook_url" value="{d['SLACK_WEBHOOK_URL']}"
               placeholder="https://hooks.slack.com/services/T000/B000/xxxx">
        <details class="help-drawer">
          <summary>How do I create an incoming webhook?</summary>
          <ol class="steps">
            <li>Go to <a href="https://api.slack.com/apps" target="_blank">api.slack.com/apps</a> and sign in to your workspace.</li>
            <li>Click <strong>Create New App → From scratch</strong>. Name it <code>AutoPlan</code>, pick your workspace, click <strong>Create App</strong>.</li>
            <li>In the left sidebar click <strong>Incoming Webhooks</strong> and toggle it <em>On</em>.</li>
            <li>At the bottom, click <strong>Add New Webhook to Workspace</strong>. Pick the channel or DM (DM yourself for private notifications) and click <strong>Allow</strong>.</li>
            <li>Copy the resulting URL (starts with <code>https://hooks.slack.com/services/</code>) and paste it here.</li>
          </ol>
        </details>
      </div>
    </div>

    <div id="sub-email" class="subform">
      <div class="field">
        <label>SMTP host</label>
        <input type="text" name="smtp_host" value="{d['SMTP_HOST']}"
               placeholder="smtp.gmail.com">
        <details class="help-drawer">
          <summary>Common SMTP hosts</summary>
          <ol class="steps">
            <li>Gmail / Google Workspace: <code>smtp.gmail.com</code></li>
            <li>Outlook / Office 365: <code>smtp.office365.com</code></li>
            <li>Fastmail: <code>smtp.fastmail.com</code></li>
            <li>iCloud: <code>smtp.mail.me.com</code></li>
            <li>Your company email: ask IT for the SMTP host, or check your email client's existing settings.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Port</label>
        <input type="number" name="smtp_port" value="{d['SMTP_PORT']}"
               placeholder="587">
        <details class="help-drawer">
          <summary>Which port?</summary>
          <ol class="steps">
            <li><strong>587</strong> (default) is right for Gmail, Outlook, Fastmail, and most modern providers — uses STARTTLS.</li>
            <li><strong>465</strong> is for direct TLS (older setups).</li>
            <li>If unsure, try 587 first.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Username (your email)</label>
        <input type="email" name="smtp_username" value="{d['SMTP_USERNAME']}">
      </div>
      <div class="field">
        <label>Password</label>
        <input type="password" name="smtp_password" value="{d['SMTP_PASSWORD']}">
        <details class="help-drawer">
          <summary>Why won't my regular password work?</summary>
          <ol class="steps">
            <li>Most providers disable plain-password SMTP for security. You need an <em>app password</em>.</li>
            <li>Gmail: enable 2-factor auth, then go to <a href="https://myaccount.google.com/apppasswords" target="_blank">myaccount.google.com/apppasswords</a>, create one for "Mail", paste the 16-char password here.</li>
            <li>Outlook / Office 365: <a href="https://account.microsoft.com/security" target="_blank">account.microsoft.com/security</a> → Advanced security options → App passwords.</li>
            <li>Fastmail: Settings → Privacy &amp; Security → App passwords → <strong>New app password</strong> scoped to <em>Mail (SMTP)</em>.</li>
            <li>iCloud: <a href="https://appleid.apple.com" target="_blank">appleid.apple.com</a> → Sign-In and Security → App-Specific Passwords.</li>
          </ol>
        </details>
      </div>
      <div class="field">
        <label>Send to
          <span class="hint">leave blank to send to yourself</span>
        </label>
        <input type="email" name="smtp_to" value="{d['SMTP_TO']}"
               placeholder="same as username by default">
      </div>
    </div>

    <div id="sub-ntfy" class="subform">
      <div class="field">
        <label>Topic
          <span class="hint">treat as a secret · pre-generated for you</span>
        </label>
        <input type="text" name="ntfy_topic" value="{d['NTFY_TOPIC']}">
        <details class="help-drawer">
          <summary>How do I use ntfy?</summary>
          <ol class="steps">
            <li>Install the free <strong>ntfy</strong> app on your phone (App Store / Google Play) or go to <a href="https://ntfy.sh" target="_blank">ntfy.sh</a> in a browser.</li>
            <li>Tap the <em>+</em> to subscribe to a topic.</li>
            <li>Paste the topic above into the app.</li>
            <li>Optional but recommended: enable Instant Delivery in the app's settings (iOS) so pushes arrive immediately.</li>
            <li>Keep the topic secret — anyone who knows it can both send and read your pushes.</li>
          </ol>
        </details>
      </div>
    </div>

    <button type="submit" class="submit">Save and finish setup</button>
  </form>
</div>
<script>{_FORM_JS}</script>
</body></html>"""


# ---------- Google sub-wizard ----------

def _render_google_wizard(*, status: str, write_calendar_name: str) -> str:
    # Banner based on current state
    if status == "connected":
        banner = (
            '<div class="notice-ok">'
            '<strong>✓ Google Calendar is connected.</strong> '
            'You can close this tab and go back to the setup form.'
            '</div>'
        )
    elif status == "credentials_only":
        banner = (
            '<div class="notice-ok" style="background: color-mix(in oklch, var(--accent) 12%, var(--canvas-2)); '
            'border-color: color-mix(in oklch, var(--accent) 40%, transparent);">'
            '<strong>credentials.json saved.</strong> Now click '
            '<em>Start Google sign-in</em> in Step 10 below to finish.'
            '</div>'
        )
    else:
        banner = ''

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPlan · connect Google Calendar</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap-wide">
  <span class="brand"><span class="dot"></span>AutoPlan</span>
  <h1>Connect Google Calendar</h1>
  <p class="lede">
    Follow the 10 steps below. You'll do steps 1-7 on Google's website,
    then come back here for steps 8-10. The whole thing takes about 10
    minutes the first time, and you never have to do it again.
  </p>

  {banner}

  <div class="wizard-step">
    <div class="step-num">1</div>
    <h3>Open Google Cloud Console</h3>
    <p>
      Visit <a href="https://console.cloud.google.com/" target="_blank">console.cloud.google.com</a>
      and sign in with the Google account whose calendar AutoPlan
      should use. Accept any Terms of Service if prompted.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">2</div>
    <h3>Create a new project</h3>
    <p>
      Top of the page, click the project dropdown (to the right of "Google Cloud"). In
      the popup, click <strong>NEW PROJECT</strong> (top right).
    </p>
    <ul>
      <li>Project name: <code>AutoPlan</code></li>
      <li>Location: leave as "No organization"</li>
    </ul>
    <p>Click <strong>CREATE</strong>. Wait a few seconds, then click <strong>SELECT PROJECT</strong> on the notification.</p>
  </div>

  <div class="wizard-step">
    <div class="step-num">3</div>
    <h3>Turn on the Calendar API</h3>
    <p>
      In the search bar at the very top, type
      <strong>Google Calendar API</strong> and click the first result.
      Click the big blue <strong>ENABLE</strong> button.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">4</div>
    <h3>Set up the consent screen</h3>
    <p>
      Open the ≡ menu (top-left) → <strong>APIs &amp; Services → OAuth consent screen</strong>.
    </p>
    <p><strong>If you see "Get started":</strong></p>
    <ul>
      <li>Click GET STARTED.</li>
      <li>App name: <code>AutoPlan</code>. User support email: your email.</li>
      <li>Audience: pick <strong>External</strong> → NEXT.</li>
      <li>Contact email: yours → NEXT → agree → CREATE.</li>
    </ul>
    <p><strong>If you see "User Type":</strong></p>
    <ul>
      <li>Pick <strong>External</strong> → CREATE.</li>
      <li>App name <code>AutoPlan</code>, your support email, your dev contact email → SAVE AND CONTINUE.</li>
      <li>Scopes page → SAVE AND CONTINUE (don't add anything).</li>
      <li>Test users → + ADD USERS → type your own Gmail → ADD → SAVE AND CONTINUE.</li>
    </ul>
    <p style="color: var(--ink-3); font-size: 13px">
      "External" sounds alarming but it just means "any Google account."
      Only people on your test-users list can use AutoPlan — it is not
      shown publicly.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">5</div>
    <h3>Make sure you're a test user</h3>
    <p>
      Still inside OAuth consent screen, find the <strong>Test users</strong>
      section. If your own Gmail isn't listed, click <strong>+ ADD USERS</strong>,
      type it, click ADD, then SAVE.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">6</div>
    <h3>Create the credentials file</h3>
    <p>
      Left sidebar → <strong>APIs &amp; Services → Credentials</strong>.
      Top of page: <strong>+ CREATE CREDENTIALS → OAuth client ID</strong>.
    </p>
    <ul>
      <li>Application type: <strong>Desktop app</strong></li>
      <li>Name: <code>AutoPlan desktop</code></li>
    </ul>
    <p>Click <strong>CREATE</strong>. A popup shows "OAuth client created." Click <strong>DOWNLOAD JSON</strong>.</p>
  </div>

  <div class="wizard-step">
    <div class="step-num">7</div>
    <h3>Find the downloaded file</h3>
    <p>
      A file like <code>client_secret_…googleusercontent.com.json</code>
      is now in your Downloads folder. You don't have to rename it.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">8</div>
    <h3>Upload the file to AutoPlan</h3>
    <p>Drag it below (or click <em>Choose file</em>), then click <strong>Upload</strong>.</p>
    <div class="inputs">
      <input type="file" id="credsFile" accept="application/json,.json">
      <button type="button" id="uploadBtn" class="secondary">Upload</button>
    </div>
    <div id="uploadStatus" class="status-line"></div>
  </div>

  <div class="wizard-step">
    <div class="step-num">9</div>
    <h3>Create the "Study Blocks" calendar</h3>
    <p>
      In a new tab, open <a href="https://calendar.google.com" target="_blank">calendar.google.com</a>.
      On the left sidebar, find <strong>Other calendars</strong>, click the
      <strong>+</strong>, pick <strong>Create new calendar</strong>, name it
      exactly <code>{write_calendar_name}</code>, and click Create calendar.
    </p>
    <p style="color: var(--ink-3); font-size: 13px">
      Want a different name? You can change it on the main setup form — just
      make sure the name matches exactly.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">10</div>
    <h3>Sign in to Google</h3>
    <p>
      Click the button below. A new browser tab will open for Google
      sign-in. Pick your account, then click through the permission prompts.
    </p>
    <p style="color: var(--ink-3); font-size: 13px">
      You'll see "Google hasn't verified this app" — click <em>Advanced</em> then
      <em>Go to AutoPlan (unsafe)</em>. It is safe; "unverified" just means
      it's your own copy that only you can use.
    </p>
    <div class="inputs">
      <button type="button" id="authorizeBtn" class="primary" disabled>Start Google sign-in</button>
    </div>
    <div id="authStatus" class="status-line"></div>
  </div>

  <p style="margin-top: 32px; color: var(--ink-3); font-size: 13px">
    All set? Close this tab and go back to the main
    <a href="/setup">setup page</a>.
  </p>
</div>
<script>
(function() {{
  var fileInput   = document.getElementById('credsFile');
  var uploadBtn   = document.getElementById('uploadBtn');
  var uploadStat  = document.getElementById('uploadStatus');
  var authBtn     = document.getElementById('authorizeBtn');
  var authStat    = document.getElementById('authStatus');
  var initialStatus = {repr(status)!s};

  if (initialStatus === 'connected' || initialStatus === 'credentials_only') {{
    authBtn.disabled = false;
    if (initialStatus === 'connected') {{
      authStat.textContent = '✓ Google Calendar already connected. You can redo the sign-in from here if you need to switch accounts.';
      authStat.className = 'status-line ok';
    }}
    if (initialStatus === 'credentials_only') {{
      uploadStat.textContent = '✓ credentials.json already saved. Skip to Step 10.';
      uploadStat.className = 'status-line ok';
    }}
  }}

  uploadBtn.addEventListener('click', function() {{
    var f = fileInput.files[0];
    if (!f) {{
      uploadStat.textContent = 'Pick a credentials.json file first.';
      uploadStat.className = 'status-line err';
      return;
    }}
    var fd = new FormData();
    fd.append('credentials', f);
    uploadStat.textContent = 'Uploading…';
    uploadStat.className = 'status-line';
    fetch('/setup/google/upload', {{method: 'POST', body: fd}})
      .then(function(r) {{ return r.json().then(function(j) {{ return {{ok:r.ok, j:j}}; }}); }})
      .then(function(res) {{
        if (res.ok && res.j.ok) {{
          uploadStat.textContent = '✓ Saved to ' + res.j.path + '. On to Step 10.';
          uploadStat.className = 'status-line ok';
          authBtn.disabled = false;
        }} else {{
          uploadStat.textContent = '✗ ' + (res.j.error || 'Upload failed.');
          uploadStat.className = 'status-line err';
        }}
      }})
      .catch(function(err) {{
        uploadStat.textContent = '✗ ' + err;
        uploadStat.className = 'status-line err';
      }});
  }});

  authBtn.addEventListener('click', function() {{
    authBtn.disabled = true;
    authStat.textContent = 'Opening Google sign-in in a new tab…';
    authStat.className = 'status-line';
    fetch('/setup/google/authorize', {{method: 'POST'}})
      .then(function(r) {{ return r.json().then(function(j) {{ return {{ok:r.ok, j:j}}; }}); }})
      .then(function(res) {{
        if (!res.ok || !res.j.ok) {{
          authStat.textContent = '✗ ' + (res.j.error || 'Could not start sign-in.');
          authStat.className = 'status-line err';
          authBtn.disabled = false;
          return;
        }}
        poll();
      }})
      .catch(function(err) {{
        authStat.textContent = '✗ ' + err;
        authStat.className = 'status-line err';
        authBtn.disabled = false;
      }});
  }});

  function poll() {{
    fetch('/setup/google/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(j) {{
        if (j.status === 'pending') {{
          authStat.textContent = 'Waiting for you to finish signing in on google.com…';
          authStat.className = 'status-line';
          setTimeout(poll, 1500);
        }} else if (j.status === 'success') {{
          authStat.textContent = '✓ Done. Google Calendar is connected. You can close this tab.';
          authStat.className = 'status-line ok';
        }} else if (j.status === 'error') {{
          authStat.textContent = '✗ ' + j.detail;
          authStat.className = 'status-line err';
          authBtn.disabled = false;
        }} else {{
          authStat.textContent = '';
        }}
      }})
      .catch(function(err) {{
        setTimeout(poll, 2000);
      }});
  }}
}})();
</script>
</body></html>"""


# ---------- Microsoft 365 sub-wizard ----------

def _render_microsoft_wizard(*, status: str, client_id: str, tenant: str,
                             write_calendar_name: str) -> str:
    if status == "connected":
        banner = (
            '<div class="notice-ok">'
            '<strong>✓ Microsoft 365 is connected.</strong> '
            'You can close this tab and go back to the setup form.'
            '</div>'
        )
    elif status == "client_id_only":
        banner = (
            '<div class="notice-ok" style="background: color-mix(in oklch, var(--accent) 12%, var(--canvas-2)); '
            'border-color: color-mix(in oklch, var(--accent) 40%, transparent);">'
            '<strong>Client ID saved.</strong> Now click '
            '<em>Start Microsoft sign-in</em> in Step 7 below to finish.'
            '</div>'
        )
    else:
        banner = ''

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPlan · connect Microsoft 365</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap-wide">
  <span class="brand"><span class="dot"></span>AutoPlan</span>
  <h1>Connect Microsoft 365</h1>
  <p class="lede">
    Follow the 7 steps below. Steps 1-5 are on Microsoft's Azure portal,
    then you'll come back here for steps 6-7. This covers Outlook Calendar,
    Outlook Mail scanning, <em>and</em> Microsoft To Do — one sign-in, three
    providers. Takes about 10 minutes the first time and never again.
  </p>

  {banner}

  <div class="wizard-step">
    <div class="step-num">1</div>
    <h3>Open the Azure portal</h3>
    <p>
      Visit <a href="https://portal.azure.com" target="_blank">portal.azure.com</a>
      and sign in with the Microsoft account whose calendar, mail, and
      To Do lists AutoPlan should read/write.
    </p>
    <p style="color: var(--ink-3); font-size: 13px">
      Personal @outlook.com / @hotmail.com accounts work fine. Work or
      school accounts also work but may require your IT admin's approval
      depending on your organization's policy.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">2</div>
    <h3>Go to App registrations</h3>
    <p>
      In the top search bar, type <strong>App registrations</strong> and
      click the first result.
    </p>
    <p>
      Click the blue <strong>+ New registration</strong> button.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">3</div>
    <h3>Fill in the registration form</h3>
    <ul>
      <li>Name: <code>AutoPlan</code></li>
      <li>Supported account types: <strong>Accounts in any organizational
        directory and personal Microsoft accounts</strong> (the broadest
        option). Pick this unless your admin says otherwise.</li>
      <li>Redirect URI: pick <strong>Public client/native (mobile &amp; desktop)</strong>
        from the dropdown and enter
        <code>http://localhost:8766</code>.</li>
    </ul>
    <p>Click <strong>Register</strong>.</p>
  </div>

  <div class="wizard-step">
    <div class="step-num">4</div>
    <h3>Copy the Client ID</h3>
    <p>
      On the app's Overview page, look for <strong>Application (client) ID</strong>
      — it's a long GUID like <code>11111111-2222-3333-4444-555555555555</code>.
      Copy it; you'll paste it in Step 6.
    </p>
    <p>
      Also note the <strong>Directory (tenant) ID</strong>. For personal
      Microsoft accounts, leave it as the default
      <code>common</code> in Step 6. For a work/school account where your
      admin requires a specific tenant, paste the GUID instead.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">5</div>
    <h3>Add API permissions</h3>
    <p>
      In the app's left sidebar, click <strong>API permissions</strong> →
      <strong>+ Add a permission</strong> → <strong>Microsoft Graph</strong>
      → <strong>Delegated permissions</strong>.
    </p>
    <p>Check these four:</p>
    <ul>
      <li><code>Calendars.ReadWrite</code></li>
      <li><code>Mail.Read</code></li>
      <li><code>Tasks.ReadWrite</code></li>
      <li><code>User.Read</code></li>
    </ul>
    <p>Click <strong>Add permissions</strong>.</p>
    <p style="color: var(--ink-3); font-size: 13px">
      "offline_access" is added automatically on first sign-in; you
      don't need to check it explicitly.
    </p>
  </div>

  <div class="wizard-step">
    <div class="step-num">6</div>
    <h3>Paste the Client ID here</h3>
    <div class="inputs" style="flex-direction: column; align-items: stretch; gap: 12px">
      <div class="field" style="margin: 0">
        <label>Application (client) ID</label>
        <input type="text" id="clientIdField" value="{client_id}"
               placeholder="11111111-2222-3333-4444-555555555555">
      </div>
      <div class="field" style="margin: 0">
        <label>Tenant
          <span class="hint">leave as "common" for personal accounts</span>
        </label>
        <input type="text" id="tenantField" value="{tenant}" placeholder="common">
      </div>
      <div>
        <button type="button" id="saveBtn" class="secondary">Save</button>
      </div>
    </div>
    <div id="saveStatus" class="status-line"></div>
  </div>

  <div class="wizard-step">
    <div class="step-num">7</div>
    <h3>Sign in to Microsoft</h3>
    <p>
      Click the button below. A new browser tab will open for Microsoft
      sign-in. Sign in with the same account you used in Step 1. Accept
      the permissions prompt (it will list the four scopes from Step 5).
    </p>
    <p style="color: var(--ink-3); font-size: 13px">
      If your organization shows an admin-consent screen, you may need an
      IT admin to approve the app once. That's a Microsoft policy setting,
      not an AutoPlan limitation.
    </p>
    <p style="color: var(--ink-3); font-size: 13px">
      Make sure to create a <strong>{write_calendar_name}</strong> calendar
      at <a href="https://outlook.office.com/calendar" target="_blank">outlook.office.com/calendar</a>
      if you haven't already (use <em>Add calendar → Create blank calendar</em>).
    </p>
    <div class="inputs">
      <button type="button" id="authorizeBtn" class="primary" disabled>Start Microsoft sign-in</button>
    </div>
    <div id="authStatus" class="status-line"></div>
  </div>

  <p style="margin-top: 32px; color: var(--ink-3); font-size: 13px">
    All set? Close this tab and go back to the main
    <a href="/setup">setup page</a>.
  </p>
</div>
<script>
(function() {{
  var saveBtn    = document.getElementById('saveBtn');
  var saveStat   = document.getElementById('saveStatus');
  var authBtn    = document.getElementById('authorizeBtn');
  var authStat   = document.getElementById('authStatus');
  var idField    = document.getElementById('clientIdField');
  var tenantField= document.getElementById('tenantField');
  var initialStatus = {repr(status)!s};

  if (initialStatus === 'connected' || initialStatus === 'client_id_only') {{
    authBtn.disabled = false;
    if (initialStatus === 'connected') {{
      authStat.textContent = '✓ Microsoft 365 already connected. You can redo the sign-in from here if you need to switch accounts.';
      authStat.className = 'status-line ok';
    }}
    if (initialStatus === 'client_id_only') {{
      saveStat.textContent = '✓ Client ID already saved. Skip to Step 7.';
      saveStat.className = 'status-line ok';
    }}
  }}

  saveBtn.addEventListener('click', function() {{
    var cid = (idField.value || '').trim();
    if (!cid) {{
      saveStat.textContent = 'Client ID is required.';
      saveStat.className = 'status-line err';
      return;
    }}
    var fd = new FormData();
    fd.append('client_id', cid);
    fd.append('tenant', (tenantField.value || '').trim() || 'common');
    saveStat.textContent = 'Saving…';
    saveStat.className = 'status-line';
    fetch('/setup/microsoft/save', {{method: 'POST', body: fd}})
      .then(function(r) {{ return r.json().then(function(j) {{ return {{ok:r.ok, j:j}}; }}); }})
      .then(function(res) {{
        if (res.ok && res.j.ok) {{
          saveStat.textContent = '✓ Saved. On to Step 7.';
          saveStat.className = 'status-line ok';
          authBtn.disabled = false;
        }} else {{
          saveStat.textContent = '✗ ' + (res.j.error || 'Save failed.');
          saveStat.className = 'status-line err';
        }}
      }})
      .catch(function(err) {{
        saveStat.textContent = '✗ ' + err;
        saveStat.className = 'status-line err';
      }});
  }});

  authBtn.addEventListener('click', function() {{
    authBtn.disabled = true;
    authStat.textContent = 'Opening Microsoft sign-in in a new tab…';
    authStat.className = 'status-line';
    fetch('/setup/microsoft/authorize', {{method: 'POST'}})
      .then(function(r) {{ return r.json().then(function(j) {{ return {{ok:r.ok, j:j}}; }}); }})
      .then(function(res) {{
        if (!res.ok || !res.j.ok) {{
          authStat.textContent = '✗ ' + (res.j.error || 'Could not start sign-in.');
          authStat.className = 'status-line err';
          authBtn.disabled = false;
          return;
        }}
        poll();
      }})
      .catch(function(err) {{
        authStat.textContent = '✗ ' + err;
        authStat.className = 'status-line err';
        authBtn.disabled = false;
      }});
  }});

  function poll() {{
    fetch('/setup/microsoft/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(j) {{
        if (j.status === 'pending') {{
          authStat.textContent = 'Waiting for you to finish signing in on login.microsoftonline.com…';
          authStat.className = 'status-line';
          setTimeout(poll, 1500);
        }} else if (j.status === 'success') {{
          authStat.textContent = '✓ Done. Microsoft 365 is connected. You can close this tab.';
          authStat.className = 'status-line ok';
        }} else if (j.status === 'error') {{
          authStat.textContent = '✗ ' + j.detail;
          authStat.className = 'status-line err';
          authBtn.disabled = false;
        }} else {{
          authStat.textContent = '';
        }}
      }})
      .catch(function() {{ setTimeout(poll, 2000); }});
  }}
}})();
</script>
</body></html>"""


# ---------- Success / error ----------

def _render_success(token: str, bootstrap_results: list | None = None) -> str:
    bootstrap_html = _render_bootstrap_notes(bootstrap_results or [])
    hub_url = f"http://127.0.0.1:8787/hub?key={token}"
    # Page polls the orchestrator and auto-redirects once it's ready.
    # The orchestrator is being spawned by BackgroundTasks in the
    # setup_submit handler — gives us a seamless first-run experience.
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>setup · done</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
<style>
  .spinner {{
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid color-mix(in oklch, var(--accent) 30%, transparent);
    border-top-color: var(--accent); border-radius: 50%;
    animation: spin 0.8s linear infinite; vertical-align: middle;
    margin-right: 8px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  #manual-fallback {{ display: none; }}
</style>
</head>
<body>
<div class="wrap">
  <span class="brand"><span class="dot"></span>AutoPlan</span>
  <h1>You're in.</h1>
  <p class="lede">
    <code>.env</code> is written, your Anthropic agent is created, and
    AutoPlan is booting up.
  </p>
  <div class="done-card">
    <h3 id="status-label"><span class="spinner"></span>Starting your hub…</h3>
    <p style="color: var(--ink-2); font-size: 14px;">
      Should take about 3 seconds. We'll send you to your hub automatically
      the moment it's ready.
    </p>
    <p style="margin-top: 14px; font-size: 13px; color: var(--ink-3);">
      <strong>Bookmark this URL for tomorrow:</strong><br>
      <code style="font-size: 13px;">{hub_url}</code>
    </p>
  </div>

  <div id="manual-fallback" class="done-card" style="margin-top: 16px;">
    <h3>Still starting up…</h3>
    <p style="color: var(--ink-2); font-size: 14px;">
      Your hub is taking longer than expected to come online. Click the
      link below to jump to it manually, or if that gives a "Not Found"
      error, relaunch the AutoPlan app from your Applications folder.
    </p>
    <p style="margin-top: 12px;">
      <a href="{hub_url}" class="btn btn-primary" style="display: inline-block; padding: 10px 18px;">
        Open hub manually →
      </a>
    </p>
  </div>
  {bootstrap_html}
</div>
<script>
(function() {{
  var hubUrl = {hub_url!r};
  var statusLabel = document.getElementById('status-label');
  var fallback = document.getElementById('manual-fallback');
  var attempts = 0;
  var maxAttempts = 20;  // ~20s total — generous

  function poll() {{
    attempts++;
    // HEAD request so we don't accidentally burn server cycles rendering the hub.
    fetch(hubUrl, {{method: 'HEAD', credentials: 'include', cache: 'no-store'}})
      .then(function(r) {{
        if (r.ok) {{
          statusLabel.textContent = 'Ready — opening your hub.';
          window.location.replace(hubUrl);
          return;
        }}
        retry();
      }})
      .catch(function() {{ retry(); }});
  }}

  function retry() {{
    if (attempts >= maxAttempts) {{
      statusLabel.innerHTML = 'Your hub is taking longer than usual.';
      fallback.style.display = '';
      return;
    }}
    setTimeout(poll, 1000);
  }}

  // Wait a beat for setup_server to die + orchestrator to come up,
  // then start polling.
  setTimeout(poll, 1500);
}})();
</script>
</body></html>"""


def _render_bootstrap_notes(results: list) -> str:
    warnings = [r for r in results if r.get("status") == "failed"]
    if not warnings:
        return ""
    items = "".join(
        f'<li><strong>{r.get("step","")}:</strong> {r.get("detail","")}</li>'
        for r in warnings
    )
    return f"""
  <div class="notice-error" style="margin-top:24px">
    <p style="margin-bottom:10px"><strong>One thing needs your attention:</strong></p>
    <ul style="list-style:disc; padding-left:20px; line-height:1.6;">{items}</ul>
    <p style="margin-top:10px; color: var(--ink-2); font-size: 14px;">
      The rest of setup is fine. Fix the item above (usually: install Xcode
      Command Line Tools with <code>xcode-select --install</code>) and
      relaunch the app.
    </p>
  </div>"""


def _render_error(msg: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>setup · error</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <span class="brand"><span class="dot"></span>AutoPlan</span>
  <h1>Setup didn't finish.</h1>
  <p class="lede">Your entries are still in the form — go back and adjust
    the field the message points at.</p>
  <div class="notice-error">{msg}</div>
  <p><a href="/setup">← Back to the form</a></p>
</div>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    print("Setup server on http://127.0.0.1:8787/setup")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
