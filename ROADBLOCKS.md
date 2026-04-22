# Schedule Agent — Build Roadblocks Log

A chronological + categorized log of every bug, surprise, or architectural dead-end we
hit while building this system, with root cause and fix. Written to serve as:

1. Historical record of why the code looks the way it does.
2. Source material for a future **test agent** that validates a fresh install.
3. Onboarding doc for anyone trying to adapt this to their own setup.

Each entry includes a "**Test signal**" line — a concrete assertion a test agent
could check to verify the fix is still in place.

---

## Table of contents

- [Architecture (high-level pivots)](#architecture-high-level-pivots)
- [macOS / Python environment](#macos--python-environment)
- [iCloud CalDAV quirks](#icloud-caldav-quirks)
- [Apple Reminders quirks](#apple-reminders-quirks)
- [Canvas LMS integration](#canvas-lms-integration)
- [Anthropic Managed Agents SDK](#anthropic-managed-agents-sdk)
- [Notification / messaging path](#notification--messaging-path)
- [Tunneling / remote access](#tunneling--remote-access)
- [Solver correctness](#solver-correctness)
- [Task intake / data consistency](#task-intake--data-consistency)
- [Reliability / lifecycle](#reliability--lifecycle)
- [FastAPI / Python gotchas](#fastapi--python-gotchas)
- [Open follow-ups](#open-follow-ups)

---

## Architecture (high-level pivots)

### A1. "Constantly moving tasks" misinterpreted as a daemon
- **Symptom:** Requirements conversation assumed an always-on agent watching state.
- **Root cause:** Anthropic Managed Agents sessions are one-shot — create, run, done.
  There is no long-lived LLM loop that "watches" anything.
- **Fix:** Periodic triggers (cron, 15-min poll, task mutations, hub-load stale check)
  wake the agent and/or solver, rather than a persistent process.
- **Test signal:** Verify that after a task mutation the solver has been invoked within
  ~5 seconds (`_SOLVER_LOG` gets a `task_mutation`-triggered entry).

### A2. LLM as scheduler vs deterministic solver
- **Symptom:** First design had the LLM read Canvas + Reminders + Calendar and pick
  times directly. It made arithmetic mistakes (scheduled during class times, inflated
  quiz durations, missed due-date priority).
- **Root cause:** LLMs are bad at combinatorial constraint satisfaction, especially
  when constraints accumulate beyond the prompt's token budget.
- **Fix:** Motion-style rebuild — deterministic Python solver places chunks;
  LLM only does task intake + daily summary. System prompt dropped from ~4000 chars
  to ~2000 chars after moving schedule data into SQLite config tables.
- **Test signal:** Agent session events should contain *only* `task_*`,
  `schedule_query`, and `send_sms` tool calls — **never** `calendar_*`.

### A3. Hub + multiple agents
- **Symptom:** Wanted to add a second agent (Email Digest) but the orchestrator was
  hardcoded for the schedule-agent only.
- **Root cause:** Tool registry and session runner were single-agent assumptions.
- **Fix:** Hook-based `AGENTS` dict with `build_kickoff`, `build_resources`,
  `dispatch_tools`, `pull_state_file`, `on_final` per entry. New agents are one dict
  entry + a system prompt update via `agents.update()`.
- **Test signal:** `GET /api/agents` returns both `schedule-agent` and
  `email-digest` without any code duplication for agent-specific paths.

---

## macOS / Python environment

### E1. No Homebrew on target Mac
- **Symptom:** `brew install python` fails.
- **Fix:** Used macOS system Python 3.9 inside a venv. Confirmed `zoneinfo` + sqlite3 +
  asyncio all present in 3.9.
- **Test signal:** `python3 --version` returns 3.9+, `python3 -c "from zoneinfo import ZoneInfo"` exits 0.

### E2. Python 3.9 doesn't support `X | Y` union syntax
- **Symptom:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`
  at class definition time.
- **Root cause:** PEP 604 pipe-union is 3.10+.
- **Fix:** Added `from __future__ import annotations` (defers annotation evaluation)
  and replaced `X | None` with `Optional[X]` in every function signature FastAPI
  introspects at decoration time (`__future__` doesn't help FastAPI because it
  forces evaluation via `eval_type_lenient`).
- **Test signal:** `grep -rn "str | None" orchestrator.py` returns 0 in endpoint
  signatures. `python -c "import orchestrator"` exits 0.

### E3. `python-dotenv`'s `set_key` wraps values in single quotes
- **Symptom:** Constructing a URL with `grep '^TOKEN=' .env | cut -d= -f2` included
  literal `'...'` quotes. Downstream HTTP request 403'd because the server-side
  token didn't match.
- **Fix:** Always extract via `dotenv.dotenv_values()` which strips quotes.
- **Test signal:** After `set_key`, `dotenv_values()[key] == raw_value_without_quotes`.

---

## iCloud CalDAV quirks

### I1. 401 Unauthorized on app-specific password
- **Symptom:** Correct app-specific password, wrong Apple ID — rejected.
- **Root cause:** `ICLOUD_USER` must be the *primary Apple ID login email*, which may
  differ from the `@icloud.com` alias shown in Mail. A user whose primary login is
  `someone@gmail.com` with an `@icloud.com` alias must use the Gmail address.
- **Fix:** Checked at appleid.apple.com → "Sign-In and Security" — the email at the top
  of that page is the one CalDAV accepts.
- **Test signal:** `curl -X PROPFIND -u "$ICLOUD_USER:$ICLOUD_APP_PASSWORD" https://caldav.icloud.com/` returns 207, not 401.

### I2. `cal.event_by_uid()` returns 412 Precondition Failed on iCloud
- **Symptom:** Couldn't modify/delete events by UID; got `caldav.lib.error.ReportError`.
- **Root cause:** iCloud's CalDAV REPORT query implementation is flaky for UID-only
  lookups on certain event collections.
- **Fix:** Replaced with `cal.search(event=True, expand=False)` and Python-side UID
  matching. See `_find_event_in_study_blocks` in `orchestrator.py`.
- **Test signal:** After creating an AUTO event, `_find_event_in_study_blocks(uid)`
  returns the event without error.

### I3. ETag mismatch (412) on update/delete via caldav library
- **Symptom:** `ev.save()` and `ev.delete()` sent `If-Match: "<etag>"` headers;
  iCloud server updated its internal ETag after our write, so the next modify
  failed.
- **Root cause:** Optimistic concurrency control in the caldav library + iCloud's
  tendency to mutate events server-side (adds `X-APPLE-*` fields).
- **Fix:** Raw `requests.put/delete` directly against the event URL, skipping
  `If-Match`. The `event_url` field is now included in `calendar_list_events` output.
- **Test signal:** `PUT` to `str(ev.url)` without `If-Match` returns 204 on first
  try after a recent read.

### I4. `cal.todos(include_completed=False)` returns ReportError
- **Symptom:** When listing open Apple Reminders via CalDAV, filtering by
  completion state crashed.
- **Root cause:** iCloud's CalDAV server doesn't implement the completion-filter
  predicate correctly.
- **Fix:** Always fetch with `include_completed=True`, filter in Python.
- **Test signal:** `cal.todos(include_completed=True)` returns without exception for
  any VTODO-supporting calendar on iCloud.

### I5. CalDAV connection keepalive timeout after idle
- **Symptom:** After the orchestrator ran idle for several hours, the next CalDAV
  call hit `urllib3.exceptions.ProtocolError: keepalive timeout`.
- **Root cause:** Cached `caldav.DAVClient.principal()` connection pool went stale.
- **Fix (partial):** `orchestrator._cache.clear()` from a fresh Python process
  resets it. **Open follow-up:** add automatic retry + cache-clear on
  `ConnectionError` in `calendar_list_events` and `replace_auto_window`.
- **Test signal:** After an intentional 10-minute idle, a CalDAV call either succeeds
  or triggers the retry-with-reconnect path.

### I6. Legacy events with no CATEGORIES tag never get cleaned up
- **Symptom:** Study Blocks written by the pre-Motion LLM agent (before we added
  `CATEGORIES:AUTO-SCHED`) stayed on the calendar forever because the
  clear-and-replace logic only touched AUTO-tagged events.
- **Root cause:** Phase-3 AUTO tagging is forward-only.
- **Fix:** `_solver_writer` now does a second sweep that deletes any **non-AUTO
  Study Blocks event in the past** (before `now`). Future non-AUTO events are
  preserved in case the user placed them manually.
- **Test signal:** After a resolve, `list(e for e in calendar_list_events(-30d, now, ["study_blocks"]) if not e["is_auto"])` is empty.

### I7. `replace_auto_window` left past AUTO blocks alive
- **Symptom:** User saw 9 AM blocks on their calendar at 11 AM with no cleanup,
  while a still-active task got rescheduled for later in the day — two entries
  for one task.
- **Root cause:** `_solver_writer` passed `start_utc = now` so the wipe range was
  `[now, now+14d]`. Anything before `now` survived.
- **Fix:** Widened clear range to `[now - 60 days, now + 14 days]`.
- **Test signal:** Inject a fake past AUTO event via `calendar_create_study_block(..., auto=True)` dated 3 days ago; after one `resolve`, `list_auto_events(-30d, now)` no longer contains it.

---

## Apple Reminders quirks

### R1. Modern reminders invisible via CalDAV
- **Symptom:** First implementation of `reminders_list_all` via CalDAV returned
  only two entries: Apple's own "these reminders were upgraded" warnings.
- **Root cause:** iOS 13+ "upgraded" reminders (subtasks, tags, shared lists) are
  invisible to CalDAV. CalDAV only sees the legacy VTODO items — none of which
  existed for this user.
- **Fix:** Wrote a native Swift binary `reminders_fetch.swift` using EventKit,
  compiled with `swiftc`. Uses indexed predicates
  (`predicateForIncompleteReminders`, `predicateForCompletedReminders`) for speed.
- **Test signal:** `./reminders_fetch` returns non-empty JSON with
  `{uid, list, title, due, completed, completed_at}` keys. TCC permission to access
  Reminders is granted on first run.

### R2. JXA (JavaScript for Automation) too slow for large reminder lists
- **Symptom:** Prototype JXA version took >60s with 846 reminders.
- **Root cause:** Every property access via the Apple Event bridge crosses a process
  boundary. 846 iterations × several accesses per reminder = minute-scale.
- **Fix:** See R1 — Swift + predicates, now ~0.7 seconds.
- **Test signal:** `time ./reminders_fetch` completes in <3s.

### R3. Swift binary needs TCC permission on first run
- **Symptom:** Swift binary silently exits with code 2 / "access denied".
- **Root cause:** macOS's Transparency/Consent/Control (TCC) requires explicit
  approval for any binary to read Reminders. The first run prompts; subsequent
  runs are authorized.
- **Fix:** Prompt appears on first invocation. User clicks Allow. If rebuilt with
  different code signature, permission resets.
- **Test signal:** After granting, `./reminders_fetch` exits 0. If permission
  revoked via System Settings, it returns stderr "access denied".

### R4. Reminder-to-task false positives (toggle loop)
- **Symptom:** Canvas task "Read Ch. 14" was auto-marked done by the reminder
  cross-reference; on next sync, Canvas re-opened it; on the sync after, the
  reminder matched again and re-closed it. Infinite flap.
- **Root cause:** Loose token overlap (2+ shared significant tokens) between a
  completed reminder and an open Canvas task triggered `mark_complete` on every
  sync.
- **Fix:** New `reminder_matches` handshake table. Once
  `(reminder_uid, task_id)` has been matched and closed, it is **never** re-matched
  — even if the reminder stays completed and the task reopens.
  Canvas is the authoritative source for open/closed state on Canvas-sourced tasks.
- **Test signal:** After two consecutive resolves with no state change, the
  reminder-matches count is zero on the second one.

---

## Canvas LMS integration

### C1. Runaway Canvas response size
- **Symptom:** A single `canvas_list_assignments()` call returned 1.68 MB of JSON
  (~420K tokens). This bloated the agent's context, eating rate-limit budget and
  burning credits.
- **Root cause:** Canvas `/courses/:id/assignments` returns full `description` HTML
  for every assignment. Multiplied by ~10 courses and hundreds of assignments.
- **Fix:** `_ASSIGNMENT_FIELDS` whitelist + `_is_relevant` filter (due within -3 to
  +21 days) trims response to ~12 KB.
- **Test signal:** `len(json.dumps(canvas_list_assignments()))` is under 50K for a
  typical semester.

### C2. Canvas `has_submitted_submissions` is a coarse signal
- **Symptom:** Tasks where user submitted a draft (but was working on a revision)
  were marked done.
- **Root cause:** Canvas returns `true` if *any* submission has ever been sent.
  Doesn't distinguish draft vs final.
- **Fix:** Document as a known behavior. User can `task_update(status='scheduled')`
  via the hub to reopen if needed.
- **Test signal:** None automatic — this is a limitation of the upstream field.

### C3. Course code extraction from Canvas course names
- **Symptom:** Course names like `"ECON104 - Dave Brown - SP26"` needed parsing
  into `"ECON 104"` to match the reminder list mapping.
- **Fix:** Regex `([A-Z]{3,5})\s*(\d{3,4})` in `sync_tasks_from_sources`.
- **Test signal:** Feed `"MGMT 301, Section 008: Basic Mgmt Concept"` → get
  `"MGMT 301"`.

---

## Anthropic Managed Agents SDK

### N1. `anthropic.beta.sessions.stream()` doesn't exist
- **Symptom:** Original docs I followed said `client.beta.sessions.stream(...)`.
  At runtime: `AttributeError: 'Sessions' object has no attribute 'stream'`.
- **Root cause:** The actual path in SDK 0.96 is
  `client.beta.sessions.events.stream(session_id=...)`. Doc drift.
- **Fix:** Use `events.stream`. The `_consume_session` helper in
  `orchestrator.py` is the canonical entry point.
- **Test signal:** `hasattr(anthropic.Anthropic().beta.sessions.events, 'stream')` is True.

### N2. `agent.custom_tool_use` event field naming
- **Symptom:** `event.tool_name` → `AttributeError`.
- **Root cause:** The actual attribute is `event.name`, not `event.tool_name`. Doc drift.
- **Fix:** `_process_event` uses `event.name`.
- **Test signal:** A session that emits a custom tool use has an event where
  `event.type == "agent.custom_tool_use"` and `getattr(event, "name", None)` is the
  tool name.

### N3. SSE stream silently disconnects mid-session
- **Symptom:** User got iMessages saying
  `"Scheduler error: RemoteProtocolError: peer closed connection without sending complete message body"`.
  The session had made partial progress on Anthropic's side — some tool calls
  were sent, but we missed them and never responded. Session ended up orphaned
  in `requires_action`.
- **Root cause:** Anthropic's SSE transport (via `httpx`) can close when the network
  hiccups or the stream is long-lived. No replay mechanism — once missed, events are lost from the stream.
- **Fix:** `_consume_session` now catches
  `(httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.ConnectError, httpx.NetworkError)`,
  then replays history via `anthropic.beta.sessions.events.list(session_id=...)`
  before reopening the stream. Dedupes by `event.id`. Exponential backoff up to 5
  reconnects before bubbling the error.
- **Test signal:** Simulate a mid-stream disconnect (e.g., kill network for 2s
  during a session); the `_consume_session` log should show a "stream hiccup;
  reconnecting" note event and the session completes normally.

### N4. Agent.update requires the current version number
- **Symptom:** First `client.beta.agents.update(id, model=...)` call errored:
  `TypeError: update() missing 1 required keyword-only argument: 'version'`.
- **Root cause:** Optimistic-concurrency requirement — you must pass the version
  you read to prove you're updating from known state. The SDK returns a new
  version number.
- **Fix:** Always `retrieve` first, pass `version=agent.version` on update.
- **Test signal:** `agents.update(id, version=N, system=...)` returns an object
  with `version=N+1`.

### N5. Billing / context-size rate limits
- **Symptom:** Session errored with
  `"model_rate_limited_error"` → masked a `"billing_error"` once credits ran out.
- **Root cause:** Opus 4.7 + 1.68 MB Canvas response = expensive.
- **Fix:** Switched to Sonnet 4.6 (~40% cheaper), trimmed Canvas response (see C1),
  restructured agent so it no longer re-reads large data on every session (now it
  queries the SQLite-materialized plan via `schedule_query`).
- **Test signal:** A typical session now consumes <5K input tokens.

---

## Notification / messaging path

### M1. Twilio toll-free verification blocks first message
- **Symptom:** Toll-free number bought, test SMS succeeded (API returned 200), but
  nothing arrived on the phone. Twilio Console showed "pending verification".
- **Root cause:** US toll-free numbers require a **Messaging Toll-Free Verification**
  form (legal entity, business type, use case) and 3–5 business day review before
  sending SMS to *any* destination.
- **Fix:** Abandoned Twilio for the personal-use case. Switched to iMessage via
  AppleScript through Messages.app on the user's Mac.
- **Test signal:** `python -c "from orchestrator import send_sms; print(send_sms('test'))"` returns `{"ok": True}` without touching Twilio.

### M2. iMessage to self doesn't push-notify
- **Symptom:** iMessage lands in the user's Messages.app chat but no iOS
  notification.
- **Root cause:** iOS suppresses notifications for messages sent from devices
  signed in to the same Apple ID ("you're messaging yourself").
- **Fix:** Accepted as a tradeoff. Messages.app badge counter still increments.
  Alternative: ntfy.sh or Pushover for push-style delivery (not adopted here).
- **Test signal:** None automatic — this is a platform behavior.

### M3. AppleScript needs TCC permission to control Messages
- **Symptom:** First `osascript send_imessage.applescript` exited code 1 with
  "not authorized to send" in stderr.
- **Root cause:** TCC permission for `osascript` to control `Messages.app`.
- **Fix:** macOS prompts on first run; user clicks Allow.
- **Test signal:** After grant, `osascript send_imessage.applescript +1... "test"` exits 0.

---

## Tunneling / remote access

### T1. Cloudflared quick tunnel URL rotation
- **Symptom:** Every `cloudflared tunnel --url` invocation gets a new
  `*.trycloudflare.com` URL. After every Mac reboot, bookmarks and iOS Shortcut
  broke.
- **Root cause:** Quick tunnels are intentionally ephemeral.
- **Fix:** Migrated to **Tailscale**. Stable MagicDNS hostname
  `<mac>.<tailnet>.ts.net` never changes. Free for personal use.
- **Test signal:** `tailscale status` returns the Mac's hostname; `curl http://<hostname>:8787/hub?key=<token>` returns 200 from the iPhone on any network.

### T2. `tailscale serve --bg` silently does nothing
- **Symptom:** `tailscale serve --bg http://localhost:8787` returned immediately;
  `tailscale serve status` showed "No serve config".
- **Root cause:** Likely requires HTTPS certificate provisioning enabled in the
  Tailscale admin console, which this user hadn't enabled.
- **Fix:** Skipped `tailscale serve`; bound the orchestrator directly on
  `0.0.0.0:8787`. Tailscale's WireGuard mesh provides the encryption + access control;
  `REPLAN_TOKEN` is still the app-level gate.
- **Test signal:** `lsof -nP -i :8787` shows `TCP *:8787 (LISTEN)`.

### T3. Hub URL returns 404 on bare hostname
- **Symptom:** Typing just the hostname (no `/hub` path) returned `{"detail":"Not Found"}`.
- **Fix:** Added a `/` → `/hub` 307 redirect that preserves query string.
- **Test signal:** `curl -I http://<host>:8787/` returns 307; follow with `-L` lands on `/hub`.

---

## Solver correctness

### S1. Deterministic scheduling is non-negotiable
- See A2 above. The fundamental architecture shift.

### S2. Overdue tasks refused placement
- **Symptom:** A task with `deadline_ts` in the past never got scheduled, even
  though Canvas showed it still open.
- **Root cause:** `_split_task_across_slots` broke out of its loop when
  `slot.start >= deadline_utc` — every future slot qualified.
- **Fix:** At the top of `_split_task_across_slots`, if `deadline_utc < now_utc`,
  set `deadline_utc = None` (no cap). Urgency boost still applies via
  `priority_score`.
- **Test signal:** Create a task with `deadline_ts = (now - 12h)` and
  `duration_min = 30`; after `solver.resolve()`, expect a chunk placed for it in a
  future free slot.

### S3. Sub-minimum sliver chunks
- **Symptom:** A task's placement ended with a 1-minute chunk right before a class
  start.
- **Root cause:** When `remaining < min_chunk_min`, the greedy took whatever the
  slot could hold without the `min_chunk` floor.
- **Fix:** Added `if take < min_chunk: break` inside the placement loop. Leftover
  minutes silently absorbed as "rounding error" in the final chunk placement.
- **Test signal:** For every emitted chunk, `chunk.duration_min >= task.min_chunk_min`.

### S4. Time-zone boundary math lost 1 minute at class boundaries
- **Symptom:** A 60-min task got a 59-min chunk when the next free slot ended at
  a class start.
- **Root cause:** `now_utc = datetime.now(timezone.utc)` has microseconds; arithmetic
  with integer minute truncation (`int((end - start).total_seconds() / 60)`)
  rounded down.
- **Fix:** Accepted as minor rounding. The `if take < min_chunk: break` rule from
  S3 absorbs the 1-minute remainder into the previous chunk rather than creating
  a sliver.
- **Test signal:** Chunks never overlap `config_blocks` — even by a second. Validate
  via `assert chunk.end <= next_blocker.start` for every (chunk, next-blocker) pair.

### S5. Priority inference for overdue-but-open
- **Symptom:** An overdue task was rated "medium" priority based on age-bracket
  heuristics.
- **Fix:** `_priority_from_canvas` treats `hours_until < 24` as `asap` — which
  correctly catches negative (overdue) values since they're < 24.
- **Test signal:** `_priority_from_canvas({"due_at": (now - 48h).iso})` returns `"asap"`.

---

## Task intake / data consistency

### D1. Reminder toggle loop (see R4)

### D2. Canvas-authoritative reopen
- **Symptom:** Tasks that got marked `done` via reminder matching stayed done
  forever, even after Canvas evidence (still unsubmitted) contradicted.
- **Fix:** `tasks.upsert_from_canvas` now **reopens** any existing Canvas-sourced
  task whose status is `done` if the Canvas call's filter put it in the
  non-submitted pipeline. Canvas beats Reminders for Canvas-sourced tasks.
- **Test signal:** Manually set a Canvas task's status to `done`. Call
  `upsert_from_canvas(same_assignment, has_submitted=False)`. Task status
  returns to `scheduled`.

### D3. iCloud Reminders list → course mapping
- **Symptom:** Reminder in list "Management" should match task with course
  "MGMT 301" but didn't.
- **Fix:** Hardcoded mapping `_REMINDER_LIST_TO_COURSE` in orchestrator.py.
- **Test signal:** User-editable. For any new course, add a row. The test agent
  could validate that each active course has a mapping.

### D4. Pre-AUTO legacy events on Study Blocks calendar (see I6)

---

## Reliability / lifecycle

### L1. Mac sleep pauses APScheduler
- **Symptom:** Solver log showed last poll at 7:17 PM; user woke Mac at 11 AM the
  next day; no resolves had run; stale blocks on calendar.
- **Root cause:** APScheduler's `IntervalTrigger` computes next fire based on
  elapsed wall-clock time. When the Mac sleeps, elapsed seconds continue, but the
  in-process scheduler's timer is also paused. On wake, the next fire is the
  regular 15-min boundary — so up to 15 min of "blindness" after wake.
- **Partial fix:**
  - `hub` handler now calls `_kick_stale_resolve(max_age_seconds=300)` — if the
    last log entry is older than 5 minutes, kick a background resolve. Fixes the
    UX: opening the hub always shows a fresh plan.
  - Orchestrator `@app.on_event("startup")` runs a first resolve on boot.
- **Deeper fix (open):** Register a macOS wake-notification observer (via
  `pmset` or IOKit) and fire a resolve on wake. Not implemented.
- **Test signal:** Open `/hub` after ~10 min of idle; `_SOLVER_LOG[0].trigger` is
  `"hub_stale"` within a few seconds.

### L2. Orchestrator restart clears in-memory state
- **Symptom:** `_SOLVER_LOG`, `SESSIONS`, `SESSION_ORDER` all live in memory.
  Launchd `KeepAlive` restart wipes them.
- **Acceptable:** Tasks persist in SQLite. Session transcripts are lost on restart;
  for this personal tool it's fine. A public multi-user version should persist
  sessions.

### L3. Tunnel (cloudflared) process crash not restarting
- **Symptom:** Cloudflared's control stream failed; process kept running but
  stopped serving.
- **Root cause:** `KeepAlive=true` on launchd only restarts on process exit, not
  on "alive but broken".
- **Fix at the time:** `kill -9` the cloudflared PID, let launchd respawn with a
  new URL. Post-Tailscale migration this class of issue is moot.

---

## FastAPI / Python gotchas

### F1. `@app.on_event("startup")` is deprecated
- **Symptom:** Logs: `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead`.
- **Not yet fixed.** Still works; migrating to `lifespan` is a tidy follow-up.

### F2. FastAPI evaluates type annotations at decoration time
- See E2 above.

### F3. `asyncio.Queue` can't be put to from a non-loop thread directly
- **Symptom:** SSE consumers needed to receive events from the solver's background
  thread.
- **Fix:** Capture the main event loop in startup hook
  (`_main_loop = asyncio.get_running_loop()`), then use
  `_main_loop.call_soon_threadsafe(q.put_nowait, envelope)` in `_publish`.
- **Test signal:** SSE subscribers receive events live (within ~100ms) from a
  solver run triggered in a background thread.

### F4. `ORDER BY deadline_ts ASC NULLS LAST` not supported in SQLite
- **Symptom:** `sqlite3.OperationalError: near "NULLS"`.
- **Fix:** `ORDER BY (deadline_ts IS NULL), deadline_ts ASC, id ASC` — puts NULLs
  last by sorting on the boolean first.

---

## Open follow-ups

Items we knowingly deferred but should revisit before any "for other people" release:

1. **Shabbat / religious observance blocking** — deferred from the Motion rebuild
   plan. Needs Hebcal API integration (free) or manual `config_blocks` entries.
2. **Task dependencies** — `task_deps` table exists in the schema but solver
   doesn't enforce it yet.
3. **Preferred-window soft bonus** — field exists on the task but the greedy
   doesn't use it.
4. **Automatic retry-on-reconnect for CalDAV** — see I5. Right now one stale
   connection can fail one resolve cycle.
5. **macOS wake hooks** — see L1. Would eliminate the hub-load-kick workaround.
6. **HTTPS on Tailscale via `tailscale serve`** — cosmetic; Safari flags HTTP as
   "Not Secure" even though WireGuard encrypts underneath.
7. **Session persistence in SQLite** — session transcripts currently lost on
   orchestrator restart. For multi-user publish, move SESSIONS/SESSION_ORDER to
   a DB table.
8. **`@app.on_event("startup")` → lifespan handler** — see F1.
9. **Agent prompt regression tests** — we've updated the agent system prompt
   several times via `agents.update`. Each update risks regressing behavior. No
   automated check that the agent still respects class times, duration rules, etc.
10. **Test-agent itself** — the thing that prompted this doc. Should cover every
    "Test signal" line above.

---

## How a test agent should use this file

Recommended approach:

1. Parse every `**Test signal:**` line into an assertion case.
2. Group cases by section — infrastructure tests run early (env, auth), then
   integration tests (CalDAV, Canvas, Reminders), then solver tests (pure
   function unit tests on `solver.py`), then end-to-end smoke tests.
3. Maintain a separate **fixtures** module with:
   - Sample Canvas assignment JSON (vary `has_submitted_submissions`, `due_at`).
   - Sample Reminder EventKit JSON.
   - Sample iCloud event VCALENDAR snippets (with and without `CATEGORIES`).
4. For SDK-dependent tests (Anthropic Managed Agents, iCloud CalDAV), the test
   agent should detect "unreachable" and skip with a warning rather than failing
   the whole suite — these are external services that can be transiently down.
5. Before publishing, each `Open follow-up` should either be closed or have its
   own `ROADBLOCKS.md` section documenting why it's still open.

Keep this file updated whenever a new class of bug surfaces — add a new entry
rather than editing in place. That preserves the historical record.
