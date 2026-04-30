"""Unit tests for wake_watchdog.detect_wake — the pure helper underneath
the watchdog thread. We don't exercise the threading loop directly because
that's a 30-second time-based check; the helper is what we trust."""
from __future__ import annotations

from wake_watchdog import detect_wake


def test_no_wake_when_drift_under_threshold():
    # 30s elapsed both monotonic and wallclock — no sleep happened.
    woke, drift, _, _ = detect_wake(
        last_mono=1000.0, last_wall=2000.0,
        now_mono=1030.0, now_wall=2030.0,
        threshold_seconds=60.0,
    )
    assert not woke
    assert drift == 0.0


def test_wake_detected_when_wallclock_jumps_ahead():
    # 30s monotonic, 3600s wallclock — host slept ~58 minutes.
    woke, drift, _, _ = detect_wake(
        last_mono=1000.0, last_wall=2000.0,
        now_mono=1030.0, now_wall=5600.0,
        threshold_seconds=60.0,
    )
    assert woke
    assert drift == 3570.0  # 3600 wall - 30 mono


def test_minor_clock_skew_ignored():
    # 30s monotonic, 35s wallclock (NTP nudge or load spike) — not a sleep.
    woke, drift, _, _ = detect_wake(
        last_mono=1000.0, last_wall=2000.0,
        now_mono=1030.0, now_wall=2035.0,
        threshold_seconds=60.0,
    )
    assert not woke
    assert drift == 5.0


def test_returned_state_can_chain_to_next_tick():
    # First tick: no wake. Returned (mono, wall) become next tick's last_*.
    woke1, _, m1, w1 = detect_wake(
        last_mono=0.0, last_wall=0.0,
        now_mono=30.0, now_wall=30.0,
    )
    assert not woke1
    assert (m1, w1) == (30.0, 30.0)

    # Second tick: machine slept for 10 minutes between them.
    woke2, drift2, _, _ = detect_wake(
        last_mono=m1, last_wall=w1,
        now_mono=m1 + 30.0, now_wall=w1 + 600.0,
    )
    assert woke2
    assert drift2 == 570.0
