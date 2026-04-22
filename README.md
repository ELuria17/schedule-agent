# schedule-agent-public

Motion-style constraint-solving personal scheduler. Reads tasks from an LMS and/or
a to-do app, places them on a calendar around your fixed commitments (classes,
meetings, working hours), re-solves on every change, and pings you via a chat
channel.

This repo is a **public-ready starting point** forked from a working personal
deployment. The code runs end-to-end for the original author (PSU student on macOS
with iCloud + Apple Reminders + Canvas + iMessage), but needs abstraction work
before it's usable by others with different stacks.

---

## Current state: what works

The code in this folder is the working end of a two-month iteration. See
`ROADBLOCKS.md` for the full list of bugs fixed along the way. Short version:

- **Deterministic constraint solver** (`solver.py`) — greedy placement with
  chunk splitting, deadline awareness, priority scoring, overdue fallback.
- **Calendar integration** (`orchestrator.py`) — CalDAV to iCloud; creates/clears
  AUTO-tagged events; respects manually-placed user events.
- **Task store** (`tasks.py`, `config.py`) — SQLite at `state.db`;
  schema for tasks, history, scheduled chunks, config blocks, working hours,
  reminder-match handshakes.
- **Canvas ingestion** — pulls assignments, trims to relevant window, auto-marks
  submitted ones done, reopens if Canvas contradicts Reminders.
- **Apple Reminders cross-reference** — native Swift binary for EventKit access
  (via CalDAV is unreliable for modern reminders).
- **Anthropic Managed Agent layer** — natural-language intake, daily summary via
  iMessage, tool-mediated task CRUD. LLM never touches the calendar directly.
- **Browser hub** (`hub.html`) — task list, manual add/complete, solver activity
  log, session live stream, agent trigger buttons. Vanilla JS, no build step.
- **Stable remote access** via Tailscale (private mesh).

---

## What needs to change before publishing

This is the shape of the work, not a committed plan. Every bullet is an
opportunity to break the current monolith into pluggable pieces.

### 1. Provider abstractions ✅ done

The `providers/` package now exposes four ABCs. Orchestrator + solver + agent
code never touches a backend-specific type — each category has an interface
and one macOS-flavored reference implementation. Adding a new backend =
writing one file that satisfies the ABC.

| Category | ABC (`providers/...`) | Reference impl | To add (examples) |
|---|---|---|---|
| Calendar | `base.CalendarProvider` + `CalendarEvent` | `icloud_caldav.ICloudCalDAVProvider` | Google Calendar, Microsoft Graph, generic CalDAV |
| Task source | `task_source.TaskSource` + `SourceTask` | `canvas_task_source.CanvasTaskSource` (any Canvas domain) | Brightspace, Moodle, Notion DB, Linear, GitHub issues |
| Todo source | `todo_source.TodoSource` + `TodoItem` | `apple_reminders.AppleRemindersTodoSource` (macOS only) | Google Tasks, Todoist, TickTick, MS To Do |
| Notifier | `notifier.Notifier` | `imessage_notifier.IMessageNotifier` (macOS only) | Pushover, ntfy.sh, email SMTP, Slack, "hub-only" |

Provider wiring lives in **`schedule_config.py`** — the single file end users
edit when picking backends. The orchestrator imports by name; it never
constructs providers itself.

```python
# schedule_config.py (abridged)
CALENDAR = ICloudCalDAVProvider(...)
TASK_SOURCES: list[TaskSource] = [CanvasTaskSource(...)]
TODO_SOURCE = AppleRemindersTodoSource(...)
NOTIFIER = IMessageNotifier(...)     # or NtfyNotifier(...)  for cross-platform push
```

Available Notifiers (cross-platform story is live):
- `IMessageNotifier` — macOS only, self-send doesn't push-notify.
- `NtfyNotifier` — free, cross-platform, real push via ntfy.sh. Works on
  Windows/Linux users who can't use iMessage.

A `providers_health()` helper in `schedule_config.py` is exposed at
`GET /api/health` — useful for the hub, and for any future test agent.

**Per-source approval gate.** Every `TaskSource` has a `require_approval`
flag (default `False`). Flip it on to hold new tasks from that source in a
`pending_review` queue instead of sending them straight to the solver:

```python
CanvasTaskSource(
    base_url=..., token=...,
    require_approval=True,   # nothing reaches your calendar until you say so
)
```

While a task sits in `pending_review`, the solver ignores it. The hub
renders a "Pending review" section at the top with per-task Approve / Reject
buttons plus an "Approve all" shortcut; the same actions are available via
`GET /api/tasks/pending`, `POST /api/tasks/{id}/approve`, and
`POST /api/tasks/{id}/reject`. Rejection moves the task to `hidden` so it
stops cluttering active views but still anchors the `(source, source_id)`
upsert key — the next sync won't re-create it.

