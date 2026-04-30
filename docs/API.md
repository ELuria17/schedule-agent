# API reference (orchestrator HTTP endpoints)

The orchestrator at `localhost:8787` exposes a small REST surface that the
browser hub uses today and the native iPhone + Mac apps consume in
[`~/schedule-agent-apps/`](https://github.com/ELuria17/schedule-agent-apps).

All endpoints require auth: pass `?key=<REPLAN_TOKEN>` as a query parameter
or send a `replan_token` cookie. The token is generated at install time and
lives in `~/schedule-agent-public/.env`.

FastAPI auto-publishes an OpenAPI 3 spec at `/openapi.json` — point any code
generator at it (e.g. `swift-openapi-generator` for the iOS apps) instead of
maintaining bindings by hand.

---

## Reads

### `GET /api/health`
Returns each configured provider's self-reported health (CalDAV, Canvas,
notifier, etc.). Used by the hub's status panel and by the test agent.

### `GET /api/tasks?status=<filter>`
List tasks. `status` is optional and accepts:
- omitted → active tasks (`scheduled`, `in_progress`, `blocked`)
- `all` → every row
- comma-separated set, e.g. `scheduled,done`

Each row is enriched with:
- `at_risk: bool` — true if the task didn't fully fit in the latest solver run.
- `chunks: [{task_id, calendar_event_uid, start_ts, end_ts, created_at}]` —
  the placed chunks for this task.

Returns `{"tasks": [...], "total": N}`.

### `GET /api/tasks/pending`
Tasks waiting for user approval before the solver picks them up. Populated
when a TaskSource is configured with `require_approval=True`. Returns
`{"tasks": [...], "total": N}`.

### `GET /api/schedule?start=<iso>&end=<iso>`
Cheap read of the cached chunks already placed by the most recent solver
run, joined with their task titles. **Does not invoke the solver** — safe
to poll from the apps. Default window: now → now+7d.

Returns:
```json
{
  "now_utc": "2026-04-30T20:01:00Z",
  "window_start": "2026-04-30T20:01:00Z",
  "window_end":   "2026-05-07T20:01:00Z",
  "chunks":  [{"task_id", "title", "start", "end", "duration_min", "notes"}, ...],
  "at_risk": [{"task_id", "title", "duration_needed_min",
               "duration_placed_min", "reason"}, ...]
}
```

### `GET /api/solver/resolve?dry_run=1` / `POST /api/solver/resolve`
Re-runs the solver. `dry_run=1` returns a fresh plan without writing to the
calendar. The bare GET/POST (no flag) writes through to the calendar — same
codepath the periodic poll uses.

### `GET /api/solver/log?limit=N`
Most-recent solver runs (default 30, max 200). Each entry has trigger,
timestamp, chunk count, at-risk count, and any error.

### `GET /api/agents`
Registered agents (`schedule-agent`, `email-digest`) with last-run summary
hydrated from in-memory state and persisted history.

### `GET /api/sessions`
Recent agent sessions (default last 20). Returns `{"sessions": [...]}`.

### `GET /api/sessions/{session_id}/events`
**SSE stream.** Replays the full transcript of a session, then tails live
events if the session is still running. Closes with an `event: close`
SSE block when terminal. The native apps subscribe here for the
"watch the agent work" view.

---

## Mutations

### `POST /api/tasks`
Create a task. Body:
```json
{
  "title": "Read Ch 3",        // required
  "duration_min": 60,          // required
  "deadline_ts": "2026-05-02T22:00:00Z",
  "priority": "medium",        // asap | high | medium | low
  "course": "ECON 104",
  "min_chunk_min": 30,
  "max_chunk_min": 120,
  "preferred_window": "morning",  // morning | afternoon | evening
  "notes": "..."
}
```
Returns `{"task": {...}}`. Asynchronously kicks a re-solve.

### `PATCH /api/tasks/{id}`
Partial update. Same field set as create; pass only what you want to change.
A change to `duration_min` flips `duration_locked=1` so the relearn step
won't override it.

### `POST /api/tasks/{id}/complete`
Body: `{"actual_min": 45}` (optional). Recorded in `task_history` for the
duration-learning loop.

### `DELETE /api/tasks/{id}`

### `POST /api/tasks/{id}/approve` / `POST /api/tasks/{id}/reject`
For pending-review tasks. Approve moves to `scheduled`; reject moves to
`hidden` (stays in source-id key, doesn't re-create on next sync).

### `POST /api/agents/{agent_key}/trigger`
Kicks off a fresh agent session. Returns `{"ok": true, "pending": true}`
immediately; subscribe to `/api/sessions` to find the new session id.

### `POST /replan`
Legacy iOS Shortcut endpoint. Same effect as triggering `schedule-agent`
via `/api/agents/schedule-agent/trigger`.

---

## Authentication

The single shared `REPLAN_TOKEN` is the only gate. `_authed` accepts:
- `?key=<TOKEN>` query param, or
- `replan_token=<TOKEN>` cookie.

For the multi-tenant SaaS path, this gets replaced by per-user JWTs — see
[`docs/SAAS_PLAN.md`](SAAS_PLAN.md).
