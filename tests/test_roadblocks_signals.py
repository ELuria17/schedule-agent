"""Roadblocks "Test signal" runner — closes ROADBLOCKS Open Follow-up #10.

Parses every `**Test signal:**` line in `ROADBLOCKS.md` and pairs it with a
handler. Three outcomes per signal:

- **Handler runs**, asserts the invariant holds → PASS / FAIL.
- **Handler is `None`** because the signal needs live network, real
  credentials, or a manual interaction → SKIP with the original signal text
  surfaced as the skip reason.
- **Section has no signal line** (a few don't) → not collected at all.

The coverage test below catches the case where someone adds a new section to
`ROADBLOCKS.md` without registering a handler or an explicit skip — that's
the regression-mode this file exists for.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


# ---------- Handler registry ----------

HANDLERS: dict[str, "object | None"] = {}


def signal(section_id: str):
    def deco(fn):
        HANDLERS[section_id] = fn
        return fn
    return deco


def skip(section_id: str, reason: str):
    """Register that a signal is intentionally not auto-runnable."""
    HANDLERS[section_id] = ("skip", reason)


# ---------- Auto-runnable signals ----------

@signal("A1")
def _a1():
    """Task mutation triggers a solver run within ~5 seconds."""
    # Verified by orchestrator's task_create_tool path: each task tool calls
    # _auto_resolve() which routes through _do_solver_run. We grep the wiring
    # rather than spinning up the full orchestrator.
    src = (REPO / "orchestrator.py").read_text()
    assert "_auto_resolve" in src and "task_mutation" in src


@signal("A2")
def _a2():
    """Agent session events contain only task_*, schedule_query, send_sms.
    Verified by inspecting the registered tool schema in setup.py."""
    from setup import TOOLS
    custom = [t["name"] for t in TOOLS if t.get("type") == "custom"]
    bad = [n for n in custom if n.startswith("calendar_")]
    assert not bad, f"calendar_* tool present: {bad}"
    allowed_prefixes = ("task_", "schedule_", "send_")
    rogue = [n for n in custom if not n.startswith(allowed_prefixes)]
    assert not rogue, f"rogue tools: {rogue}"


@signal("A3")
def _a3():
    """`/api/agents` returns both schedule-agent and email-digest without
    code duplication. Verified by checking the AGENTS dict keys."""
    src = (REPO / "orchestrator.py").read_text()
    assert "AGENTS = {" in src or "AGENTS: dict" in src
    assert '"schedule-agent"' in src and '"email-digest"' in src


@signal("E1")
def _e1():
    """Python ≥3.9 with zoneinfo + sqlite3 + asyncio."""
    assert sys.version_info >= (3, 9)
    from zoneinfo import ZoneInfo  # noqa: F401
    import sqlite3, asyncio  # noqa: F401


@signal("E2")
def _e2():
    """No PEP-604 union syntax (`X | Y`) in endpoint signatures.
    `from __future__ import annotations` defers evaluation, but FastAPI
    re-evaluates parameter annotations at decoration time."""
    src = (REPO / "orchestrator.py").read_text()
    # Look at lines under @app.<verb>(...) decorators
    in_endpoint = False
    for line in src.splitlines():
        if line.startswith("@app."):
            in_endpoint = True
            continue
        if in_endpoint and line.startswith("def ") or line.startswith("async def "):
            assert "str | None" not in line, f"PEP-604 in endpoint: {line!r}"
            in_endpoint = False
        elif not line.strip():
            in_endpoint = False


@signal("E3")
def _e3():
    """`dotenv.dotenv_values()` strips the wrapping quotes that `set_key` adds."""
    import tempfile
    from dotenv import set_key, dotenv_values
    with tempfile.NamedTemporaryFile("w+", suffix=".env", delete=False) as fh:
        path = fh.name
    set_key(path, "SOME_KEY", "raw value")
    assert dotenv_values(path)["SOME_KEY"] == "raw value"


@signal("C3")
def _c3():
    """Course-code regex extracts e.g. `MGMT 301` from a Canvas course name."""
    pattern = re.compile(r"([A-Z]{3,5})\s*(\d{3,4})")
    m = pattern.search("MGMT 301, Section 008: Basic Mgmt Concept")
    assert m and m.group(0).replace(" ", "") == "MGMT301"


@signal("S1")
def _s1():
    """Deterministic scheduling: solver re-runs are reproducible. Same inputs
    → same chunks. Verified end-to-end by the existing solver tests; here we
    just spot-check determinism on a fixed task list."""
    from datetime import datetime, timezone
    from solver import Interval, place
    now = datetime(2026, 4, 21, 9, tzinfo=timezone.utc)
    base_slots = lambda: [Interval(
        datetime(2026, 4, 21, 10, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 12, tzinfo=timezone.utc))]
    task = {"id": 1, "title": "x", "duration_min": 60,
            "min_chunk_min": 30, "max_chunk_min": 60, "priority": "medium"}
    a, _ = place([task], base_slots(), now)
    b, _ = place([task], base_slots(), now)
    assert [c.start for c in a] == [c.start for c in b]


@signal("S2")
def _s2():
    """Overdue tasks ARE placed (not refused). The solver treats a past
    deadline as 'unbounded for placement purposes'."""
    from datetime import datetime, timezone
    from solver import Interval, place
    now = datetime(2026, 4, 21, 9, tzinfo=timezone.utc)
    slots = [Interval(
        datetime(2026, 4, 21, 10, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 11, tzinfo=timezone.utc))]
    overdue = {"id": 1, "title": "late", "duration_min": 60,
               "min_chunk_min": 30, "max_chunk_min": 60, "priority": "high",
               "deadline_ts": "2026-04-20T00:00:00Z"}  # yesterday
    chunks, _ = place([overdue], slots, now)
    assert len(chunks) == 1


@signal("S3")
def _s3():
    """No sliver chunks below `min_chunk_min`."""
    from datetime import datetime, timezone
    from solver import Interval, place
    now = datetime(2026, 4, 21, 9, tzinfo=timezone.utc)
    # 75-minute slot, min_chunk=60 → first 60-min chunk fills slot, leaving 15
    # min remainder. The solver must NOT create a 15-min sliver.
    slots = [Interval(
        datetime(2026, 4, 21, 10, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 11, 15, tzinfo=timezone.utc))]
    task = {"id": 1, "title": "x", "duration_min": 90,
            "min_chunk_min": 60, "max_chunk_min": 60, "priority": "medium"}
    chunks, _ = place([task], slots, now)
    assert all(c.duration_min >= 60 for c in chunks)


@signal("S4")
def _s4():
    """Local-time math doesn't lose minutes at TZ boundaries. Verified by
    `_day_local_window` round-tripping any HH:MM cleanly."""
    from datetime import date
    from solver import _day_local_window
    iv = _day_local_window(date(2026, 4, 21), "10:00", "11:00")
    assert iv.duration_min == 60


@signal("S5")
def _s5():
    """Priority for overdue-but-open: an asap-tier overdue task should outrank
    a low-priority well-future task in placement order."""
    from datetime import datetime, timezone
    from solver import priority_score
    now = datetime(2026, 4, 21, 12, tzinfo=timezone.utc)
    overdue_asap = {"priority": "asap", "deadline_ts": "2026-04-20T00:00:00Z"}
    future_low = {"priority": "low", "deadline_ts": "2026-05-21T00:00:00Z"}
    assert priority_score(overdue_asap, now) > priority_score(future_low, now)


@signal("F1")
def _f1():
    """`@app.on_event("startup")` is deprecated → migrated to `lifespan`."""
    src = (REPO / "orchestrator.py").read_text()
    assert "@app.on_event" not in src
    assert "lifespan=" in src or "asynccontextmanager" in src


@signal("F2")
def _f2():
    """FastAPI re-evaluates annotations at decoration time, so PEP-604 unions
    in endpoint signatures break under Python 3.9. Same check as E2 — keep
    it independent so a regression that re-introduces them trips both."""
    _e2()


@signal("F3")
def _f3():
    """Cross-thread asyncio.Queue.put_nowait MUST go through
    call_soon_threadsafe; otherwise the event loop never sees it."""
    src = (REPO / "orchestrator.py").read_text()
    # The publish path lives in _publish — check the right idiom is used.
    assert "call_soon_threadsafe" in src
    # And that put_nowait isn't called bare from a thread context.
    bad = re.findall(r"\.put_nowait\(", src)
    # Allow the one inside the call_soon_threadsafe call — it's wrapped.
    # Sanity: every put_nowait should be inside a call_soon_threadsafe call.
    for m in re.finditer(r"\.put_nowait\(", src):
        # 80-char window before the match should mention call_soon_threadsafe.
        window = src[max(0, m.start() - 80):m.start()]
        assert "call_soon_threadsafe" in window, (
            f"put_nowait without call_soon_threadsafe wrapper near "
            f"{src[max(0, m.start()-40):m.start()+40]!r}"
        )


@signal("F4")
def _f4():
    """SQLite doesn't accept `NULLS LAST`. Use `ORDER BY (col IS NULL), col`."""
    for f in REPO.glob("**/*.py"):
        if "venv" in f.parts:
            continue
        text = f.read_text()
        assert "NULLS LAST" not in text and "NULLS FIRST" not in text, f