### 2. Config-driven setup ✅ done (Python-module form)

All user-editable values live in a single file, `schedule_config.py`:

- `TIMEZONE` — IANA zone name (also respects a `TIMEZONE` env override)
- `WORKING_HOURS` — `{weekday: (start, end)}` in local time
- `CLASS_BLOCKS` — recurring commitments (classes, standing meetings, sleep, Shabbat) as `(kind, label, rrule, start, end)` tuples; seeded into SQLite on first boot
- `TODO_LIST_TO_COURSE` — bridge between todo-app list names and task-source `course` codes (empty by default — the matcher falls back to substring comparison)
- `CALENDAR`, `TASK_SOURCES`, `TODO_SOURCE`, `NOTIFIER` — provider instances

New installs start with empty class blocks and an empty todo→course map; users
fill them in by editing the file. A later `config.yaml` layer on top of this
(for users who prefer non-code config) is a separate, optional step.

### 3. Platform support ✅ done

The orchestrator, solver, tasks layer, and HTTP-only providers (Canvas, ntfy)
are pure Python and run anywhere Python 3.9+ does. Platform-specific files
live in well-named folders:

- `macos/` — AppleScript, Swift/EventKit bridge, launchd wrapper + plist
  template. See `macos/README.md`.
- `deploy/systemd/` — Linux unit template + README for bare-metal /
  VPS deploys. See `deploy/systemd/README.md`.
- `Dockerfile`, `.dockerignore`, `docker-compose.yml` — containerized
  deploys. Mount `.env` + `state.db` as volumes; the image itself carries
  no secrets or state.

On Linux/Windows pick `NtfyNotifier` (cross-platform push) in the wizard
and set `TODO_SOURCE = None` (or a cloud-API TodoSource) — nothing in
`macos/` runs. iCloud CalDAV still works fine since it's pure HTTP.

### 4. Install + first-run UX ✅ done (wizards), 🟡 GUI packaging scaffolded

Two wizards ship, same flow, different surfaces:

- `python install.py` — terminal wizard; prompts, masked secrets,
  re-runnable with existing `.env` values as defaults.
- `python run.py` — the unified entry point. Checks `.env`; if
  unconfigured, boots `setup_server.py` and opens
  `http://127.0.0.1:8787/setup` in the browser; otherwise launches the
  orchestrator directly. This is what a packaged app invokes.

Both write `.env`, call `setup.py` to create the Anthropic environment +
agent, and can be re-run to edit settings.

The GUI packaging path is scaffolded in `packaging/macos/` — PyInstaller
spec + `build.sh` + full README covering the unsigned-build workflow and
the later signing / notarization recipe. Output is a `.app` and optional
`.dmg`. First launch of the `.app` opens the browser wizard the same way
`run.py` does. This build still needs to actually be produced on a Mac
(pip install pyinstaller + `bash packaging/macos/build.sh`) and
distributed; the spec and recipe are the scaffolding.

Still open: sign the build (~$99/year to Apple Developer Program), move
`.env` / `state.db` to `~/Library/Application Support/schedule-agent/`
so bundle replacement doesn't wipe user state, and auto-generate a
LaunchAgent plist / systemd unit at the end of the wizard.

### 5. Anthropic agent layer ✅ done

System prompt in `setup.py` is now provider-neutral — no references to a
specific user, school, LMS, or messaging channel. It talks about "configured
task sources" and "the Notifier" rather than Canvas + iMessage. Duration
baselines still lean toward schoolwork but are framed as starting points that
get refined from `task_history`.

### 6. Multi-user + persistence (in progress)

**Session + solver-log persistence ✅ done.** `history.py` mirrors every
solver run + every session event into SQLite (`solver_log`, `sessions`,
`session_events` tables). `GET /api/solver/log`, `GET /api/sessions`, and
`GET /api/sessions/{id}/events` read from the DB with a live-memory merge
for in-flight sessions. Orchestrator restarts (launchd respawn, `docker
compose up -d`, etc.) no longer wipe the hub's activity lists. Writes are
best-effort — a history failure logs to stderr but does not crash the
live agent run.

**Still open — per-user auth.** Single shared `REPLAN_TOKEN` remains the
only gate. For a multi-tenant deployment, swap it for per-user
authentication (magic link, Google Sign-In, passkey) plus a `users` table
keyed off the session/task rows.

### 7. Test suite + CI (in progress)

