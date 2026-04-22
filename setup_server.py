"""First-run setup server.

A small FastAPI app that serves the install wizard as HTML forms at
`http://127.0.0.1:8787/setup` — parallel to `install.py`'s terminal flow,
but GUI-friendly for packaged distributions.

The launcher (`run.py`) picks between this and `orchestrator.py` based on
whether the user has finished setup:

    configured = (.env exists AND has ENVIRONMENT_ID and AGENT_ID set)

- not configured → run setup_server.py, user points a browser at
  http://127.0.0.1:8787/ and fills in provider credentials through the
  wizard. On submit, we write .env + call Anthropic to create the agent,
  then show a success page that says "close this window and relaunch the
  app."
- configured → run orchestrator.py as usual.

The setup server is intentionally standalone — it does NOT import
`schedule_config.py` (which would force every env var to already be set).
It only imports `install.py`'s helpers (_read_env / _write_env / _quote /
_KEYS) plus the Anthropic SDK for the agent-creation call.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import install as install_mod

app = FastAPI()
PROJECT_DIR = Path(__file__).resolve().parent


def _env_snapshot() -> dict[str, str]:
    return install_mod._read_env()


def _is_configured(env: dict[str, str]) -> bool:
    return bool(env.get("ENVIRONMENT_ID") and env.get("AGENT_ID"))


@app.get("/", response_class=HTMLResponse)
@app.get("/setup", response_class=HTMLResponse)
async def setup_form():
    env = _env_snapshot()
    # Pre-fill with any existing values; generate tokens/topics if missing.
    defaults = {
        "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", ""),
        "REPLAN_TOKEN": env.get("REPLAN_TOKEN") or secrets.token_urlsafe(32),
        "TIMEZONE": env.get("TIMEZONE") or "America/New_York",
        "ICLOUD_USER": env.get("ICLOUD_USER", ""),
        "ICLOUD_APP_PASSWORD": env.get("ICLOUD_APP_PASSWORD", ""),
        "WRITE_CALENDAR_NAME": env.get("WRITE_CALENDAR_NAME") or "Study Blocks",
        "CANVAS_BASE_URL": env.get("CANVAS_BASE_URL") or "https://psu.instructure.com/api/v1",
        "CANVAS_TOKEN": env.get("CANVAS_TOKEN", ""),
        "USER_PHONE": env.get("USER_PHONE", ""),
        "NTFY_TOPIC": env.get("NTFY_TOPIC") or f"schedule-agent-{secrets.token_urlsafe(16)}",
    }
    # Default notifier: ntfy on non-macOS, iMessage on macOS.
    default_notifier = "imessage" if sys.platform == "darwin" else "ntfy"
    current_notifier = "imessage" if env.get("USER_PHONE") else (
        "ntfy" if env.get("NTFY_TOPIC") else default_notifier
    )
    return HTMLResponse(_render_form(defaults, current_notifier))


@app.post("/setup")
async def setup_submit(request: Request):
    form = await request.form()
    data = {k: str(v).strip() for k, v in form.items()}

    # Collect existing .env, overwrite with form values.
    env = _env_snapshot()
    env["ANTHROPIC_API_KEY"] = data.get("anthropic_api_key", "")
    env["REPLAN_TOKEN"] = data.get("replan_token", "") or secrets.token_urlsafe(32)
    env["TIMEZONE"] = data.get("timezone", "") or "America/New_York"

    calendar_choice = data.get("calendar", "icloud")
    if calendar_choice == "icloud":
        env["ICLOUD_USER"] = data.get("icloud_user", "")
        env["ICLOUD_APP_PASSWORD"] = data.get("icloud_app_password", "")
        env["WRITE_CALENDAR_NAME"] = data.get("write_calendar_name", "") or "Study Blocks"

    task_source = data.get("task_source", "canvas")
    if task_source == "canvas":
        env["CANVAS_BASE_URL"] = data.get("canvas_base_url", "") or \
            "https://psu.instructure.com/api/v1"
        env["CANVAS_TOKEN"] = data.get("canvas_token", "")

    notifier = data.get("notifier", "ntfy")
    if notifier == "imessage":
        env["USER_PHONE"] = data.get("user_phone", "")
        env.pop("NTFY_TOPIC", None)
    else:
        env["NTFY_TOPIC"] = data.get("ntfy_topic", "") or \
            f"schedule-agent-{secrets.token_urlsafe(16)}"
        env.pop("USER_PHONE", None)

    # Validate required fields
    missing = []
    if not env["ANTHROPIC_API_KEY"]:
        missing.append("Anthropic API key")
    if calendar_choice == "icloud" and not (env.get("ICLOUD_USER") and env.get("ICLOUD_APP_PASSWORD")):
        missing.append("iCloud email + app-specific password")
    if task_source == "canvas" and not env.get("CANVAS_TOKEN"):
        missing.append("Canvas access token")
    if notifier == "imessage" and not env.get("USER_PHONE"):
        missing.append("phone number")
    if notifier == "ntfy" and not env.get("NTFY_TOPIC"):
        missing.append("ntfy topic")

    if missing:
        return HTMLResponse(_render_error(f"Missing: {', '.join(missing)}."), status_code=400)

    # Write .env.
    install_mod._write_env(env)

    # Create Anthropic environment + agent via setup.py. Done in-process with
    # os.environ hydrated so the existing code path works unchanged.
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

    # First-launch side effects — only useful if the user enabled the
    # Apple Reminders todo source, but idempotent otherwise.
    import bootstrap
    bootstrap_results = bootstrap.run_all()

    # Read .env back — setup.py just appended ENVIRONMENT_ID and AGENT_ID.
    final_env = _env_snapshot()
    token = final_env.get("REPLAN_TOKEN", "")
    return HTMLResponse(_render_success(token, bootstrap_results))


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
  .lede {
    color: var(--ink-2); font-size: 16px; line-height: 1.55;
    max-width: 54ch; margin-bottom: 48px;
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

  input[type=text], input[type=email], input[type=password] {
    width: 100%; font: inherit; color: var(--ink-1);
    background: var(--canvas-2);
    border: 1px solid var(--border-soft); border-radius: 6px;
    padding: 10px 12px;
    transition: border-color 160ms var(--ease-out),
                background 160ms var(--ease-out);
  }
  input::placeholder { color: var(--ink-3); }
  input:hover { border-color: var(--border); }
  input:focus {
    outline: 0; border-color: var(--accent);
    background: color-mix(in oklch, var(--accent-quiet) 8%, var(--canvas-2));
  }

  .radios { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 14px; }
  .radios label {
    display: inline-flex; align-items: center; gap: 8px;
    font-weight: 400; cursor: pointer; color: var(--ink-1);
    padding: 6px 0;
  }
  .radios input[type=radio] {
    appearance: none; width: 16px; height: 16px; border-radius: 999px;
    border: 1.5px solid var(--border); background: transparent;
    cursor: pointer; transition: border-color 160ms var(--ease-out);
    position: relative;
  }
  .radios input[type=radio]:checked {
    border-color: var(--accent);
  }
  .radios input[type=radio]:checked::after {
    content: ""; position: absolute; inset: 3px; border-radius: 999px;
    background: var(--accent);
  }

  .pill {
    font-family: "JetBrains Mono", ui-monospace, Menlo, monospace;
    font-size: 10px; font-weight: 500;
    color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.1em;
    padding: 2px 7px; border: 1px solid var(--border);
    border-radius: 3px; margin-left: 6px;
  }

  button.submit {
    margin-top: 48px; width: 100%;
    background: var(--accent); color: var(--accent-on);
    font: inherit; font-weight: 500; font-size: 15px;
    border: 0; border-radius: 8px; padding: 14px 22px;
    cursor: pointer;
    transition: transform 160ms var(--ease-out),
                filter 200ms var(--ease-out);
  }
  button.submit:hover { filter: brightness(1.06); }
  button.submit:active { transform: scale(0.98); }

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

  /* Success page */
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

  @media (prefers-reduced-motion: reduce) {
    *, button.submit, input { transition: none !important; }
  }
"""