@signal("L2")
def _l2():
    """Sessions persist in SQLite, surviving orchestrator restart.
    Closed by ROADBLOCKS Open Follow-up #7 (history.upsert_session)."""
    import history
    assert callable(history.upsert_session)
    assert callable(history.append_session_event)
    assert callable(history.list_sessions)


# ---------- Skips with rationale ----------

skip("I1", "Needs real iCloud credentials + network — runs in user's manual smoke test.")
skip("I2", "Needs real iCloud account + a write-calendar event.")
skip("I3", "Needs real iCloud server (412 behavior is server-specific).")
skip("I4", "Needs a CalDAV server with VTODO support.")
skip("I5", "Tested by tests/test_caldav_provider.py::TestRetryOnReconnect.")
skip("I6", "Needs live calendar with legacy untagged events.")
skip("I7", "Needs live calendar; covered manually after a deploy.")
skip("R1", "Needs macOS + Reminders.app data + the compiled Swift binary.")
skip("R2", "Needs macOS + Reminders.app to compare timings.")
skip("R3", "Needs macOS TCC prompt — first-run only.")
skip("R4", "Needs Reminders.app + a paired Canvas task — manual smoke.")
skip("C1", "Needs a real Canvas account.")
skip("C2", "No automatic check possible — Canvas API limitation.")
skip("D1", "Same as R4 — needs Reminders + Canvas pairing.")
skip("D2", "Needs a Canvas reopen cycle to observe.")
skip("D3", "Needs a real Reminders list whose name maps to a course.")
skip("D4", "Needs a calendar with pre-AUTO legacy events.")
skip("L1", "Needs an actual host sleep cycle to observe; covered by wake_watchdog tests.")
skip("L3", "cloudflared deprecated in favor of tailscale; signal kept as historical record.")
skip("M1", "Twilio integration is no longer the default notifier.")
skip("M2", "iMessage-to-self push behavior is a platform property — not testable.")
skip("M3", "macOS TCC prompt — manual.")
skip("N1", "Needs an Anthropic API key + network.")
skip("N2", "Needs a live Anthropic session to observe an event.")
skip("N3", "Tested by orchestrator's _consume_session reconnect loop; live network needed.")
skip("N4", "Needs a real agent_id + Anthropic API.")
skip("N5", "Billing limit — observed by hitting the limit, not by assertion.")
skip("T1", "cloudflared deprecated; tunnel layer is now Tailscale.")
skip("T2", "Tested operationally via scripts/enable_tailscale_https.py.")
skip("T3", "Needs an HTTP request against a running orchestrator.")


