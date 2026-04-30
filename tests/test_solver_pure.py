"""Unit tests for pure solver helpers — no DB, no external services."""
from __future__ import annotations

from datetime import datetime, date, timedelta, timezone

import pytest

import solver
from solver import (
    Interval,
    _subtract,
    _byday_matches,
    priority_score,
    _split_task_across_slots,
    place,
)


# Small time helpers for readability
def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ---------- _subtract ----------

class TestSubtract:
    def test_no_blockers_returns_base(self):
        base = Interval(_utc(2026, 4, 21, 9), _utc(2026, 4, 21, 17))
        assert _subtract(base, []) == [base]

    def test_single_middle_blocker_splits(self):
        base = Interval(_utc(2026, 4, 21, 9), _utc(2026, 4, 21, 17))
        blocker = Interval(_utc(2026, 4, 21, 12), _utc(2026, 4, 21, 13))
        out = _subtract(base, [blocker])
        assert len(out) == 2
        assert out[0].start == _utc(2026, 4, 21, 9)
        assert out[0].end == _utc(2026, 4, 21, 12)
        assert out[1].start == _utc(2026, 4, 21, 13)
        assert out[1].end == _utc(2026, 4, 21, 17)

    def test_blocker_covering_entire_base_returns_empty(self):
        base = Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11))
        blocker = Interval(_utc(2026, 4, 21, 9), _utc(2026, 4, 21, 12))
        assert _subtract(base, [blocker]) == []

    def test_blocker_outside_base_is_ignored(self):
        base = Interval(_utc(2026, 4, 21, 9), _utc(2026, 4, 21, 17))
        blocker = Interval(_utc(2026, 4, 21, 20), _utc(2026, 4, 21, 22))
        assert _subtract(base, [blocker]) == [base]

    def test_overlapping_blockers_merged(self):
        base = Interval(_utc(2026, 4, 21, 9), _utc(2026, 4, 21, 17))
        b1 = Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 12))
        b2 = Interval(_utc(2026, 4, 21, 11), _utc(2026, 4, 21, 14))
        out = _subtract(base, [b2, b1])  # unordered
        # Merged blocker is 10-14 → remaining is 9-10 and 14-17
        assert len(out) == 2
        assert out[0].end == _utc(2026, 4, 21, 10)
        assert out[1].start == _utc(2026, 4, 21, 14)


# ---------- _byday_matches ----------

class TestBydayMatches:
    def test_mwf_rule_on_monday_matches(self):
        assert _byday_matches("FREQ=WEEKLY;BYDAY=MO,WE,FR", date(2026, 4, 20)) is True

    def test_mwf_rule_on_tuesday_does_not_match(self):
        assert _byday_matches("FREQ=WEEKLY;BYDAY=MO,WE,FR", date(2026, 4, 21)) is False

    def test_empty_rrule_always_matches(self):
        assert _byday_matches(None, date(2026, 4, 21)) is True
        assert _byday_matches("", date(2026, 4, 21)) is True

    def test_rrule_without_byday_always_matches(self):
        assert _byday_matches("FREQ=WEEKLY", date(2026, 4, 21)) is True


# ---------- priority_score ----------

class TestPriorityScore:
    def test_no_deadline_returns_tier_weight_only(self):
        now = _utc(2026, 4, 21, 12)
        assert priority_score({"priority": "high"}, now) == 2.0
        assert priority_score({"priority": "low"}, now) == 0.5

    def test_close_deadline_boosts_score(self):
        now = _utc(2026, 4, 21, 12)
        close = {"priority": "medium",
                 "deadline_ts": _utc(2026, 4, 21, 13).isoformat()}  # 1h away
        far = {"priority": "medium",
               "deadline_ts": _utc(2026, 4, 28, 12).isoformat()}    # 7d away
        assert priority_score(close, now) > priority_score(far, now)

    def test_overdue_treated_as_maximum_urgency(self):
        """Deadlines in the past should score at least as high as something
        due 1 hour from now (overdue == ASAP, per solver S5 semantics)."""
        now = _utc(2026, 4, 21, 12)
        overdue = {"priority": "medium",
                   "deadline_ts": _utc(2026, 4, 20, 12).isoformat()}  # 24h ago
        imminent = {"priority": "medium",
                    "deadline_ts": _utc(2026, 4, 21, 13).isoformat()}  # 1h ahead
        assert priority_score(overdue, now) >= priority_score(imminent, now)

    def test_bad_iso_falls_back_to_tier_only(self):
        now = _utc(2026, 4, 21, 12)
        task = {"priority": "medium", "deadline_ts": "not-a-date"}
        assert priority_score(task, now) == 1.0