_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Bricolage+Grotesque:opsz,wght@12..96,300..600&'
    'family=JetBrains+Mono:wght@400;500&display=swap">'
)


def _render_form(d: dict, notifier_default: str) -> str:
    imessage_checked = "checked" if notifier_default == "imessage" else ""
    ntfy_checked = "checked" if notifier_default == "ntfy" else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>schedule-agent · setup</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <span class="brand"><span class="dot"></span>schedule-agent</span>

  <h1>Let's get you set up.</h1>
  <p class="lede">
    One pass through this form writes <code>.env</code> and creates your
    Anthropic agent. You can come back and change any of this later.
  </p>

  <form method="POST" action="/setup" autocomplete="off">

    <span class="section-label">Core</span>
    <div class="field">
      <label>Anthropic API key
        <span class="hint">get one at console.anthropic.com/settings/keys</span>
      </label>
      <input type="password" name="anthropic_api_key"
             value="{d['ANTHROPIC_API_KEY']}" required>
    </div>
    <div class="field">
      <label>Timezone</label>
      <input type="text" name="timezone" value="{d['TIMEZONE']}"
             placeholder="America/New_York">
      <p class="note">Any IANA zone name.</p>
    </div>
    <div class="field">
      <label>Hub auth token
        <span class="hint">generated · save the hub URL</span>
      </label>
      <input type="text" name="replan_token" value="{d['REPLAN_TOKEN']}">
    </div>

    <span class="section-label">Calendar</span>
    <div class="radios">
      <label><input type="radio" name="calendar" value="icloud" checked>iCloud</label>
      <label><input type="radio" name="calendar" value="skip">Skip for now</label>
    </div>
    <div class="field">
      <label>Apple ID email
        <span class="hint">primary Apple ID, not a @icloud.com alias</span>
      </label>
      <input type="email" name="icloud_user" value="{d['ICLOUD_USER']}">
    </div>
    <div class="field">
      <label>App-specific password
        <span class="hint">from appleid.apple.com</span>
      </label>
      <input type="password" name="icloud_app_password"
             value="{d['ICLOUD_APP_PASSWORD']}"
             placeholder="xxxx-xxxx-xxxx-xxxx">
    </div>
    <div class="field">
      <label>Write calendar name</label>
      <input type="text" name="write_calendar_name"
             value="{d['WRITE_CALENDAR_NAME']}">
      <p class="note">Create this calendar in Calendar.app first if it doesn't exist yet.</p>
    </div>

    <span class="section-label">Task source</span>
    <div class="radios">
      <label><input type="radio" name="task_source" value="canvas" checked>Canvas LMS</label>
      <label><input type="radio" name="task_source" value="skip">None / manual only</label>
    </div>
    <div class="field">
      <label>Canvas API base URL</label>
      <input type="text" name="canvas_base_url" value="{d['CANVAS_BASE_URL']}">
    </div>
    <div class="field">
      <label>Canvas access token
        <span class="hint">&lt;canvas&gt;/profile/settings → New Access Token</span>
      </label>
      <input type="password" name="canvas_token" value="{d['CANVAS_TOKEN']}">
    </div>

    <span class="section-label">Notifier</span>
    <div class="radios">
      <label>
        <input type="radio" name="notifier" value="imessage" {imessage_checked}>
        iMessage<span class="pill">macOS</span>
      </label>
      <label>
        <input type="radio" name="notifier" value="ntfy" {ntfy_checked}>
        ntfy.sh<span class="pill">any OS</span>
      </label>
    </div>
    <div class="field">
      <label>Phone number
        <span class="hint">E.164 (e.g. +15551234567); iMessage only</span>
      </label>
      <input type="text" name="user_phone" value="{d['USER_PHONE']}"
             placeholder="+15551234567">
    </div>
    <div class="field">
      <label>ntfy topic
        <span class="hint">secret · generated for you</span>
      </label>
      <input type="text" name="ntfy_topic" value="{d['NTFY_TOPIC']}">
      <p class="note">Subscribe the ntfy mobile app to this topic to get pushes on your phone.</p>
    </div>

    <button type="submit" class="submit">Save and create Anthropic agent</button>
  </form>
</div>
</body></html>"""


def _render_success(token: str, bootstrap_results: list | None = None) -> str:
    bootstrap_html = _render_bootstrap_notes(bootstrap_results or [])
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>setup · done</title>
{_FONT_LINK}
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <span class="brand"><span class="dot"></span>schedule-agent</span>
  <h1>You're in.</h1>
  <p class="lede">
    <code>.env</code> is written, your Anthropic environment and agent are
    created, and everything is ready to run.
  </p>
  <div class="done-card">
    <h3>What happens next</h3>
    <ol>
      <li>Close this tab.</li>
      <li>Relaunch the app (or restart the orchestrator process).</li>
      <li>Open <code>http://127.0.0.1:8787/hub?key={token}</code> — that's your hub. Bookmark it.</li>
    </ol>
  </div>
  {bootstrap_html}
</div>
</body></html>"""


def _render_bootstrap_notes(results: list) -> str:
    """Render a short notes block for any non-ok bootstrap step. Skipped
    steps (e.g. non-macOS) are hidden; failed steps become a visible
    warning so the user knows to fix them before the agent runs."""
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
  <span class="brand"><span class="dot"></span>schedule-agent</span>
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
