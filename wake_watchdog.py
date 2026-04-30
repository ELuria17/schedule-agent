"""Cross-platform "system woke from sleep" detector.

Compares `time.monotonic()` (paused while suspended) against `time.time()`
(advances during sleep on every modern OS). When wallclock has jumped ahead
of monotonic by more than `threshold_seconds` between ticks, the host slept
and just woke. We fire `on_wake(slept_seconds)` exactly once per wake.

Pure stdlib — no PyObjC/IOKit, so the same code path covers macOS, Linux,
and Windows builds. Tradeoff vs IOKit: detection lags by up to one tick
(default 30s). That's enough to replace the hub-load-kick workaround,
which fired on the *first request* after wake — typically much later.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


def start_watchdog(
    on_wake: Callable[[float], None],
    *,
    tick_seconds: float = 30.0,
    threshold_seconds: float = 60.0,
    name: str = "wake-watchdog",
) -> threading.Thread:
    """Start a daemon thread that calls `on_wake(slept_seconds)` after each
    detected sleep>resume cycle. Returns the thread (already started).

    `threshold_seconds` is how much wallclock-vs-monotonic drift counts as
    "we slept". A short hibernation (lid tap, etc.) under the threshold is
    ignored to avoid spamming the resolver.
    """
    def _loop():
        last_mono = time.monotonic()
        last_wall = time.time()
        while True:
            time.sleep(tick_seconds)
            now_mono = time.monotonic()
            now_wall = time.time()
            mono_elapsed = now_mono - last_mono
            wall_elapsed = now_wall - last_wall
            drift = wall_elapsed - mono_elapsed
            last_mono = now_mono
            last_wall = now_wall
            if drift > threshold_seconds:
                try:
                    on_wake(drift)
                except Exception:
                    # Swallow — a bad on_wake handler must not kill the watchdog.
                    pass

    t = threading.Thread(target=_loop, name=name, daemon=True)
    t.start()
    return t


def detect_wake(
    last_mono: float, last_wall: float,
    *, threshold_seconds: float = 60.0,
    now_mono: Optional[float] = None, now_wall: Optional[float] = None,
) -> tuple[bool, float, float, float]:
    """Pure helper used by the watchdog (and tests).

    Returns (woke, drift_seconds, new_mono, new_wall). Caller passes back
    new_mono/new_wall to its loop state for the next tick.
    """
    nm = time.monotonic() if now_mono is None else now_mono
    nw = time.time() if now_wall is None else now_wall
    drift = (nw - last_wall) - (nm - last_mono)
    return drift > threshold_seconds, drift, nm, nw