Every "Test signal" line in `ROADBLOCKS.md` is a candidate for a pytest case.
`tests/` currently covers the solver core, the data-access layer, history
persistence, all three shipped providers, and the task-approval gate —
121 tests total:

- `tests/test_solver_pure.py` — 21 unit tests over `_subtract`,
  `_byday_matches`, `priority_score`, `_split_task_across_slots`, `place`.
  No DB, no external services.
- `tests/test_solver_resolve.py` — 12 integration tests over `free_slots`
  and `resolve` using a throwaway SQLite DB. Covers ROADBLOCKS signals
  S2 (overdue placement), S3 (no sub-min chunks), S4 (no class-block overlap).
- `tests/test_tasks.py` — 14 tests over `tasks.py`. Covers F4 (NULLS-last
  ordering), D2 (source-authoritative reopen), history-driven
  `suggest_duration`, and `mark_done_by_source` semantics.
- `tests/test_canvas_provider.py` — 12 tests over `CanvasTaskSource` with
  `requests.get` monkeypatched. Locks in C1 (response trim + window),
  C2 (submitted → completed), C3 (course-code extraction), and the
  per-source `require_approval` propagation.
- `tests/test_ntfy_provider.py` — 10 tests over `NtfyNotifier`. Covers
  request shape, priority/tags/auth headers, self-hosted base URL,
  error paths, and topic-redacting `health_check`.
- `tests/test_approval_flow.py` — 13 tests over the pending-review gate:
  upsert routing, solver-ignores-pending, approve/reject transitions,
  and the full "pending → approved → solver places" round trip.
- `tests/test_history.py` — 15 tests over `history.py`: solver-log
  ordering + trim, session upsert / rename / event ordering, ON DELETE
  CASCADE on child events, graceful log-and-continue on bad writes.
- `tests/test_caldav_provider.py` — 24 tests over `ICloudCalDAVProvider`
  with `caldav.DAVClient` + `requests.delete` monkeypatched at the module
  boundary. Covers the full contract: list/create/delete, AUTO-marker
  round-trip, UID-fallback delete, broken-calendar tolerance, and
  `reset()` cache invalidation (ROADBLOCKS §I2, §I3, §I5).

CI: `.github/workflows/test.yml` runs `pytest` on Python 3.9 and 3.12 on every
push and pull request.

Still to do: per-provider integration smoke tests gated on real credentials
(Canvas / iCloud / Anthropic) — skipped by default, opt-in via env.

Run locally:

```bash
pip install -r requirements-dev.txt
pytest
```

### 8. Docs + LICENSE ✅ done

- [`LICENSE`](LICENSE) — MIT.
- [`docs/getting-started.md`](docs/getting-started.md) — first-run
  walkthrough paired with the install.py wizard.
- [`docs/providers.md`](docs/providers.md) — the four ABCs with worked
  examples (Google Calendar sketch, Todoist sketch) and test pattern.
- [`docs/deployment.md`](docs/deployment.md) — launchd / systemd / Docker
  with a picker table.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — symptom-indexed,
  curated from `ROADBLOCKS.md`.

---

## Repo layout (current — will evolve)

```
schedule-agent-public/
├── providers/                         # pluggable integrations (platform-neutral)
│   ├── base.py                        # CalendarProvider + CalendarEvent
│   ├── icloud_caldav.py               #   → iCloud via CalDAV
│   ├── task_source.py                 # TaskSource + SourceTask
│   ├── canvas_task_source.py          #   → any Canvas instance
│   ├── todo_source.py                 # TodoSource + TodoItem
│   ├── apple_reminders.py             #   → Apple Reminders (macOS; needs macos/reminders_fetch)
│   ├── notifier.py                    # Notifier
│   ├── imessage_notifier.py           #   → iMessage (macOS; needs macos/send_imessage.applescript)
│   └── ntfy_notifier.py               #   → ntfy.sh (cross-platform)
├── macos/                             # macOS-only runtime bits; Linux/Windows installs ignore
│   ├── README.md                      # TCC grants, launchd plist template, compile notes
│   ├── reminders_fetch.swift          # EventKit bridge (compile with swiftc -O)
│   ├── send_imessage.applescript      # used by IMessageNotifier
│   └── launch.sh                      # launchd entrypoint (relative-path version)
├── deploy/systemd/                    # Linux service template + walkthrough
│   ├── README.md
│   └── schedule-agent.service
├── Dockerfile, .dockerignore          # container deploys
├── docker-compose.yml                 #   ↳ with .env + state.db volumes
├── schedule_config.py                 # USER-FACING: pick backends here
├── orchestrator.py                    # FastAPI + solver glue; imports from schedule_config
├── solver.py                          # deterministic constraint solver
├── tasks.py                           # SQLite data access for tasks
├── history.py                         # SQLite-backed solver log + session transcripts
├── config.py                          # schema + seed data
├── paths.py                           # data_dir resolution (dev / frozen / SCHEDULE_AGENT_DATA_DIR)
├── bootstrap.py                       # first-launch side effects (swiftc compile, etc.)
├── run.py                             # unified entry point (picks wizard vs. orchestrator)
├── setup_server.py                    # browser-based first-run wizard (served at /setup)
├── install.py                         # terminal first-run wizard (same flow, no browser)
├── setup.py                           # one-time: create Anthropic env + agent
├── hub.html                           # single-file browser UI
├── site/                              # static landing page (GitHub Pages-ready)
├── packaging/macos/                   # PyInstaller spec + build.sh for .app/.dmg bundle
├── .impeccable.md                     # design context (palette, type, motion, anti-refs)
├── docs/                              # long-form docs (getting-started, providers, deployment, troubleshooting)
├── tests/                             # pytest suite — solver, tasks, history, Canvas, ntfy, approval
├── requirements.txt
├── requirements-dev.txt               # adds pytest for running the suite
├── ROADBLOCKS.md                      # every issue we hit + fix + test-agent hints
├── .env.example
├── .gitignore
└── README.md
```

