from __future__ import annotations
import os, sys, json, time, threading, uuid, subprocess, asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from fastapi import FastAPI, Request, HTTPException, Cookie
from fastapi.responses import Response, HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests
import httpx

# Hydrate os.environ from the resolved .env BEFORE importing
# schedule_config — schedule_config instantiates providers at import time
# and reads credentials off os.environ. When orchestrator is imported by
# run.py, run.py already loaded dotenv; this call is a harmless no-op in
# that case (dotenv skips already-set keys). When orchestrator is imported
# standalone (tests, `python orchestrator.py` directly, or a shell that
# sourced .env by hand), this still does the right thing.
from paths import env_path, learning_state_path, morning_marker_path
load_dotenv(env_path())

# Motion-style rebuild modules (phase 1+2 foundations).
import config as _config_mod  # noqa: F401 (import triggers schema bootstrap)
import tasks as tasks_mod
import solver as solver_mod
import history
from providers.base import CalendarEvent
from providers.task_source import SourceTask
from providers.todo_source import TodoItem
# Backend wiring — swap providers by editing schedule_config.py, not this file.
from schedule_config import CALENDAR, TASK_SOURCES, TODO_SOURCE, NOTIFIER, TIMEZONE

# Only operational / hub-auth env lives here. All provider-related
# environment variables are read by schedule_config.py.
ENV_ID = os.environ["ENVIRONMENT_ID"]
AGENT_ID = os.environ["AGENT_ID"]
REPLAN_TOKEN = os.environ["REPLAN_TOKEN"]
TZ = ZoneInfo(TIMEZONE)

PROJECT_DIR = Path(__file__).resolve().parent
STATE_PATH = learning_state_path()
HUB_HTML_PATH = PROJECT_DIR / "hub.html"   # bundled asset, not user state
MORNING_MARKER_PATH = morning_marker_path()

if not STATE_PATH.exists():
    STATE_PATH.write_text(json.dumps({
        "task_duration_estimates": {},
        "time_of_day_productivity": {},
        "last_run": None,
    }, indent=2))

anthropic = Anthropic()

# ========== Email digest (wraps ~/gmail-digest-agent/run_digest.py as a library) ==========
sys.path.insert(0, str(Path.home() / "gmail-digest-agent"))
try:
    import run_digest as _digest
    DIGEST_IMPORT_ERROR = None
except Exception as e:
    _digest = None
    DIGEST_IMPORT_ERROR = f"{type(e).__name__}: {e}"

def _digest_build_kickoff():
    hours = _digest.default_lookback_hours()
    emails, _received = _digest.fetch_recent_emails(hours=hours)
    if not emails:
        return (f"No emails from the tracked senders in the last {hours}h. "
                f"Produce a brief 'no digest today' HTML note.")
    emails_text = _digest.format_emails(emails)
    return f"Produce today's digest from these emails:\n\n{emails_text}"

def _digest_on_final(collected_text: str):
    html, charts = _digest.extract_charts(collected_text)
    _digest.send_digest(html, charts)
    return f"Digest emailed ({len(html)} chars HTML, {len(charts)} charts)"

# Providers (CALENDAR, TASK_SOURCES, TODO_SOURCE, NOTIFIER) are imported
# from schedule_config above. Don't re-instantiate them here.

# ========== Notifier shim ==========
# Thin wrapper so the agent tool "send_sms" resolves to NOTIFIER.send.
# Keeping the function name preserves the agent's tool schema unchanged.
def send_sms(body: str):
    return NOTIFIER.send(body)

# ========== Agent-facing task tools (Motion-style) ==========

def _auto_resolve() -> dict:
    """Re-run the solver after a task mutation. Routes through _do_solver_run
    so it's serialized with the poll + logged."""
    entry = _do_solver_run(trigger="task_mutation", sync=False)
    return entry


def task_create_tool(title, duration_min, deadline_ts=None, priority="medium",
                     course=None, min_chunk_min=30, max_chunk_min=120,
                     preferred_window=None, notes=None):
    t = tasks_mod.create(
        title=title, duration_min=int(duration_min),
        deadline_ts=deadline_ts, priority=priority, course=course,
        min_chunk_min=int(min_chunk_min), max_chunk_min=int(max_chunk_min),
        preferred_window=preferred_window, source="llm", notes=notes,
    )
    return {"ok": True, "task": t, "resolve": _auto_resolve()}


def task_update_tool(id, **fields):
    t = tasks_mod.update(int(id), **fields)
    if t is None:
        return {"error": f"task {id} not found"}
    return {"ok": True, "task": t, "resolve": _auto_resolve()}


def task_complete_tool(id, actual_min=None):
    t = tasks_mod.mark_complete(int(id), actual_min=actual_min)
    if t is None:
        return {"error": f"task {id} not found"}
    return {"ok": True, "task": t, "resolve": _auto_resolve()}


def task_list_tool(status_filter=None, limit=50):
    if status_filter:
        statuses = status_filter if isinstance(status_filter, list) else [status_filter]
        rows = tasks_mod.list_all(status_filter=statuses)
    else:
        rows = tasks_mod.list_active()
    return {"tasks": rows[: int(limit)], "total": len(rows)}


def schedule_query_tool(start, end, include_at_risk=True):
    """Return chunks the solver has placed in [start, end). Optionally run a
    fresh dry-run to surface at-risk tasks."""
    import config as _cfg
    # Chunks already placed in DB (what's on iCloud right now)
    with _cfg.connect() as conn:
        rows = conn.execute(
            """SELECT sc.id, sc.task_id, sc.start_ts, sc.end_ts,
                      t.title, t.course, t.priority, t.duration_min
               FROM scheduled_chunks sc
               JOIN tasks t ON t.id = sc.task_id
               WHERE sc.start_ts >= ? AND sc.start_ts < ?
               ORDER BY sc.start_ts""",
            (start, end),
        ).fetchall()
    chunks = [dict(r) for r in rows]
    out = {"chunks": chunks, "count": len(chunks)}
    if include_at_risk:
        try:
            dry = solver_mod.resolve(
                window_days=14,
                external_events_fn=_solver_external_events,
                writer_fn=None,
            )
            out["at_risk"] = [r.to_dict() for r in dry.at_risk]
        except Exception as ex:
            out["at_risk_error"] = f"{type(ex).__name__}: {ex}"
    return out


