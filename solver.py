"""
Deterministic constraint solver. Takes Tasks + config blocks + iCloud events,
produces scheduled Chunks over a forward window (default 14 days).

Phase 2 (shadow mode): computes chunks, never writes to iCloud.
Phase 3 will add the write path via `replace_auto_window`.

All internal times are timezone-aware UTC datetimes. Caller passes either
naive datetimes (assumed UTC) or any tz-aware datetime; we normalize.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import config
import tasks as tasks_mod

TZ = ZoneInfo(config.TZ_NAME)

# ---------- Types ----------

@dataclass
class Interval:
    start: datetime  # UTC, aware
    end: datetime    # UTC, aware

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def __repr__(self) -> str:
        return f"[{self.start.isoformat()} → {self.end.isoformat()} ({self.duration_min}m)]"


@dataclass
class Chunk:
    task_id: int
    title: str
    start: datetime
    end: datetime
    notes: str = ""

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_min": self.duration_min,
            "notes": self.notes,
        }


@dataclass
class AtRisk:
    task_id: int
    title: str
    duration_needed_min: int
    duration_placed_min: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResolveResult:
    chunks: list[Chunk] = field(default_factory=list)
    at_risk: list[AtRisk] = field(default_factory=list)
    now_utc: str = ""
    window_start: str = ""
    window_end: str = ""
    free_slot_minutes_total: int = 0
    scheduled_minutes: int = 0

    def to_dict(self) -> dict:
        d = {
            "now_utc": self.now_utc,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "free_slot_minutes_total": self.free_slot_minutes_total,
            "scheduled_minutes": self.scheduled_minutes,
            "chunks": [c.to_dict() for c in self.chunks],
            "at_risk": [r.to_dict() for r in self.at_risk],
        }
        if "write_summary" in self.__dict__:
            d["write_summary"] = self.__dict__["write_summary"]
        return d


# ---------- Time helpers ----------

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local(dt: datetime) -> datetime:
    return _ensure_utc(dt).astimezone(TZ)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _day_local_window(d: date, start_hm: str, end_hm: str) -> Interval:
    """Given a local date and start/end 'HH:MM', return UTC Interval."""
    s_local = datetime.combine(d, _parse_hhmm(start_hm), tzinfo=TZ)
    e_local = datetime.combine(d, _parse_hhmm(end_hm), tzinfo=TZ)
    return Interval(start=s_local.astimezone(timezone.utc),
                    end=e_local.astimezone(timezone.utc))


def _byday_matches(rrule: Optional[str], d: date) -> bool:
    """Supports 'FREQ=WEEKLY;BYDAY=MO,WE,FR' style. Other patterns fall back to True (always)."""
    if not rrule:
        return True
    parts = dict(kv.split("=", 1) for kv in rrule.split(";") if "=" in kv)
    byday = parts.get("BYDAY")
    if not byday:
        return True
    mapping = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    allowed = {mapping[x] for x in byday.split(",") if x in mapping}
    return d.weekday() in allowed


# ---------- Free-slot computation ----------

def _subtract(base: Interval, blockers: list[Interval]) -> list[Interval]:
    """Subtract blocker intervals from base. Blockers need not be sorted."""
    if not blockers:
        return [base] if base.end > base.start else []
    blockers = sorted((b for b in blockers if b.end > base.start and b.start < base.end),
                      key=lambda b: b.start)
    out: list[Interval] = []
    cursor = base.start
    for b in blockers:
        if b.start > cursor:
            out.append(Interval(cursor, min(b.start, base.end)))
        if b.end > cursor:
            cursor = max(cursor, b.end)
        if cursor >= base.end:
            break
    if cursor < base.end:
        out.append(Interval(cursor, base.end))
    return [iv for iv in out if iv.duration_min > 0]


def free_slots(
    start_utc: datetime,
    end_utc: datetime,
    external_events: Optional[list[dict]] = None,
    hard_block_kinds: Iterable[str] = ("class", "sleep", "shabbat"),
    now_utc: Optional[datetime] = None,
) -> list[Interval]:
    """
    Compute free-work slots in [start_utc, end_utc). Subtracts:
      1. Time outside daily working hours (config_hours).
      2. Config blocks (class schedule, etc).
      3. External (iCloud) events — expected shape like calendar_list_events.
         Caller should pass events that are NOT AUTO-tagged (so we don't
         exclude our own scheduled chunks when re-planning).

    `now_utc` (if given) floors the start to now — no slots in the past.
    """
    start_utc = _ensure_utc(start_utc)
    end_utc = _ensure_utc(end_utc)
    if now_utc is not None:
        start_utc = max(start_utc, _ensure_utc(now_utc))
    if end_utc <= start_utc:
        return []

    working_hours = config.get_working_hours()  # {dow: (start_hm, end_hm)}
    blocks = config.get_blocks(kinds=list(hard_block_kinds))

    # Build the per-day working windows (in UTC) spanning start..end
    day_windows: list[Interval] = []
    cursor_local = _local(start_utc).date()
    end_local_date = _local(end_utc).date()
    while cursor_local <= end_local_date:
        dow = cursor_local.weekday()
        if dow in working_hours:
            s_hm, e_hm = working_hours[dow]
            w = _day_local_window(cursor_local, s_hm, e_hm)
            # Clip to [start_utc, end_utc)
            clipped_start = max(w.start, start_utc)
            clipped_end = min(w.end, end_utc)
            if clipped_end > clipped_start:
                day_windows.append(Interval(clipped_start, clipped_end))
        cursor_local = cursor_local + timedelta(days=1)

    # Collect blockers: config_blocks expanded to each day they apply + external events
    blockers: list[Interval] = []

    # Expand config_blocks across the date range
    cursor_local = _local(start_utc).date()
    while cursor_local <= end_local_date:
        for b in blocks:
            if _byday_matches(b["rrule"], cursor_local):
                bi = _day_local_window(cursor_local, b["start_local"], b["end_local"])
                if bi.end > start_utc and bi.start < end_utc:
                    blockers.append(bi)
        cursor_local = cursor_local + timedelta(days=1)

    # External events — clip to range
    if external_events:
        for ev in external_events:
            try:
                s = _ensure_utc(datetime.fromisoformat(str(ev["start"]).replace("Z", "+00:00")))
                e_ = ev.get("end")
                e = _ensure_utc(datetime.fromisoformat(str(e_).replace("Z", "+00:00"))) if e_ else None
            except Exception:
                continue
            if e is None:
                continue
            if e <= start_utc or s >= end_utc:
                continue
            blockers.append(Interval(max(s, start_utc), min(e, end_utc)))

    # Apply blockers to each day window
    free: list[Interval] = []
    for w in day_windows:
        free.extend(_subtract(w, blockers))
    return free


# ---------- Prioritization ----------

_PRIORITY_WEIGHT = {"asap": 4.0, "high": 2.0, "medium": 1.0, "low": 0.5}


def priority_score(task: dict, now: datetime) -> float:
    """Combined score: urgency (time-to-deadline) + user priority tier."""
    tier = _PRIORITY_WEIGHT.get(task.get("priority") or "medium", 1.0)
    deadline = task.get("deadline_ts")
    if not deadline:
        return tier  # no deadline → priority tier only
    try:
        dt = _ensure_utc(datetime.fromisoformat(deadline.replace("Z", "+00:00")))
    except Exception:
        return tier
    hours_until = max(0.1, (dt - now).total_seconds() / 3600)
    urgency = 10.0 / hours_until  # 1h out → 10; 24h → ~0.4
    return urgency + tier


# ---------- Placement ----------

_PREFERRED_WINDOW_RANGES = {
    # local-hour ranges; midpoint of a candidate slot is checked against these
    "morning":   (5, 12),
    "afternoon": (12, 18),
    "evening":   (18, 23),
}


def _slot_in_preferred_window(slot: Interval, preferred: Optional[str]) -> bool:
    """Soft preference: does the slot's local-time midpoint fall in the band?"""
    if not preferred:
        return False
    bounds = _PREFERRED_WINDOW_RANGES.get(preferred)
    if not bounds:
        return False
    mid = slot.start + (slot.end - slot.start) / 2
    h = _local(mid).hour
    return bounds[0] <= h < bounds[1]