# ---------- _split_task_across_slots ----------

class TestSplitTaskAcrossSlots:
    def test_task_fits_in_one_slot(self):
        now = _utc(2026, 4, 21, 9)
        slot = Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 14))
        task = {"id": 1, "title": "t", "duration_min": 60,
                "min_chunk_min": 30, "max_chunk_min": 120}
        chunks, remaining = _split_task_across_slots(task, [slot], now)
        assert remaining == 0
        assert len(chunks) == 1
        assert chunks[0].duration_min == 60
        assert chunks[0].start == _utc(2026, 4, 21, 10)

    def test_task_splits_across_two_slots_respects_max_chunk(self):
        now = _utc(2026, 4, 21, 9)
        slots = [
            Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 12)),  # 120m
            Interval(_utc(2026, 4, 21, 14), _utc(2026, 4, 21, 16)),  # 120m
        ]
        task = {"id": 1, "title": "t", "duration_min": 180,
                "min_chunk_min": 30, "max_chunk_min": 90}
        chunks, remaining = _split_task_across_slots(task, slots, now)
        assert remaining == 0
        # Max chunk 90 → first slot contributes 90 then loop moves on.
        # Second slot contributes the remaining 90.
        assert all(c.duration_min <= 90 for c in chunks)
        assert sum(c.duration_min for c in chunks) == 180

    def test_s3_no_sub_min_chunk_sliver(self):
        """ROADBLOCKS §S3: a chunk shorter than min_chunk_min is never emitted."""
        now = _utc(2026, 4, 21, 9)
        # Only one slot of 20 min available; task needs 30, min_chunk 30.
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 10, 20))]
        task = {"id": 1, "title": "t", "duration_min": 30,
                "min_chunk_min": 30, "max_chunk_min": 120}
        chunks, remaining = _split_task_across_slots(task, slots, now)
        assert chunks == []
        assert remaining == 30

    def test_s2_overdue_task_still_placed_in_future_slot(self):
        """ROADBLOCKS §S2: a task with deadline in the past should get a
        chunk placed in a future free slot, not refused outright."""
        now = _utc(2026, 4, 21, 12)
        slots = [Interval(_utc(2026, 4, 21, 14), _utc(2026, 4, 21, 16))]
        task = {
            "id": 1, "title": "overdue",
            "duration_min": 30, "min_chunk_min": 30, "max_chunk_min": 120,
            "deadline_ts": _utc(2026, 4, 20, 12).isoformat(),  # 24h ago
        }
        chunks, remaining = _split_task_across_slots(task, slots, now)
        assert remaining == 0
        assert len(chunks) == 1
        assert chunks[0].start >= now

    def test_deadline_caps_placement(self):
        """A task with a soon deadline inside a large slot should place only
        up to the deadline, not beyond it."""
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 16))]
        task = {
            "id": 1, "title": "capped", "duration_min": 180,
            "min_chunk_min": 30, "max_chunk_min": 120,
            "deadline_ts": _utc(2026, 4, 21, 11).isoformat(),  # 1h after slot start
        }
        chunks, remaining = _split_task_across_slots(task, slots, now)
        # At most 60 minutes should be placed before the deadline.
        placed_min = sum(c.duration_min for c in chunks)
        assert placed_min <= 60
        for c in chunks:
            assert c.end <= _utc(2026, 4, 21, 11)


# ---------- place ----------

class TestPlace:
    def test_higher_priority_task_placed_first(self):
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11))]  # 60m
        tasks = [
            {"id": 1, "title": "low", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 120, "priority": "low"},
            {"id": 2, "title": "asap", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 120, "priority": "asap"},
        ]
        chunks, at_risk = place(tasks, slots, now)
        assert len(chunks) == 1
        assert chunks[0].task_id == 2            # asap won
        assert len(at_risk) == 1
        assert at_risk[0].task_id == 1

    def test_at_risk_records_placement_shortfall(self):
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11))]  # 60m
        tasks = [
            {"id": 1, "title": "big", "duration_min": 120,
             "min_chunk_min": 30, "max_chunk_min": 120, "priority": "medium"},
        ]
        chunks, at_risk = place(tasks, slots, now)
        assert len(chunks) == 1
        assert chunks[0].duration_min == 60
        assert len(at_risk) == 1
        assert at_risk[0].duration_needed_min == 120
        assert at_risk[0].duration_placed_min == 60

    def test_chunks_sorted_by_start_time(self):
        now = _utc(2026, 4, 21, 9)
        slots = [
            Interval(_utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15)),
            Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11)),
        ]
        tasks = [
            {"id": 1, "title": "one", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 120, "priority": "medium"},
            {"id": 2, "title": "two", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 120, "priority": "medium"},
        ]
        chunks, _ = place(tasks, slots, now)
        starts = [c.start for c in chunks]
        assert starts == sorted(starts)