TOOLS = {
    # Agent-facing (task CRUD + queries + SMS). This is everything the LLM is
    # allowed to invoke. Calendar writes now flow through the solver →
    # CalendarProvider, not through the agent.
    "task_create": task_create_tool,
    "task_update": task_update_tool,
    "task_complete": task_complete_tool,
    "task_list": task_list_tool,
    "schedule_query": schedule_query_tool,
    "send_sms": send_sms,
}

def run_tool(name, input_):
    try:
        return json.dumps(TOOLS[name](**input_), default=str), False
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True

# ========== Agent registry ==========
def _schedule_build_kickoff():
    """Default kickoff for a hub-triggered run: morning summary style."""
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59).astimezone(ZoneInfo("UTC"))
    return (
        f"Current time: {now.isoformat()} {TIMEZONE}.\n"
        f"Run schedule_query(start='{start.isoformat()}', end='{end.isoformat()}', include_at_risk=true) "
        f"and then send_sms with a concise summary of today's Study Blocks plus any at-risk items. "
        f"Keep the message under 320 chars when possible."
    )

AGENTS = {
    "schedule-agent": {
        "name": "Schedule Planner",
        "description": "Motion-style: the solver schedules Study Blocks from Tasks; this agent handles intake + daily summary via iMessage.",
        "agent_id": AGENT_ID,
        "environment_id": ENV_ID,
        "model": "claude-sonnet-4-6",
        "build_kickoff": _schedule_build_kickoff,
        "build_resources": lambda: [],      # no state file needed; state lives in SQLite
        "dispatch_tools": True,
        "pull_state_file": False,
        "on_final": None,
    },
}

if _digest is not None:
    AGENTS["email-digest"] = {
        "name": "Email Digest",
        "description": "Synthesizes the latest finance newsletters (Polcari, PitchBook, Exec Sum, Lion's Eye) into an HTML digest and emails it back to you.",
        "agent_id": os.environ["GMAIL_DIGEST_AGENT_ID"],
        "environment_id": os.environ["GMAIL_DIGEST_ENV_ID"],
        "model": "claude-opus-4-7",
        "build_kickoff": _digest_build_kickoff,
        "build_resources": lambda: [],
        "dispatch_tools": False,
        "pull_state_file": False,
        "on_final": _digest_on_final,
    }

# ========== Session store + pub/sub ==========
@dataclass
class TrackedSession:
    id: str
    agent_key: str
    title: str
    status: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(TZ))
    finished_at: datetime | None = None
    events: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)

SESSIONS: dict = {}  # session_id -> TrackedSession
SESSION_ORDER: deque = deque(maxlen=20)
_agent_locks: dict = {k: threading.Lock() for k in AGENTS}

_main_loop: asyncio.AbstractEventLoop | None = None

def _publish(session: TrackedSession, envelope: dict):
    session.events.append(envelope)
    # Mirror into SQLite so the transcript survives orchestrator restarts.
    # seq is the index we just appended at; ts defaults to now if missing.
    history.append_session_event(
        session_id=session.id,
        seq=len(session.events) - 1,
        ts=envelope.get("ts") or _now_iso(),
        envelope=envelope,
    )
    if _main_loop is None:
        return
    for q in list(session.subscribers):
        try:
            _main_loop.call_soon_threadsafe(q.put_nowait, envelope)
        except Exception:
            pass

def _now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")

# ========== Event processing ==========
_RETRYABLE_STREAM_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.NetworkError,
)

def _process_event(event, tracked, cfg, session_id: str, collected_text_parts: list) -> str:
    """Handle one event. Returns 'done' if the session is terminal, else 'continue'."""
    t = event.type
    if t == "agent.message":
        for b in event.content:
            if b.type == "text":
                collected_text_parts.append(b.text)
                _publish(tracked, {"type": "message", "text": b.text, "ts": _now_iso()})
    elif t == "agent.thinking":
        for b in getattr(event, "content", []) or []:
            txt = getattr(b, "thinking", None)
            if txt:
                _publish(tracked, {"type": "thinking", "text": txt, "ts": _now_iso()})
    elif t == "agent.custom_tool_use":
        if cfg["dispatch_tools"]:
            result, is_err = run_tool(event.name, event.input)
        else:
            result, is_err = json.dumps({"error": f"agent has no tool dispatcher"}), True
        preview = result if len(result) < 600 else result[:600] + "…"
        _publish(tracked, {
            "type": "tool_call", "name": event.name, "args": event.input,
            "result_preview": preview, "result_len": len(result),
            "is_error": is_err, "ts": _now_iso(),
        })
        anthropic.beta.sessions.events.send(
            session_id=session_id,
            events=[{
                "type": "user.custom_tool_result",
                "custom_tool_use_id": event.id,
                "content": [{"type": "text", "text": result}],
                "is_error": is_err,
            }],
        )
    elif t == "session.error":
        _publish(tracked, {"type": "error",
                           "text": event.model_dump_json()[:400],
                           "ts": _now_iso()})
    elif t == "session.status_terminated":
        return "done"
    elif t == "session.status_idle":
        srt = getattr(getattr(event, "stop_reason", None), "type", None)
        if srt != "requires_action":
            return "done"
    return "continue"