def _split_task_across_slots(
    task: dict,
    free: list[Interval],
    now_utc: datetime,
    earliest_start: Optional[datetime] = None,
) -> tuple[list[Chunk], int]:
    """
    Place a task's duration across free slots, respecting min/max chunk size,
    the task's deadline, an optional `earliest_start` floor (used to honor
    task_deps), and a soft preferred-window bonus. Returns
    (placed_chunks, minutes_not_placed).

    Mutates `free` in-place: consumes used portions so subsequent tasks see
    reduced availability. Slot iteration order is reordered per task to put
    preferred-window slots first; this is the soft bonus from ROADBLOCKS §3.
    """
    needed = int(task["duration_min"])
    min_chunk = int(task.get("min_chunk_min") or 30)
    max_chunk = int(task.get("max_chunk_min") or 120)
    deadline = task.get("deadline_ts")
    deadline_utc = None
    if deadline:
        try:
            deadline_utc = _ensure_utc(datetime.fromisoformat(deadline.replace("Z", "+00:00")))
        except Exception:
            deadline_utc = None
    # If the deadline is already in the past, the task is overdue — the user
    # still needs to do it (we got here because Canvas says it's unsubmitted).
    # Treat deadline as "unbounded for placement purposes" so the solver finds
    # the next available slot instead of refusing to schedule.
    if deadline_utc is not None and deadline_utc < now_utc:
        deadline_utc = None

    earliest = _ensure_utc(earliest_start) if earliest_start else None
    preferred = task.get("preferred_window")

    # Reorder `free` so preferred-window slots are tried first. Slot order
    # is otherwise arbitrary across tasks (greedy mutates it as it consumes
    # space), so this reshuffle is safe — chunks are sorted by start in `place()`.
    if preferred:
        free.sort(key=lambda s: (0 if _slot_in_preferred_window(s, preferred) else 1, s.start))
    else:
        free.sort(key=lambda s: s.start)

    placed: list[Chunk] = []
    remaining = needed
    i = 0
    while remaining > 0 and i < len(free):
        slot = free[i]
        if slot.duration_min < min_chunk:
            i += 1
            continue
        if deadline_utc is not None and slot.start >= deadline_utc:
            i += 1
            continue
        # Honor a dep-derived floor: skip slots that start before all deps end.
        slot_start = slot.start
        if earliest is not None and slot_start < earliest:
            if slot.end <= earliest:
                i += 1
                continue
            slot_start = earliest
            if (slot.end - slot_start).total_seconds() / 60 < min_chunk:
                i += 1
                continue
        usable_min = int((slot.end - slot_start).total_seconds() / 60)
        # How much can we take from this slot?
        take = min(remaining, usable_min, max_chunk)
        if take < min_chunk:
            # Sub-minimum remainder — don't create a sliver chunk. Either the
            # task is 99% placed (remaining was tiny) or this slot is smaller
            # than the task's min_chunk.
            i += 1
            continue
        slot_end_candidate = slot_start + timedelta(minutes=take)
        if deadline_utc is not None and slot_end_candidate > deadline_utc:
            # Cap at deadline
            take = max(0, int((deadline_utc - slot_start).total_seconds() / 60))
            if take < min_chunk:
                i += 1
                continue
            slot_end_candidate = slot_start + timedelta(minutes=take)

        placed.append(Chunk(
            task_id=task["id"],
            title=task["title"],
            start=slot_start,
            end=slot_end_candidate,
            notes=task.get("notes") or "",
        ))
        remaining -= take
        # Consume used portion from slot. If we trimmed the leading edge for
        # `earliest`, the gap before slot_start is unusable for this task and
        # also for any later task with deps (they'd hit the same floor or a
        # later one). Drop it cleanly.
        new_start = slot_end_candidate
        if new_start >= slot.end:
            free.pop(i)
        else:
            free[i] = Interval(new_start, slot.end)
            if free[i].duration_min < min_chunk:
                free.pop(i)
            else:
                i += 1
    return placed, remaining