# ---------- Parser ----------

def _parse_signals() -> list[tuple[str, str]]:
    """Return [(section_id, signal_text), ...] from ROADBLOCKS.md."""
    text = (REPO / "ROADBLOCKS.md").read_text().splitlines()
    out: list[tuple[str, str]] = []
    current = None
    for line in text:
        m = re.match(r"^### ([A-Z]+\d+)\.", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^- \*\*Test signal:\*\*\s*(.+)$", line)
        if m and current:
            out.append((current, m.group(1).strip()))
    return out


SIGNALS = _parse_signals()
SIGNAL_IDS = [s for s, _ in SIGNALS]


# ---------- Pytest entry ----------

@pytest.mark.parametrize(("section", "signal_text"), SIGNALS, ids=SIGNAL_IDS)
def test_roadblock_signal(section: str, signal_text: str):
    handler = HANDLERS.get(section)
    if handler is None:
        pytest.fail(
            f"section {section} has a Test signal but no registered handler "
            f"or skip(...) entry. Add one to test_roadblocks_signals.py.\n"
            f"Signal: {signal_text}"
        )
    if isinstance(handler, tuple) and handler[0] == "skip":
        pytest.skip(f"{handler[1]}\nSignal: {signal_text}")
    handler()  # type: ignore[operator]


def test_signal_count_sanity():
    """Tripwire: if someone adds/removes a signal, this number changes."""
    assert len(SIGNALS) == 39, (
        f"ROADBLOCKS.md signal count changed (was 39, now {len(SIGNALS)}). "
        f"Update handlers and bump this number."
    )