def _consume_session(session_id: str, tracked, cfg, collected_text_parts: list,
                     max_reconnects: int = 5):
    """
    Tail a session's events, with lossless reconnect on network disconnects.

    On each (re)connect: replay via events.list() first (dedupe by id), then tail live.
    This covers the stream-has-no-replay gap after RemoteProtocolError etc.
    """
    seen = set()
    reconnects = 0
    while True:
        try:
            # 1) Replay any history we haven't seen (catches events that arrived during a disconnect)
            for hist in anthropic.beta.sessions.events.list(session_id=session_id):
                if hist.id in seen:
                    continue
                seen.add(hist.id)
                if _process_event(hist, tracked, cfg, session_id, collected_text_parts) == "done":
                    return
            # 2) Tail live events
            with anthropic.beta.sessions.events.stream(session_id=session_id) as stream:
                for event in stream:
                    if event.id in seen:
                        continue
                    seen.add(event.id)
                    if _process_event(event, tracked, cfg, session_id, collected_text_parts) == "done":
                        return
            # Stream closed cleanly without a terminal event — loop to replay via list.
            # (Shouldn't normally happen; list() will surface the terminal event.)
        except _RETRYABLE_STREAM_ERRORS as e:
            reconnects += 1
            if reconnects > max_reconnects:
                _publish(tracked, {"type": "error",
                                   "text": f"stream failed after {max_reconnects} reconnects: {type(e).__name__}: {e}",
                                   "ts": _now_iso()})
                raise
            _publish(tracked, {"type": "note",
                               "text": f"stream hiccup ({type(e).__name__}); reconnecting {reconnects}/{max_reconnects}",
                               "ts": _now_iso()})
            time.sleep(min(2 ** reconnects, 15))


# ========== Session runner ==========
def run_session(agent_key: str, kickoff: Optional[str] = None):
    if agent_key not in AGENTS:
        return {"error": f"unknown agent: {agent_key}"}

    cfg = AGENTS[agent_key]
    lock = _agent_locks[agent_key]
    if not lock.acquire(blocking=False):
        if agent_key == "schedule-agent":
            send_sms("Already re-planning - ignoring this trigger.")
        return {"error": "already running"}

    kickoff = kickoff or cfg["build_kickoff"]()
    sid_placeholder = f"pending_{uuid.uuid4().hex[:8]}"
    tracked = TrackedSession(id=sid_placeholder, agent_key=agent_key,
                             title=f"{cfg['name']} {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}")
    SESSIONS[sid_placeholder] = tracked
    SESSION_ORDER.appendleft(sid_placeholder)
    # Persist the session row before the first _publish — session_events has a
    # foreign key to sessions(id), so the row must exist first.
    history.upsert_session(
        session_id=sid_placeholder, agent_key=agent_key, title=tracked.title,
        status="running", started_at=tracked.started_at.isoformat(timespec="seconds"),
    )
    _publish(tracked, {"type": "status", "status": "running", "ts": _now_iso(),
                       "kickoff": kickoff[:500] + ("…" if len(kickoff) > 500 else "")})

    collected_text_parts = []

    try:
        resources = cfg["build_resources"]()

        session = anthropic.beta.sessions.create(
            agent=cfg["agent_id"],
            environment_id=cfg["environment_id"],
            title=tracked.title,
            resources=resources,
        )
        # Re-key by the real session id
        SESSIONS.pop(sid_placeholder, None)
        tracked.id = session.id
        SESSIONS[session.id] = tracked
        try:
            SESSION_ORDER.remove(sid_placeholder)
        except ValueError:
            pass
        SESSION_ORDER.appendleft(session.id)
        # Migrate the persisted row + event history to the real session id.
        history.rename_session(sid_placeholder, session.id)

        # Send the kickoff first (idempotent from the orchestrator's POV — only sent once per run)
        anthropic.beta.sessions.events.send(
            session_id=session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": kickoff}]}],
        )
        _consume_session(session.id, tracked, cfg, collected_text_parts)

        # Pull updated state
        if cfg.get("pull_state_file"):
            time.sleep(2)
            try:
                files = anthropic.beta.files.list(scope_id=session.id, betas=["managed-agents-2026-04-01"])
                for f in files.data:
                    if f.filename == "learning_state.json":
                        anthropic.beta.files.download(f.id).write_to_file(str(STATE_PATH))
                        break
            except Exception as ex:
                _publish(tracked, {"type": "note", "text": f"state retrieval warning: {ex}", "ts": _now_iso()})

        # Post-session hook (e.g., digest → SMTP send)
        if cfg.get("on_final"):
            try:
                final_text = "".join(collected_text_parts).strip()
                result_msg = cfg["on_final"](final_text)
                if result_msg:
                    _publish(tracked, {"type": "note", "text": result_msg, "ts": _now_iso()})
            except Exception as ex:
                _publish(tracked, {"type": "error",
                                   "text": f"on_final: {type(ex).__name__}: {ex}",
                                   "ts": _now_iso()})

        try: anthropic.beta.sessions.archive(session_id=session.id)
        except Exception: pass

        tracked.status = "done"
        tracked.finished_at = datetime.now(TZ)
        _publish(tracked, {"type": "status", "status": "done", "ts": _now_iso()})
        history.upsert_session(
            session_id=tracked.id, agent_key=agent_key, title=tracked.title,
            status="done", started_at=tracked.started_at.isoformat(timespec="seconds"),
            finished_at=tracked.finished_at.isoformat(timespec="seconds"),
        )
    except Exception as e:
        tracked.status = "error"
        tracked.finished_at = datetime.now(TZ)
        err_text = f"{type(e).__name__}: {e}"
        _publish(tracked, {"type": "status", "status": "error",
                           "detail": err_text, "ts": _now_iso()})
        history.upsert_session(
            session_id=tracked.id, agent_key=agent_key, title=tracked.title,
            status="error", started_at=tracked.started_at.isoformat(timespec="seconds"),
            finished_at=tracked.finished_at.isoformat(timespec="seconds"),
            error=err_text,
        )
        if agent_key == "schedule-agent":
            try: send_sms(f"Scheduler error: {err_text}")
            except Exception: pass
    finally:
        lock.release()

