# Getting started

Bring a fresh checkout of this repo from zero to "the orchestrator is running
and the agent just sent me a notification" in under ten minutes.

## Prereqs

- Python 3.9 or newer. (`from __future__ import annotations` is used
  throughout, so older versions will fail with syntax errors.)
- An Anthropic API key — [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).
- One backend per category you want to enable:

  | Category | What works today | What you'll need |
  |---|---|---|
  | Calendar | iCloud CalDAV | Apple ID primary email + app-specific password |
  | Task source | Canvas LMS | Canvas personal access token |
  | Todo source | Apple Reminders (macOS only) | none; TCC prompt on first run |
  | Notifier | iMessage (macOS) or ntfy.sh | phone number or ntfy topic |

  Swapping any of those for a different backend means writing one provider
  class — see [providers.md](providers.md).

## Install

```bash
git clone <your-fork-url> schedule-agent-public
cd schedule-agent-public
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python install.py
```

The wizard takes you through five screens:

1. **Core settings.** API key, timezone, a freshly generated
   `REPLAN_TOKEN`. The token is the auth gate for the hub — save the URL
   (`/hub?key=<token>`) it prints.
2. **Calendar.** iCloud or skip. iCloud needs your *primary* Apple ID email
   (not a `@icloud.com` alias, if they differ) and an [app-specific
   password](https://appleid.apple.com) — not your real Apple password.
3. **Task source.** Canvas or none. Base URL defaults to
   `https://psu.instructure.com/api/v1`; swap it for your school's
   hostname. Token is generated at `<canvas>/profile/settings → New Access
   Token`.
4. **Todo source.** Apple Reminders or none. Defaults to Reminders on
   macOS, none on other platforms.
5. **Notifier.** iMessage (self-send shows in the Messages badge but does
   *not* push-notify — see `ROADBLOCKS.md §M2`) or ntfy.sh
   (cross-platform, real push, generated topic is a secret).

After the review screen, the wizard writes `.env`, runs `setup.py` to
create your Anthropic environment + agent (populating `ENVIRONMENT_ID` and
`AGENT_ID` in `.env`), offers to compile `macos/reminders_fetch` on
macOS, and prints a `providers_health()` summary.

### What "healthy" looks like

```
── Provider health check ──
  ✓ calendar: require_approval=False, write_calendar=Study Blocks
  ✓ task_sources[canvas]: base_url=https://..., canvas_user=Eytan Luria
  ✓ todo_source: binary=..., exit_code=0, total_reminders_surfaced=843
  ✓ notifier: channel=imessage, recipient=+1...
  timezone: America/New_York
```

A red `✗` next to any line points at the misconfigured credential — fix
the value in `.env` and re-run `install.py`; existing values show as
defaults so you only need to change the broken one.

## First run

```bash
python orchestrator.py
```

The orchestrator listens on `0.0.0.0:8787`. Point a browser at

```
http://127.0.0.1:8787/hub?key=<REPLAN_TOKEN from .env>
```

(Replace `<REPLAN_TOKEN>` with the actual value — `grep ^REPLAN_TOKEN .env`.)

The hub will be mostly empty. On first load it kicks an initial sync + solver
run, which pulls assignments from Canvas, places chunks on free time, and
writes them as AUTO-tagged Study Blocks on your calendar. Give it 10–20
seconds.

### Triggering your first agent session

Click **Run now** next to the schedule agent. You should see:
- A new session appear in *Live* while it runs.
- The agent call tools: `schedule_query`, then `send_sms`.
- A notification on your phone (iMessage badge, or ntfy push, depending on
  your pick).
- After the session ends, the *Live* section collapses and the session
  moves to *Recent sessions* with its transcript.

### Validating persistence

Restart the orchestrator (Ctrl-C, then `python orchestrator.py` again).
Refresh the hub. **Solver activity** and **Recent sessions** should still
show your prior runs — those are backed by SQLite
([`history.py`](../history.py)), not in-memory state.

## Where to go next

- [providers.md](providers.md) — add a new backend (Google Calendar,
  Todoist, Pushover, …).
- [deployment.md](deployment.md) — make it a long-lived service
  (launchd, systemd, Docker).
- [troubleshooting.md](troubleshooting.md) — if something doesn't work.
- `ROADBLOCKS.md` at the repo root — the full build log with every bug
  we hit and its root cause.
