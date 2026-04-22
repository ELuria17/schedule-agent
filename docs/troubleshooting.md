# Troubleshooting

Organized by symptom. Each entry points at the underlying root cause in
`ROADBLOCKS.md` — that file is the authoritative build log and goes deeper.

If your symptom isn't listed, skim `ROADBLOCKS.md` by category heading —
the build log entries are grouped by subsystem (macOS env, iCloud CalDAV,
Apple Reminders, Canvas, Anthropic SDK, notification path, tunneling,
solver, task intake, lifecycle, FastAPI).

---

## Install / startup

### `python install.py` says "KeyError: 'ICLOUD_USER'"

You skipped the calendar step in the wizard but `schedule_config.py`'s
default `ICloudCalDAVProvider(...)` still requires `ICLOUD_USER` at
import time. Either fill in an iCloud value in the wizard, or edit
`schedule_config.py` to use a different `CalendarProvider` (and delete the
iCloud-specific imports).

### `python install.py` hangs at "Create Anthropic environment + agent"

Behind the scenes we're calling `client.beta.environments.create` +
`client.beta.agents.create`. Both require network access to the Anthropic
API. If you're behind a proxy or your API key is wrong, the hang is an
httpx timeout. Check `ANTHROPIC_API_KEY` is valid and you have connectivity
to `api.anthropic.com`.

### `orchestrator.py` exits immediately with `ModuleNotFoundError: caldav`

The `caldav` library is a `requirements.txt` dep. You ran the orchestrator
outside the virtualenv, or the venv was never activated. `source venv/bin/activate`
first. If using systemd, the unit points at `<REPO>/venv/bin/python` to
avoid this.

### Python version too old — `TypeError: unsupported operand type(s) for |`

You're on Python 3.9 and hit a `str | None` union syntax added in 3.10.
All user-facing endpoints are already protected by `from __future__ import
annotations`. If you hit this in *your own* code, add the import at the
top of your file, or use `Optional[T]` instead. See ROADBLOCKS §E2.

---

## Calendar

### "Can't connect to iCloud CalDAV" / 401 Unauthorized