def _topo_order(tasks_by_id: dict[int, dict],
                deps: dict[int, list[int]],
                priority_key) -> tuple[list[dict], set[int]]:
    """Kahn's algorithm with priority as the within-level tie-breaker.

    Tasks whose deps reference IDs not in `tasks_by_id` (e.g. a dep that's
    already done and filtered out of `active`) are treated as unblocked w.r.t.
    that dep.

    Returns (ordered_tasks, cycle_member_ids). Cycle members are emitted at
    the end in priority order; the caller should ignore dep enforcement for
    those ids (otherwise a mutual dep would deadlock both tasks into at-risk).
    """
    indeg: dict[int, int] = {tid: 0 for tid in tasks_by_id}
    blocks: dict[int, list[int]] = {tid: [] for tid in tasks_by_id}
    for tid in tasks_by_id:
        for d in deps.get(tid, []):
            if d in tasks_by_id:
                blocks[d].append(tid)
                indeg[tid] += 1

    ready = [tid for tid, n in indeg.items() if n == 0]
    out: list[dict] = []
    placed_ids: set[int] = set()
    while ready:
        ready.sort(key=lambda tid: priority_key(tasks_by_id[tid]), reverse=True)
        tid = ready.pop(0)
        out.append(tasks_by_id[tid])
        placed_ids.add(tid)
        for nxt in blocks.get(tid, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)

    cycle_members: set[int] = set()
    if len(out) < len(tasks_by_id):
        leftover_ids = [tid for tid in tasks_by_id if tid not in placed_ids]
        leftover_ids.sort(key=lambda tid: priority_key(tasks_by_id[tid]), reverse=True)
        for tid in leftover_ids:
            out.append(tasks_by_id[tid])
            cycle_members.add(tid)
    return out, cycle_members