# ========== FastAPI ==========
from contextlib import asynccontextmanager  # noqa: E402

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown wiring. Replaces deprecated @app.on_event decorators
    (ROADBLOCKS §F1). Code before `yield` runs at startup; code after runs
    on shutdown — currently nothing because every long-lived resource lives
    in module-scope singletons (BackgroundScheduler, watchdog thread, etc.)
    that the OS reaps on process exit."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    # Kick an initial sync+resolve in the background so the hub shows current
    # tasks immediately (otherwise we'd have to wait for the 15-min poll).
    threading.Thread(
        target=_do_solver_run,
        kwargs={"trigger": "startup", "sync": True},
        daemon=True,
    ).start()
    # First-wake trigger: morning_plan no-ops if already run today.
    threading.Thread(target=morning_plan, daemon=True).start()
    yield

app = FastAPI(lifespan=_lifespan)

def _authed(request: Request, cookie_token: Optional[str]) -> bool:
    key = request.query_params.get("key")
    if key == REPLAN_TOKEN:
        return True
    return cookie_token == REPLAN_TOKEN

def _require_auth(request: Request, cookie_token: Optional[str]):
    if not _authed(request, cookie_token):
        raise HTTPException(403, "forbidden")

# --- Root redirect (friendlier than a bare 404) ---
@app.get("/")
async def root(request: Request):
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/hub{q}", status_code=307)