Two common causes:
1. **Using your regular Apple password.** iCloud CalDAV requires an
   *app-specific* password generated at [appleid.apple.com](https://appleid.apple.com)
   → Sign-In and Security → App-Specific Passwords.
2. **Using an `@icloud.com` alias instead of your primary Apple ID.** If
   your primary login is `you@gmail.com` but your alias is `you@icloud.com`,
   CalDAV wants the primary. The email shown at the top of the
   "Sign-In and Security" page is the right one. See ROADBLOCKS §I1.

Validate manually:
```bash
curl -X PROPFIND -u "$ICLOUD_USER:$ICLOUD_APP_PASSWORD" https://caldav.icloud.com/
```
Should return 207, not 401.

### "Nothing appears on my calendar after a solver run"

Check in order:
1. *Does the write calendar exist?* It's "Study Blocks" by default. Create
   it in Calendar.app first if you haven't.
2. *Did the sync run?* `GET /api/solver/log` should have at least one
   entry. If `chunks=0`, there's nothing to write.
3. *Permissions.* If the solver wrote events but Calendar.app never shows
   them, turn off other calendar filters or make sure the write calendar
   is visible.

### "Study Blocks keep piling up; no old ones get cleaned"

The AUTO-tagging logic only deletes events marked with
`CATEGORIES:AUTO-SCHED`. Pre-existing events created before AUTO tagging
landed get a secondary cleanup pass: any non-AUTO event *in the past* on
the write calendar is swept every resolve. Future non-AUTO events are
preserved (treated as user-placed). See ROADBLOCKS §I6, §I7.

### "CalDAV worked for a few hours then stopped"

The `caldav` library's connection pool can go stale after a long idle.
Our providers expose `reset()` which the orchestrator calls after a
suspected transient failure. If you're seeing persistent
`ProtocolError: keepalive timeout`, restart the orchestrator to force a
fresh DAVClient. Tracked as ROADBLOCKS §I5 (open follow-up for automatic
retry).

---

## Apple Reminders (macOS)

### `reminders_fetch` silently exits code 2

macOS TCC (Transparency/Consent/Control) requires explicit permission for
any binary to read Reminders. First invocation triggers a system prompt —
click Allow. If you revoked the grant via System Settings → Privacy &
Security → Reminders, the binary will silently fail until you re-grant.

If you rebuilt the binary with `swiftc`, the code signature changes and
TCC resets — run once to get the prompt back. See ROADBLOCKS §R3.

### "My modern reminders don't show up"

CalDAV-based Reminder reads only see *legacy* VTODO items. Anything
created on iOS 13+ with subtasks, tags, or shared lists is invisible to
CalDAV. That's why `AppleRemindersTodoSource` uses the Swift/EventKit
binary instead. See ROADBLOCKS §R1.

### A task keeps oscillating between "done" and "scheduled"

Reminder-match false positive. Your reminder closes the matching task;
the next Canvas sync reopens it (because Canvas still says it's
unsubmitted); the following Reminder sync closes it again; loop.

Guard: the `reminder_matches` handshake table records every
`(reminder_id, task_id)` pairing that closed a task. The same pair is
never used to close the task twice, so after the first reopen it stays
open. See ROADBLOCKS §R4.

If you're still seeing oscillation, something is creating *new* reminder
rows each cycle — check the reminder IDs in the DB:

```bash
sqlite3 state.db 'SELECT reminder_uid, task_id FROM reminder_matches'
```

Multiple rows for the same task_id mean the reminder side is generating a
new UID each time.

---

## Canvas

### "Canvas returns 1+ MB per request and the agent runs out of context"

Already handled — `CanvasTaskSource._ASSIGNMENT_FIELDS` whitelist trims
the response to the columns the solver actually uses, and `_is_relevant`
drops everything outside a -3d..+21d due-date window. A typical semester
fits in ~12 KB. If you changed the filter or the field list, revert.
See ROADBLOCKS §C1.

### "A task I already submitted is still 'scheduled'"

Canvas' `has_submitted_submissions` is coarse — it returns True for *any*
submission, including drafts. If you submitted a draft but are working on
a revision, Canvas still flags it submitted and we close the task. Use
the hub's quick-update ("status → scheduled") to reopen it manually. See
ROADBLOCKS §C2.

### "Course codes aren't matching my reminder lists"

We map via the `TODO_LIST_TO_COURSE` dict in `schedule_config.py`:

```python
TODO_LIST_TO_COURSE = {
    "accounting": "ACCTG 211",
    "macro": "ECON 104",
}
```

Key is a lowercased todo-list name; value is the course code as Canvas
emits it. Empty dict is fine — the matcher falls back to substring
comparison between the list name and the task's `course` field. See
ROADBLOCKS §D3.

---

## Agent / Anthropic

### "Session error: RemoteProtocolError: peer closed connection"

Anthropic's SSE stream can disconnect mid-session. The orchestrator
handles this: it catches the disconnect, replays history via
`sessions.events.list`, de-dupes by event id, and reopens the stream.
Up to 5 reconnects with exponential backoff.

If you're still seeing the error bubble up, it's past 5 reconnects —
which typically means the network was out longer than ~30 seconds. Check
connectivity; the session's progress up to that point is already in
`history.py`'s SQLite transcript. See ROADBLOCKS §N3.

### "agent.update() error: missing keyword-only argument 'version'"

The Anthropic SDK requires passing the current version number on update
(optimistic concurrency). Use `agents.retrieve(id)` first, then
`agents.update(id, version=agent.version, ...)`. The returned object has
a new `version = N+1`. See ROADBLOCKS §N4.

### "Session cost is too high"

Switch the model from Opus to Sonnet. `setup.py` defaults to
`claude-sonnet-4-6` for exactly this reason — our schedule-agent session
consumes <5K input tokens typically. If you customized `setup.py`'s
`model=` field, the billing difference is significant.

---

## Hub / network

### "Hub URL returns 404 on the bare hostname"

The hub lives at `/hub`, not `/`. The orchestrator includes a `/` → `/hub`
307 redirect that preserves the `?key=...` query string. If you're seeing
a 404, something in front of the orchestrator (reverse proxy, CDN) is
stripping the redirect — check your proxy config.

### "Hub token works but some endpoints return 401"

Every `/api/*` endpoint gates on `REPLAN_TOKEN`. The wizard stores it in
`.env`; the browser picks it up from the `?key=...` query param the first
time you load `/hub?key=<token>` and sets a cookie. Subsequent requests
use the cookie. Clear the cookie or switch browsers → you need the query
param again.

### "Cloudflared quick-tunnel URL changes every reboot"

Quick tunnels are ephemeral. Migrate to Tailscale:

```bash
# install Tailscale on the Mac, sign in
tailscale status      # note the MagicDNS hostname
# then on your phone/laptop:
open http://<host>.<tailnet>.ts.net:8787/hub?key=<token>
```

Free for personal use, stable hostname across reboots, WireGuard mesh
provides encryption. See ROADBLOCKS §T1–T2.

---

## Notifications

### "iMessage sent but no notification on my phone"

iOS intentionally suppresses notifications for messages sent from devices
signed in to the same Apple ID ("you're messaging yourself"). The message
lands in Messages.app — badge counts go up — but no push. This is a
platform behavior, not a bug on our side.

Switch to `NtfyNotifier` if you want real push. ROADBLOCKS §M2.

### "`osascript` exit 1, 'not authorized to send'"

TCC grant needed for osascript to drive Messages.app. Run manually once:

```bash
osascript macos/send_imessage.applescript "+15551234567" "test"
```

Accept the prompt. Subsequent sends (orchestrator-driven) work without
further prompts. ROADBLOCKS §M3.

### "ntfy pushes stopped arriving"

Check in order:
1. **Phone offline / in focus mode?** Obvious but common.
2. **Topic correct on both sides?** `grep NTFY_TOPIC .env` and compare
   against the topic you're subscribed to in the ntfy app.
3. **HTTP error on send?** `POST /api/solver/log` to see if the solver
   run even reached the notifier. If `sync_summary.error` mentions
   HTTP 429, you're rate-limited on public ntfy.sh — switch to a
   self-hosted ntfy server (pass `base_url=` to the constructor).

---

## Solver correctness

### "A task never gets scheduled"

Most likely the duration exceeds every free slot. The solver respects
`min_chunk_min`; if all slots are smaller than that floor, no placement
happens and the task lands in `at_risk`. Check:

```
GET /api/solver/log → look at at_risk_task_ids on the latest entry.
```

Lower `min_chunk_min` on the task, or clear some calendar time.

### "Overdue task doesn't get placed"

Shouldn't happen — ROADBLOCKS §S2 tests that. If you see this, the task's
deadline is probably in a bad format. Try setting `deadline_ts` to NULL
for that task via the hub and see if it places.

### "Chunks overlap my class blocks"

Shouldn't happen — ROADBLOCKS §S4 tests for it. If you see overlap,
check `config_blocks` in the DB:

```bash
sqlite3 state.db 'SELECT * FROM config_blocks'
```

The RRULE is the recurrence pattern — `FREQ=WEEKLY;BYDAY=MO,WE,FR` for a
MWF class. If yours is wrong, update it via the hub or fix the
`CLASS_BLOCKS` entry in `schedule_config.py` and re-bootstrap.

---

## Still stuck?

1. Check `ROADBLOCKS.md` — it's exhaustive and searchable.
2. Check `GET /api/health` — shows every provider's self-reported status.
3. Check `sqlite3 state.db .schema` — confirms the DB bootstrapped.
4. File an issue with: `orchestrator.log` tail, `sqlite3 state.db '.tables'`
   output, the output of `GET /api/health`, and the symptom.
