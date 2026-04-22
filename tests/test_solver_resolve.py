"""Integration tests for solver.free_slots and solver.resolve — they read
working hours, config_blocks, and tasks out of the SQLite DB. The `db`
fixture in conftest.py provides a throwaway DB per test, pre-seeded with
working hours of 09:00-22:00 every day and no class blocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import solver

NY = ZoneInfo("America/New_York")


def _ny(y, m, d, hh=0, mm=0):
    """Return a NY-local datetime converted to UTC."""
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(timezone.utc)


# ---------- free_slots ----------

class TestFreeSlots:
    def test_empty_day_has_13h_window(self, db):
        """With 09:00-22:00 working hours and no blocks, a full weekday is
        13 hours of free time."""
        # Monday, April 20 2026 — DST active (EDT).
        start = _ny(2026, 4, 20, 0)
        end = _ny(2026, 4, 21, 0)
        slots = solver.free_slots(start, end, now_utc=start)
        total = sum(s.duration_min for s in slots)
        assert total == 13 * 60
        assert slots[0].start == _ny(2026, 4, 20, 9)
        assert slots[-1].end == _ny(2026, 4, 20, 22)

    def test_s4_class_block_removed_from_free_window(self, db, add_block):
        """ROADBLOCKS §S4: config blocks must never overlap free slots.
        A class 11:00-12:00 on MWF must carve a gap out of Monday's window."""
        add_block("class", "CS 101", "FREQ=WEEKLY;BYDAY=MO,WE,FR",
                  "11:00", "12:00")
        start = _ny(2026, 4, 20, 0)  # Monday
        end = _ny(2026, 4, 21, 0)
        slots = solver.free_slots(start, end, now_utc=start)
        # No slot should contain any minute of 11:00-12:00 NY.
        class_start = _ny(2026, 4, 20, 11)
        class_end = _ny(2026, 4, 20, 12)
        for s in slots:
            overlaps = s.start < class_end and s.end > class_start
            assert not overlaps, f"slot {s} overlaps class {class_start}-{class_end}"
        # And the total free time should shrink by the 60-minute class.
        total = sum(s.duration_min for s in slots)
        assert total == 13 * 60 - 60

    def test_block_on_wrong_weekday_does_not_apply(self, db, add_block):
        """A MWF-only class should not carve a gap out of a Tuesday."""
        add_block("class", "CS 101", "FREQ=WEEKLY;BYDAY=MO,WE,FR",
                  "11:00", "12:00")
        start = _ny(2026, 4, 21, 0)  # Tuesday
        end = _ny(2026, 4, 22, 0)
        slots = solver.free_slots(start, end, now_utc=start)
        total = sum(s.duration_min for s in slots)
        assert total == 13 * 60  # full 09-22 window intact

    def test_external_events_carve_out_slots(self, db):
        """An external busy event should subtract its window from free slots."""
        start = _ny(2026, 4, 20, 0)
        end = _ny(2026, 4, 21, 0)
        ev = {
            "start": _ny(2026, 4, 20, 14).isoformat(),
            "end":   _ny(2026, 4, 20, 15).isoformat(),
            "title": "meeting",
        }
        slots = solver.free_slots(start, end, external_events=[ev], now_utc=start)
        total = sum(s.duration_min for s in slots)
        assert total == 13 * 60 - 60

    def test_now_utc_floors_start(self, db):
        """`now_utc` shifts the window start forward — past slots are dropped."""
        start = _ny(2026, 4, 20, 0)
        end = _ny(2026, 4, 21, 0)
        # Pretend it's 15:00 NY — we've already lost 09-15 = 6h of the window.
        now = _ny(2026, 4, 20, 15)
        slots = solver.free_slots(start, end, now_utc=now)
        total = sum(s.duration_min for s in slots)
        assert total == 7 * 60  # 15-22 remaining

    def test_only_hard_block_kinds_subtracted(self, db, add_block):
        """A 'custom' block by default is NOT a hard block — should not cut
        into free slots unless explicitly included."""
        add_block("custom", "soft block", "FREQ=WEEKLY;BYDAY=MO",
                  "11:00", "12:00")
        start = _ny(2026, 4, 20, 0)  # Monday
        end = _ny(2026, 4, 21, 0)
        slots = solver.free_slots(start, end, now_utc=start)
        assert sum(s.duration_min for s in slots) == 13 * 60


# ---------- resolve ----------

class TestResolve:
    def test_single_task_gets_placed(self, db, add_task):
        add_task("Simple task", 60)
        now = _ny(2026, 4, 20, 9)
        result = solver.resolve(now_utc=now, window_days=1)
        assert len(result.chunks) == 1
        assert result.chunks[0].duration_min == 60
        assert result.at_risk == []

    def test_s2_resolve_overdue_task_places_in_future(self, db, add_task):
        """ROADBLOCKS §S2: overdue (deadline in the past) task must still
        land on the schedule."""
        add_task(
            "overdue essay", 60,
            deadline_ts=(_ny(2026, 4, 19, 12)).isoformat(),  # yesterday
        )
        now = _ny(2026, 4, 20, 10)
        result = solver.resolve(now_utc=now, window_days=1)
        assert len(result.chunks) == 1
        assert result.chunks[0].start >= now

    def test_chunks_never_overlap_class_block(self, db, add_task, add_block):
        """Whole-solver version of the S4 signal: no chunk should overlap
        a hard-block row in config_blocks."""
        add_block("class", "Stats", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                  "13:00", "14:00")
        add_task("big task", 6 * 60)  # 6h total, forces placement around the class
        now = _ny(2026, 4, 20, 9)     # Monday 9 AM
        result = solver.resolve(now_utc=now, window_days=1)
        # Assemble the class's UTC window on this date.
        class_start = _ny(2026, 4, 20, 13)
        class_end = _ny(2026, 4, 20, 14)
        for c in result.chunks:
            overlaps = c.start < class_end and c.end > class_start
            assert not overlaps, f"chunk {c.start}-{c.end} overlaps class"

    def test_insufficient_capacity_reports_at_risk(self, db, add_task):
        """A task bigger than remaining capacity in the window should be
        flagged at_risk with partial placement."""
        # 2-hour window (20:00-22:00 local = 2h), ask for 5 hours.
        now = _ny(2026, 4, 20, 20)
        add_task("big", 5 * 60)
        result = solver.resolve(now_utc=now, window_days=1)
        assert len(result.at_risk) == 1
        assert result.at_risk[0].duration_placed_min < 5 * 60

    def test_done_tasks_ignored(self, db, add_task):
        add_task("active", 60)
        add_task("done", 60, status="done")
        now = _ny(2026, 4, 20, 9)
        result = solver.resolve(now_utc=now, window_days=1)
        titles = [c.title for c in result.chunks]
        assert titles == ["active"]

    def test_writer_fn_invoked_with_chunks(self, db, add_task):
        """resolve() should call the injected writer_fn with the chunks when
        one is provided. Keeps the solver-to-calendar seam testable."""
        add_task("t", 60)
        captured = {}

        def writer(start, end, chunks):
            captured["n"] = len(chunks)
            return {"ok": True}

        now = _ny(2026, 4, 20, 9)
        result = solver.resolve(now_utc=now, window_days=1, writer_fn=writer)
        assert captured.get("n") == 1
        assert result.to_dict().get("write_summary") == {"ok": True}