# --- Hub UI ---
@app.get("/hub", response_class=HTMLResponse)
async def hub(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    if not _authed(request, replan_token):
        raise HTTPException(403, "add ?key=... to the URL")
    # If nothing solved in the last 5 min (Mac probably slept overnight, or the
    # process just restarted), kick a background resolve so the hub renders
    # fresh data the moment the page mounts.
    _kick_stale_resolve(max_age_seconds=300)
    html = HUB_HTML_PATH.read_text()
    resp = HTMLResponse(html)
    if request.query_params.get("key") == REPLAN_TOKEN and replan_token != REPLAN_TOKEN:
        resp.set_cookie("replan_token", REPLAN_TOKEN, httponly=True, samesite="lax", max_age=60*60*24*90)
    return resp


def _kick_stale_resolve(max_age_seconds: int = 300):
    """If the most recent solver run is older than max_age_seconds (or the log
    is empty), fire a background resolve. Never blocks the caller."""
    try:
        if not _SOLVER_LOG:
            stale = True
        else:
            last_ts = _SOLVER_LOG[0].get("ts")
            last_dt = datetime.fromisoformat(last_ts)
            age = (datetime.now(TZ) - last_dt).total_seconds()
            stale = age > max_age_seconds
    except Exception:
        stale = True
    if stale and not _solver_lock.locked():
        threading.Thread(target=_do_solver_run,
                         kwargs={"trigger": "hub_stale", "sync": True},
                         daemon=True).start()

# --- API ---
@app.get("/api/health")
async def providers_health_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    """Snapshot of every configured provider's self-reported health.
    Consumed by the hub UI and by any test agent validating an install."""
    _require_auth(request, replan_token)
    import schedule_config as _cfg
    return _cfg.providers_health()


@app.get("/api/agents")
async def list_agents(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    # Build a 1-deep "last session per agent_key" map. In-memory SESSION_ORDER
    # wins for live sessions; persisted history fills in everything else so the
    # UI shows the most recent run even right after an orchestrator restart.
    persisted = {r["id"]: r for r in history.list_sessions(limit=50)}
    last_by_agent: dict = {}
    for sid in SESSION_ORDER:
        s = SESSIONS.get(sid)
        if not s or s.agent_key in last_by_agent:
            continue
        last_by_agent[s.agent_key] = {
            "id": s.id, "status": s.status,
            "started_at": s.started_at.isoformat(timespec="seconds"),
        }
    for row in persisted.values():
        last_by_agent.setdefault(row["agent_key"], {
            "id": row["id"], "status": row["status"],
            "started_at": row["started_at"],
        })

    out = []
    for key, cfg in AGENTS.items():
        out.append({"key": key, "name": cfg["name"], "description": cfg["description"],
                    "model": cfg["model"], "running": _agent_locks[key].locked(),
                    "last": last_by_agent.get(key)})
    return {"agents": out}

@app.post("/api/agents/{agent_key}/trigger")
async def trigger_agent(agent_key: str, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    if agent_key not in AGENTS:
        raise HTTPException(404, "unknown agent")
    body = {}
    try: body = await request.json()
    except Exception: pass
    kickoff = body.get("kickoff") if isinstance(body, dict) else None

    # Pre-create a pending session id so we can return it immediately and the UI can subscribe.
    pending_id = f"pending_{uuid.uuid4().hex[:8]}"
    tracked = TrackedSession(id=pending_id, agent_key=agent_key,
                             title=f"{AGENTS[agent_key]['name']} {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}")
    SESSIONS[pending_id] = tracked
    SESSION_ORDER.appendleft(pending_id)

    def _wrapped():
        # run_session creates its own pending id; we want to reuse ours so the UI can follow along
        # Instead: publish our pending, then let run_session do its thing and migrate
        # Simplest: just call run_session which will create its own tracked. We drop ours.
        SESSIONS.pop(pending_id, None)
        try: SESSION_ORDER.remove(pending_id)
        except ValueError: pass
        run_session(agent_key, kickoff)

    if _agent_locks[agent_key].locked():
        SESSIONS.pop(pending_id, None)
        try: SESSION_ORDER.remove(pending_id)
        except ValueError: pass
        raise HTTPException(409, f"{agent_key} is already running")
    threading.Thread(target=_wrapped, daemon=True).start()
    return {"ok": True, "pending": True, "note": "A fresh session will appear in the list shortly."}

@app.get("/api/sessions")
async def list_sessions(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    # SQLite is the authoritative store for history. Merge any in-memory
    # sessions that haven't been flushed yet (shouldn't normally happen —
    # every creation path upserts — but belt-and-suspenders for live rows
    # whose status is ahead of the persisted snapshot).
    rows = history.list_sessions(limit=20)
    by_id = {r["id"]: r for r in rows}
    for sid in SESSION_ORDER:
        s = SESSIONS.get(sid)
        if not s:
            continue
        live = {"id": s.id, "agent_key": s.agent_key, "title": s.title,
                "status": s.status,
                "started_at": s.started_at.isoformat(timespec="seconds"),
                "finished_at": s.finished_at.isoformat(timespec="seconds") if s.finished_at else None,
                "event_count": len(s.events)}
        by_id[s.id] = live
    merged = sorted(by_id.values(), key=lambda r: r["started_at"], reverse=True)[:20]
    return {"sessions": merged}

@app.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    sess = SESSIONS.get(session_id)
    if sess is None:
        # Session not in memory — serve the persisted transcript and close.
        stored = history.get_session(session_id)
        if stored is None:
            raise HTTPException(404, "no such session")
        events = history.get_session_events(session_id)

        async def replay_only():
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            yield "event: close\ndata: {}\n\n"
        return StreamingResponse(replay_only(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    async def gen():
        # Replay history
        for ev in sess.events:
            yield f"data: {json.dumps(ev)}\n\n"
        if sess.status != "running":
            yield "event: close\ndata: {}\n\n"
            return
        queue: asyncio.Queue = asyncio.Queue()
        sess.subscribers.append(queue)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev.get("type") == "status" and ev.get("status") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in sess.subscribers:
                sess.subscribers.remove(queue)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# --- Solver (Motion-style planner) ---

def _solver_external_events(start_utc, end_utc):
    """Events the solver should treat as blockers. AUTO-tagged events are our
    own previous chunks — excluded so a re-solve can clear and repopulate."""
    try:
        events = CALENDAR.list_events(start_utc, end_utc)
    except Exception:
        # One retry after clearing any cached connection.
        CALENDAR.reset()
        events = CALENDAR.list_events(start_utc, end_utc)
    return [
        {"uid": e.id, "start": e.start.isoformat(), "end": e.end.isoformat(),
         "title": e.title, "is_auto": False}
        for e in events if not e.is_auto
    ]


# Todo-list name → course code bridge is user-configurable in schedule_config.
# Empty dict is fine; the matcher falls back to substring comparison between
# the todo list name and the task's course field.
from schedule_config import TODO_LIST_TO_COURSE


def _tokens(s: str) -> set:
    """Alphanumeric tokens ≥2 chars, lowercased. Short alpha like 'hw', 'ch', 'ps', 'q1' count."""
    import re
    out = set()
    for w in re.findall(r"[A-Za-z0-9]+", (s or "").lower()):
        if len(w) >= 2:
            out.add(w)
    return out - {"the", "and", "due", "for", "from", "with"}


def _todo_matches_task(todo: TodoItem, task: dict) -> bool:
    """Course-aligned; titles must share ≥1 alpha token AND (if both titles
    contain digits) ≥1 shared digit. See ROADBLOCKS §R4 for why the
    digit-match is critical — prevents "Chapter 14" from matching "Chapter 15".
    """
    list_lower = (todo.list_name or "").lower()
    mapped_course = TODO_LIST_TO_COURSE.get(list_lower)
    task_course = (task.get("course") or "").strip().upper()
    if mapped_course:
        if task_course != mapped_course:
            return False
    else:
        # Fallback: some substring overlap between todo list name and task course
        if not task_course or not any(x in task_course.lower() for x in list_lower.split()):
            return False
    todo_toks = _tokens(todo.title)
    task_toks = _tokens(task.get("title", ""))
    todo_nums = {t for t in todo_toks if t.isdigit()}
    task_nums = {t for t in task_toks if t.isdigit()}
    if not ((todo_toks - todo_nums) & (task_toks - task_nums)):
        return False
    if todo_nums and task_nums and not (todo_nums & task_nums):
        return False
    return True


def sync_tasks_from_sources() -> dict:
    """Pull every configured TaskSource + the TodoSource, upsert/close rows.
    Idempotent. Returns a summary dict for logging."""
    summary: dict = {"sources": {}, "todo_matches": 0, "errors": []}

    # ---- Task sources ----
    for source in TASK_SOURCES:
        src_summary = {"upserted": 0, "marked_done": 0, "errors": []}
        try:
            items = source.list_active_tasks()
        except Exception as ex:
            src_summary["errors"].append(f"{type(ex).__name__}: {ex}")
            summary["sources"][source.SOURCE or type(source).__name__] = src_summary
            continue
        require_approval = bool(getattr(source, "require_approval", False))
        for item in items:
            try:
                if item.is_completed:
                    if tasks_mod.mark_done_by_source(item.source, item.source_id):
                        src_summary["marked_done"] += 1
                    continue
                tasks_mod.upsert_from_source(item, require_approval=require_approval)
                src_summary["upserted"] += 1
            except Exception as ex:
                src_summary["errors"].append(f"{item.source_id}: {ex}")
        summary["sources"][source.SOURCE or type(source).__name__] = src_summary

    # ---- Todo cross-reference ----
    if TODO_SOURCE is None:
        return summary
    try:
        todos = TODO_SOURCE.list_items(include_completed=True)
    except Exception as ex:
        summary["errors"].append(f"todo_source: {ex}")
        return summary

    completed = [t for t in todos if t.completed and t.id]
    if not completed:
        return summary
    completed_uids = {t.id for t in completed}

    import config as _cfg
    with _cfg.connect() as conn:
        prior_matches = list(conn.execute(
            "SELECT reminder_uid, task_id FROM reminder_matches"
        ).fetchall())
    # Re-apply still-completed prior matches so upstream "still open" doesn't
    # strand a legitimately-done task as 'scheduled'. (ROADBLOCKS §R4)
    for row in prior_matches:
        if row["reminder_uid"] in completed_uids:
            tasks_mod.mark_complete(row["task_id"])
            summary["todo_matches"] += 1
    already_matched = {(row["reminder_uid"], row["task_id"]) for row in prior_matches}

    active = tasks_mod.list_active()
    with _cfg.connect() as conn:
        for todo in completed:
            for task in active:
                if (todo.id, task["id"]) in already_matched:
                    continue
                if _todo_matches_task(todo, task):
                    tasks_mod.mark_complete(task["id"])
                    conn.execute(
                        "INSERT OR IGNORE INTO reminder_matches(reminder_uid, task_id) VALUES(?,?)",
                        (todo.id, task["id"]),
                    )
                    already_matched.add((todo.id, task["id"]))
                    summary["todo_matches"] += 1
                    break
    return summary


def _solver_writer(start_utc, end_utc, chunks) -> dict:
    """Solver writer hook.

    Every run:
    1. Wipes ALL AUTO-tagged events written by the solver in [-60d, window_end)
       — catches stale past chunks and future chunks about to be replaced.
    2. Additionally wipes any NON-AUTO event on the WRITE calendar in the PAST
       (before start_utc) — cruft from a pre-Motion era or expired blocks the
       user never marked done. Still-active tasks they covered get re-placed
       by the solver naturally. Future non-AUTO events are preserved — the
       user may have placed them manually.
    Uses the CalendarProvider interface; never touches CalDAV/iCal directly.
    """
    wide_start = start_utc - timedelta(days=60)
    summary = {"deleted": 0, "legacy_deleted": 0, "created": 0,
               "delete_errors": [], "create_errors": []}

    # List everything on our write calendar in the wipe range.
    try:
        existing = CALENDAR.list_events(wide_start, end_utc,
                                        include_read_calendars=False)
    except Exception as ex:
        CALENDAR.reset()
        try:
            existing = CALENDAR.list_events(wide_start, end_utc,
                                            include_read_calendars=False)
        except Exception as ex2:
            summary["list_error"] = f"{type(ex2).__name__}: {ex2}"
            existing = []

    # Delete AUTO events (anywhere in the wipe range) and past non-AUTO events.
    for ev in existing:
        should_delete = ev.is_auto or (not ev.is_auto and ev.end <= start_utc)
        if not should_delete:
            continue
        try:
            CALENDAR.delete_event(ev)
            if ev.is_auto:
                summary["deleted"] += 1
            else:
                summary["legacy_deleted"] += 1
        except Exception as ex:
            summary["delete_errors"].append(f"{ev.id[:8]}: {ex}")

    # Clear the ledger rows for this window.
    tasks_mod.clear_chunks_in_window(
        wide_start.astimezone(ZoneInfo("UTC")).isoformat(),
        end_utc.astimezone(ZoneInfo("UTC")).isoformat(),
    )

    # Create new AUTO events from the solver's chunks.
    per_task: dict = {}
    for c in chunks:
        if hasattr(c, "start"):                       # solver.Chunk dataclass
            cs, ce, ctitle, cnotes, ctask_id = c.start, c.end, c.title, (c.notes or ""), c.task_id
        else:                                          # dict fallback
            cs = datetime.fromisoformat(c["start"].replace("Z", "+00:00"))
            ce = datetime.fromisoformat(c["end"].replace("Z", "+00:00"))
            ctitle, cnotes, ctask_id = c["title"], (c.get("notes") or ""), c["task_id"]
        try:
            new_ev = CALENDAR.create_auto_event(
                start_utc=cs, end_utc=ce, title=ctitle,
                notes=cnotes, task_id=ctask_id,
            )
        except Exception as ex:
            summary["create_errors"].append(f"{ctitle[:40]}: {ex}")
            continue
        summary["created"] += 1
        per_task.setdefault(ctask_id, []).append((
            new_ev.id,
            new_ev.start.astimezone(ZoneInfo("UTC")).isoformat(),
            new_ev.end.astimezone(ZoneInfo("UTC")).isoformat(),
        ))

    for tid, rows in per_task.items():
        tasks_mod.record_chunks(tid, rows)

    return summary


# Rolling log of solver runs — visible in the hub via /api/solver/log.
_SOLVER_LOG: deque = deque(maxlen=30)
_solver_lock = threading.Lock()
_LAST_AT_RISK: set = set()  # task_ids flagged at risk on the last solver run

def _do_solver_run(trigger: str = "manual", sync: bool = True) -> dict:
    """Single source of truth for solver runs: sync + resolve, write result to _SOLVER_LOG."""
    if not _solver_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "another solver run in progress"}
    try:
        started_at = datetime.now(TZ)
        sync_summary = sync_tasks_from_sources() if sync else None
        try:
            result = solver_mod.resolve(
                window_days=14,
                external_events_fn=_solver_external_events,
                writer_fn=_solver_writer,
            )
            _LAST_AT_RISK.clear()
            _LAST_AT_RISK.update(r.task_id for r in result.at_risk)
            entry = {
                "trigger": trigger,
                "ts": started_at.isoformat(timespec="seconds"),
                "chunks": len(result.chunks),
                "at_risk": len(result.at_risk),
                "at_risk_task_ids": list(_LAST_AT_RISK),
                "scheduled_min": result.scheduled_minutes,
                "free_min": result.free_slot_minutes_total,
                "sync_summary": sync_summary,
                "write": result.__dict__.get("write_summary"),
            }
        except Exception as ex:
            entry = {
                "trigger": trigger,
                "ts": started_at.isoformat(timespec="seconds"),
                "error": f"{type(ex).__name__}: {ex}",
                "sync_summary": sync_summary,
            }
        _SOLVER_LOG.appendleft(entry)
        history.record_solver_run(entry)
        return entry
    finally:
        _solver_lock.release()


@app.get("/api/solver/resolve")
@app.post("/api/solver/resolve")
async def solver_resolve_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    dry_run = request.query_params.get("dry_run", "0") not in ("0", "false", "False", "no")
    window_days = int(request.query_params.get("window_days", "14"))
    skip_sync = request.query_params.get("skip_sync", "0") in ("1", "true", "yes")

    if dry_run:
        # Dry-run: no write, no log entry, no lock — cheap preview.
        sync_summary = None
        if not skip_sync:
            try:
                sync_summary = sync_tasks_from_sources()
            except Exception as ex:
                sync_summary = {"error": f"{type(ex).__name__}: {ex}"}
        try:
            result = solver_mod.resolve(
                window_days=window_days,
                external_events_fn=_solver_external_events,
                writer_fn=None,
            )
        except Exception as ex:
            return JSONResponse({"error": f"{type(ex).__name__}: {ex}"}, status_code=500)
        out = result.to_dict()
        if sync_summary is not None:
            out["sync_summary"] = sync_summary
        return out

    # Live run: go through the common lock + log path.
    entry = _do_solver_run(trigger="manual", sync=not skip_sync)
    return entry


@app.get("/api/solver/log")
async def solver_log_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "30")), 200))
    except ValueError:
        limit = 30
    # SQLite is authoritative. The in-memory deque is retained as a live cache
    # for this process, but history.list_solver_runs also includes entries
    # written by prior runs that crashed / restarted.
    return {"runs": history.list_solver_runs(limit=limit)}


# ---------- Tasks REST API ----------

@app.get("/api/tasks")
async def tasks_list_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    status = request.query_params.get("status")
    if status == "all":
        rows = tasks_mod.list_all()
    elif status:
        rows = tasks_mod.list_all(status_filter=status.split(","))
    else:
        rows = tasks_mod.list_active()
    chunks_by_task = {}
    for c in tasks_mod.list_chunks():
        chunks_by_task.setdefault(c["task_id"], []).append(c)
    # Attach at-risk flag and chunks to each task
    enriched = []
    for t in rows:
        t = dict(t)
        t["at_risk"] = t["id"] in _LAST_AT_RISK
        t["chunks"] = chunks_by_task.get(t["id"], [])
        enriched.append(t)
    return {"tasks": enriched, "total": len(enriched)}


@app.post("/api/tasks")
async def tasks_create_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    body = await request.json()
    try:
        t = tasks_mod.create(
            title=body["title"],
            duration_min=int(body["duration_min"]),
            deadline_ts=body.get("deadline_ts"),
            priority=body.get("priority", "medium"),
            course=body.get("course"),
            min_chunk_min=int(body.get("min_chunk_min") or 30),
            max_chunk_min=int(body.get("max_chunk_min") or 120),
            preferred_window=body.get("preferred_window"),
            source=body.get("source", "manual"),
            source_id=body.get("source_id"),
            notes=body.get("notes"),
        )
    except Exception as ex:
        raise HTTPException(400, f"{type(ex).__name__}: {ex}")
    # Kick a re-solve asynchronously so the UI returns quickly.
    threading.Thread(target=_do_solver_run, kwargs={"trigger": "task_create", "sync": False}, daemon=True).start()
    return {"task": t}


# NOTE: /api/tasks/pending must be registered BEFORE /api/tasks/{task_id} so
# FastAPI does not swallow "pending" as an integer task_id (would 422).
@app.get("/api/tasks/pending")
async def tasks_pending_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    """Tasks held in pending_review — waiting for the user to approve or
    reject before the solver will consider them. Populated only when a
    TaskSource has require_approval=True."""
    _require_auth(request, replan_token)
    rows = tasks_mod.list_pending_review()
    return {"tasks": rows, "total": len(rows)}


@app.patch("/api/tasks/{task_id}")
async def tasks_update_endpoint(task_id: int, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    body = await request.json()
    t = tasks_mod.update(task_id, **body)
    if t is None:
        raise HTTPException(404, "not found")
    threading.Thread(target=_do_solver_run, kwargs={"trigger": "task_update", "sync": False}, daemon=True).start()
    return {"task": t}


@app.post("/api/tasks/{task_id}/complete")
async def tasks_complete_endpoint(task_id: int, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    body = {}
    try: body = await request.json()
    except Exception: pass
    actual = body.get("actual_min")
    t = tasks_mod.mark_complete(task_id, actual_min=int(actual) if actual else None)
    if t is None:
        raise HTTPException(404, "not found")
    threading.Thread(target=_do_solver_run, kwargs={"trigger": "task_complete", "sync": False}, daemon=True).start()
    return {"task": t}


@app.delete("/api/tasks/{task_id}")
async def tasks_delete_endpoint(task_id: int, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    ok = tasks_mod.delete(task_id)
    if not ok:
        raise HTTPException(404, "not found")
    threading.Thread(target=_do_solver_run, kwargs={"trigger": "task_delete", "sync": False}, daemon=True).start()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/approve")
async def tasks_approve_endpoint(task_id: int, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    t = tasks_mod.approve(task_id)
    if t is None:
        raise HTTPException(404, "not found")
    threading.Thread(target=_do_solver_run, kwargs={"trigger": "task_approve", "sync": False}, daemon=True).start()
    return {"task": t}


@app.post("/api/tasks/{task_id}/reject")
async def tasks_reject_endpoint(task_id: int, request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    t = tasks_mod.reject(task_id)
    if t is None:
        raise HTTPException(404, "not found")
    return {"task": t}


# --- Schedule read (fast: no solver invocation, no calendar network) ---
# /api/solver/resolve?dry_run=1 re-plans on demand (slow). The native iPhone +
# Mac apps want a cheap, frequent read for the "Today" view, so this endpoint
# returns the cached chunks already in the scheduled_chunks table joined with
# their task titles. The window is whatever the caller asks for; default is
# the next 7 days starting now.
@app.get("/api/schedule")
async def schedule_endpoint(request: Request, replan_token: Optional[str] = Cookie(default=None)):
    _require_auth(request, replan_token)
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    qp = request.query_params

    def _parse(name: str, default: datetime) -> datetime:
        raw = qp.get(name)
        if not raw:
            return default
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"{name} must be ISO 8601, got {raw!r}")

    start = _parse("start", now)
    end = _parse("end", now + timedelta(days=7))
    if end <= start:
        raise HTTPException(400, "end must be after start")

    # Query scheduled_chunks joined with tasks. Cheap: indexed on (start_ts, end_ts).
    with _config_mod.connect() as conn:
        rows = conn.execute(
            """SELECT sc.task_id, sc.start_ts, sc.end_ts, t.title, t.notes
               FROM scheduled_chunks sc
               JOIN tasks t ON t.id = sc.task_id
               WHERE sc.start_ts >= ? AND sc.start_ts < ?
               ORDER BY sc.start_ts ASC""",
            (start.isoformat().replace("+00:00", "Z"),
             end.isoformat().replace("+00:00", "Z")),
        ).fetchall()
    chunks = []
    for r in rows:
        s = datetime.fromisoformat(r["start_ts"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(r["end_ts"].replace("Z", "+00:00"))
        chunks.append({
            "task_id": r["task_id"],
            "title": r["title"],
            "start": r["start_ts"],
            "end": r["end_ts"],
            "duration_min": int((e - s).total_seconds() / 60),
            "notes": r["notes"] or "",
        })

    # at-risk list comes from the most recent solver run's snapshot.
    at_risk = []
    if _LAST_AT_RISK:
        # Hydrate task titles + missing-min summary by reading the latest
        # solver_log entry; that's where the per-task duration_needed lives.
        recent = history.list_solver_runs(limit=1)
        if recent and isinstance(recent[0], dict):
            entry = recent[0]
            for risk_id in _LAST_AT_RISK:
                t = tasks_mod.get(int(risk_id))
                if t is None:
                    continue
                at_risk.append({
                    "task_id": t["id"],
                    "title": t["title"],
                    "duration_needed_min": int(t["duration_min"]),
                    "duration_placed_min": 0,  # detailed split not retained; 0 is "unknown placed"
                    "reason": "see at-risk list from latest solver run",
                })

    return {
        "now_utc": now.isoformat().replace("+00:00", "Z"),
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": end.isoformat().replace("+00:00", "Z"),
        "chunks": chunks,
        "at_risk": at_risk,
    }


# --- iOS Shortcut (kept for backwards compat) ---
@app.post("/replan")
async def replan(request: Request):
    key = request.query_params.get("key") or request.headers.get("x-replan-token", "")
    if key != REPLAN_TOKEN:
        raise HTTPException(403, "forbidden")
    if _agent_locks["schedule-agent"].locked():
        send_sms("Already re-planning - ignoring this trigger.")
        return {"ok": True, "status": "already_running"}
    threading.Thread(target=run_session, args=("schedule-agent",), daemon=True).start()
    return {"ok": True, "status": "replanning"}

# --- Morning run (fires on first wake of the day, not a fixed hour) ---
def morning_plan(force: bool = False):
    """Sync + solve, then kick schedule-agent summary + email-digest in parallel.
    Idempotent per calendar day via MORNING_MARKER_PATH — safe to call from
    startup and poll. `force=True` bypasses the date guard (manual re-run)."""
    today = datetime.now(TZ).date().isoformat()
    if not force:
        try:
            if MORNING_MARKER_PATH.read_text().strip() == today:
                return
        except FileNotFoundError:
            pass
    MORNING_MARKER_PATH.write_text(today)

    def _run():
        try:
            solver_mod.resolve(
                window_days=14,
                external_events_fn=_solver_external_events,
                writer_fn=_solver_writer,
            )
        except Exception as e:
            try: send_sms(f"Morning solver error: {type(e).__name__}: {e}")
            except Exception: pass
        run_session("schedule-agent")
    threading.Thread(target=_run, daemon=True).start()
    if "email-digest" in AGENTS:
        threading.Thread(target=run_session, args=("email-digest",), daemon=True).start()

# Background solver poll: every 15 minutes, re-solve + sync, and fire morning
# if it hasn't run today yet (covers the "Mac woke mid-morning" case when the
# orchestrator stayed up through sleep and no startup handler re-ran).
def _poll_solver():
    _do_solver_run(trigger="poll", sync=True)
    morning_plan()


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(_poll_solver, "interval", minutes=15, id="solver_poll",
                  coalesce=True, max_instances=1)
scheduler.start()


# --- Wake watchdog (replaces the hub-load-kick workaround for ROADBLOCKS §L1) ---
# The Mac (or any host) sleeps; APScheduler's interval timer pauses with it,
# so the next poll fires on the regular 15-min boundary after wake. The
# watchdog checks every 30s for monotonic-vs-wallclock drift; when wallclock
# jumps ahead by >60s we know the host just resumed, and we kick a resolve
# right away.
import wake_watchdog  # noqa: E402

def _on_wake(slept_seconds: float):
    threading.Thread(
        target=_do_solver_run,
        kwargs={"trigger": "wake", "sync": True},
        daemon=True,
    ).start()
    threading.Thread(target=morning_plan, daemon=True).start()

wake_watchdog.start_watchdog(_on_wake)

if __name__ == "__main__":
    import uvicorn
    # Bind all interfaces so Tailscale (100.x) can reach us alongside localhost.
    # REPLAN_TOKEN on every endpoint gates access.
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")