---

## Running the current code as-is

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python run.py                 # launches the setup wizard in your browser
                              # (or the hub, if .env is already configured)
```

`run.py` is the single entry point. On a fresh checkout it opens the
browser-based setup wizard at `http://127.0.0.1:8787/setup` — pick
providers, enter credentials, hit submit, and it writes `.env` and
creates your Anthropic agent. On subsequent launches it boots the full
orchestrator and opens the hub at `http://127.0.0.1:8787/hub?key=<token>`.

For a terminal-only install (no browser wizard), `python install.py` still
works — same fields, interactive prompts instead of a form. For more
detail on individual steps see `ROADBLOCKS.md` (entries E1–E3, I1,
R1–R3, T1–T2).

For lifetime service on macOS: see `macos/README.md` for the LaunchAgent
plist template. Point it at `macos/launch.sh` (which uses relative paths, so
the repo can live anywhere).

On Linux/Windows: the wizard defaults to ntfy.sh (cross-platform push) and
no todo source. Calendar remains iCloud CalDAV by default — swap to a
Google Calendar / Outlook / generic CalDAV provider when those land (see
the `providers/base.py` ABC for what a new impl looks like).

## Landing page

Static site in [`site/`](site/) — single-page, GitHub Pages-ready. Same
warm-tinted dark palette as the hub + setup wizard (design context
pinned in [`.impeccable.md`](.impeccable.md)). Replace the
`YOUR-HANDLE` placeholders in `site/index.html` once the repo has a
canonical URL. Preview locally:

```bash
python -m http.server -d site 8000
# open http://127.0.0.1:8000/
```

## Docs

Long-form docs live under [`docs/`](docs/):

- [Getting started](docs/getting-started.md) — first-run walkthrough.
- [Writing a new provider](docs/providers.md) — adding a backend.
- [Deployment](docs/deployment.md) — launchd / systemd / Docker.
- [Troubleshooting](docs/troubleshooting.md) — symptom-indexed.

`ROADBLOCKS.md` is the authoritative build log — every bug + fix + test
signal from the two-month iteration. The troubleshooting doc is a curated
subset for end users.

---

## Headless deploys

Two supported paths for running without a macOS desktop:

**Docker / docker-compose** (easiest):

```bash
python install.py             # writes .env (pick ntfy + None for todo)
docker compose up -d --build  # builds image, mounts .env + state.db
```

See `docker-compose.yml` for volume layout. Swap your macOS-specific
`schedule_config.py` with a portable one (`NtfyNotifier`, no todo source)
before building — the macOS providers won't import in a Linux container.

**systemd on a Linux host** (for VPS / bare-metal):

See `deploy/systemd/README.md` for the full walkthrough. Short version:

```bash
venv/bin/python install.py
sed -e 's|<USER>|schedule|g' -e 's|<REPO>|/srv/schedule-agent-public|g' \
    deploy/systemd/schedule-agent.service \
    > /etc/systemd/system/schedule-agent.service
systemctl enable --now schedule-agent
```

Put either path behind a reverse proxy (nginx, Caddy, Tailscale serve)
with TLS — `REPLAN_TOKEN` is the app-level gate but HTTP isn't.

---

## License

[MIT](LICENSE). Do what you want, keep the copyright notice, no warranty.

---

## Credits

Built by iterating with Claude across many sessions. The `ROADBLOCKS.md` file is
essentially a build log of that iteration — useful as context for contributors.