def place(active_tasks: list[dict], free: list[Interval], now_utc: datetime,
          deps: Optional[dict[int, list[int]]] = None
          ) -> tuple[list[Chunk], list[AtRisk]]:
    """
    Greedy placement: topo-sort by `task_deps` (priority score is the
    within-level tie-breaker), then place each task's minutes across available
    free slots. A task with deps cannot start before the latest end-time of
    all its deps' placed chunks; if a dep failed to place fully, the dependent
    task is marked at-risk and skipped.

    `free` is mutated as slots are consumed.
    """
    pri_key = lambda t: priority_score(t, now_utc)
    by_id = {int(t["id"]): t for t in active_tasks}
    deps = deps or {}
    ordered, cycle_ids = _topo_order(by_id, deps, pri_key)

    placed_chunks_by_task: dict[int, list[Chunk]] = {}
    fully_placed: set[int] = set()
    at_risk_ids: set[int] = set()
    all_chunks: list[Chunk] = []
    at_risk: list[AtRisk] = []
    for t in ordered:
        tid = int(t["id"])
        # Resolve earliest start from active deps. Cycle members ignore
        # deps — otherwise a mutual cycle would deadlock both members.
        earliest = None
        unmet = False
        if tid not in cycle_ids:
            for d in deps.get(tid, []):
                if d not in by_id:
                    continue  # dep already done / filtered
                if d in at_risk_ids and d not in fully_placed:
                    unmet = True
                    break
                d_chunks = placed_chunks_by_task.get(d, [])
                if not d_chunks:
                    # Dep is active but produced zero placement — same effect as at-risk.
                    unmet = True
                    break
                d_end = max(c.end for c in d_chunks)
                earliest = d_end if earliest is None else max(earliest, d_end)

        if unmet:
            at_risk.append(AtRisk(
                task_id=tid, title=t["title"],
                duration_needed_min=int(t["duration_min"]),
                duration_placed_min=0,
                reason="blocked by unplaced dependency",
            ))
            at_risk_ids.add(tid)
            continue

        placed, remaining = _split_task_across_slots(t, free, now_utc, earliest_start=earliest)
        placed_chunks_by_task[tid] = placed
        all_chunks.extend(placed)
        if remaining > 0:
            placed_min = sum(c.duration_min for c in placed)
            at_risk.append(AtRisk(
                task_id=tid, title=t["title"],
                duration_needed_min=int(t["duration_min"]),
                duration_placed_min=placed_min,
                reason=("over deadline" if t.get("deadline_ts") else "insufficient slots"),
            ))
            at_risk_ids.add(tid)
        else:
            fully_placed.add(tid)
    # Sort chunks by start time for stable output
    all_chunks.sort(key=lambda c: c.start)
    return all_chunks, at_risk