class TestTaskDeps:
    def test_dep_pushes_dependent_after_dep_end(self):
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 14))]  # 4h
        tasks = [
            {"id": 1, "title": "writeup", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 60, "priority": "medium"},
            {"id": 2, "title": "submit",  "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 60, "priority": "medium"},
        ]
        deps = {2: [1]}  # submit depends on writeup
        chunks, at_risk = place(tasks, slots, now, deps=deps)
        assert at_risk == []
        by_task = {c.task_id: c for c in chunks}
        assert by_task[2].start >= by_task[1].end

    def test_unplaced_dep_marks_dependent_at_risk(self):
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11))]  # only 60m
        tasks = [
            {"id": 1, "title": "huge", "duration_min": 240,
             "min_chunk_min": 60, "max_chunk_min": 120, "priority": "high"},
            {"id": 2, "title": "tiny", "duration_min": 30,
             "min_chunk_min": 30, "max_chunk_min": 30, "priority": "high"},
        ]
        deps = {2: [1]}
        chunks, at_risk = place(tasks, slots, now, deps=deps)
        # Task 1 took the only slot; task 2 has nowhere to go after it.
        ids_at_risk = {r.task_id for r in at_risk}
        assert 2 in ids_at_risk
        # And task 2 is flagged for the dep reason, not deadline.
        r2 = next(r for r in at_risk if r.task_id == 2)
        assert "depend" in r2.reason or "blocked" in r2.reason

    def test_done_dep_does_not_block(self):
        # If a dep id isn't in active_tasks (already done & filtered),
        # the dependent should still place freely.
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 11))]
        tasks = [
            {"id": 2, "title": "ready", "duration_min": 60,
             "min_chunk_min": 30, "max_chunk_min": 60, "priority": "medium"},
        ]
        deps = {2: [1]}  # task 1 is already done → not in active list
        chunks, at_risk = place(tasks, slots, now, deps=deps)
        assert len(chunks) == 1
        assert at_risk == []

    def test_dep_cycle_does_not_hang(self):
        now = _utc(2026, 4, 21, 9)
        slots = [Interval(_utc(2026, 4, 21, 10), _utc(2026, 4, 21, 13))]  # 3h
        tasks = [
            {"id": 1, "title": "a", "duration_min": 30,
             "min_chunk_min": 30, "max_chunk_min": 30, "priority": "asap"},
            {"id": 2, "title": "b", "duration_min": 30,
             "min_chunk_min": 30, "max_chunk_min": 30, "priority": "low"},
        ]
        deps = {1: [2], 2: [1]}  # mutual cycle
        chunks, _ = place(tasks, slots, now, deps=deps)
        # Both should still get placed (cycle break by priority).
        ids = {c.task_id for c in chunks}
        assert ids == {1, 2}


class TestPreferredWindow:
    def test_morning_preference_picks_morning_slot(self):
        # Two equal-length slots: 06:00 UTC (~02:00 ET, night) and 14:00 UTC (~10:00 ET, morning).
        # With TZ=America/New_York the second is "morning".
        # Use times that are unambiguous regardless of DST.
        now = _utc(2026, 4, 21, 0)
        # 14:00 UTC = 10:00 ET (morning); 22:00 UTC = 18:00 ET (evening)
        slot_eve = Interval(_utc(2026, 4, 21, 22), _utc(2026, 4, 21, 23))
        slot_morn = Interval(_utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15))
        slots = [slot_eve, slot_morn]  # eve listed first
        task = {"id": 1, "title": "homework", "duration_min": 60,
                "min_chunk_min": 30, "max_chunk_min": 60,
                "priority": "medium", "preferred_window": "morning"}
        chunks, _ = place([task], slots, now)
        assert len(chunks) == 1
        assert chunks[0].start == _utc(2026, 4, 21, 14)

    def test_no_preference_keeps_earliest_slot(self):
        now = _utc(2026, 4, 21, 0)
        slot_eve = Interval(_utc(2026, 4, 21, 22), _utc(2026, 4, 21, 23))
        slot_morn = Interval(_utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15))
        task = {"id": 1, "title": "homework", "duration_min": 60,
                "min_chunk_min": 30, "max_chunk_min": 60, "priority": "medium"}
        chunks, _ = place([task], [slot_eve, slot_morn], now)
        assert chunks[0].start == _utc(2026, 4, 21, 14)  # earliest wins