# ---------- Main entry point ----------

def resolve(
    *,
    now_utc: Optional[datetime] = None,
    window_days: int = 14,
    external_events_fn: Optional[Callable[[datetime, datetime], list[dict]]] = None,
    writer_fn: Optional[Callable[[datetime, datetime, list["Chunk"]], dict]] = None,
    exclude_kinds: Optional[Iterable[str]] = None,
) -> ResolveResult:
    """
    Core planning call.

    - `external_events_fn(start_utc, end_utc)` → iCloud events (non-AUTO)
      that the solver must schedule around.
    - `writer_fn(start_utc, end_utc, chunks)` → if provided, commit the plan
      by clearing any AUTO-tagged events in the window and writing new ones.
      If None, the solver runs in dry-run mode and writes nothing.

    Both seams are DI — keeps solver.py free of CalDAV imports.
    """
    now = _ensure_utc(now_utc or datetime.now(timezone.utc))
    start = now
    end = now + timedelta(days=window_days)
    events = []
    if external_events_fn is not None:
        try:
            events = external_events_fn(start, end) or []
        except Exception:
            events = []

    slots = free_slots(start, end, external_events=events, now_utc=now)
    total_free = sum(s.duration_min for s in slots)

    # Refresh duration estimates from history before planning. Cheap when
    # task_history is stable; updates each source-originated task whose
    # historical median has shifted ≥10% since the last estimate.
    try:
        tasks_mod.relearn_durations()
    except Exception:
        pass  # learning is best-effort; never block a plan cycle

    active = tasks_mod.list_active()
    deps = tasks_mod.get_all_deps()
    chunks, at_risk = place(active, slots, now, deps=deps)
    scheduled_min = sum(c.duration_min for c in chunks)

    write_summary = None
    if writer_fn is not None and chunks:
        try:
            write_summary = writer_fn(start, end, chunks)
        except Exception as ex:
            write_summary = {"error": f"{type(ex).__name__}: {ex}"}

    result = ResolveResult(
        chunks=chunks,
        at_risk=at_risk,
        now_utc=now.isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        free_slot_minutes_total=total_free,
        scheduled_minutes=scheduled_min,
    )
    if write_summary is not None:
        # Attach via attribute so to_dict() picks it up below
        result.__dict__["write_summary"] = write_summary
    return result


if __name__ == "__main__":
    # Quick smoke test without iCloud events
    result = resolve()
    print(f"Now: {result.now_utc}")
    print(f"Window: {result.window_start} → {result.window_end}")
    print(f"Free minutes in window: {result.free_slot_minutes_total}")
    print(f"Scheduled minutes: {result.scheduled_minutes}")
    print(f"Chunks: {len(result.chunks)}   At risk: {len(result.at_risk)}")
    for c in result.chunks:
        print(f"  {c.start.isoformat()}  {c.title[:60]}  ({c.duration_min}m)")
    for r in result.at_risk:
        print(f"  AT-RISK  {r.title}  placed {r.duration_placed_min}/{r.duration_needed_min}m")
