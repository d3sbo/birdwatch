"""
watchdog.py  –  Reports when the app freezes, and where.

The app runs on eventlet: web page, Socket.IO, MQTT and all 7 capture loops
take turns on one thread. If any of them makes a call that doesn't hand back
control, everything else stops (captures then "time out after 45 s", the page
shows "disconnected", MQTT drops).

A green heartbeat ticks every 0.5 s. A real OS thread (outside eventlet) checks
it; if it hasn't ticked for STALL_SECONDS it logs the stack of the main thread,
i.e. the exact line everything is waiting on. One report per stall.

Turn off with WATCHDOG=off in the environment.
"""

import logging
import os
import sys
import time
import traceback

import eventlet
from eventlet import patcher

logger = logging.getLogger("watchdog")

_real_threading = patcher.original("threading")
_real_time = patcher.original("time")

STALL_SECONDS = float(os.getenv("WATCHDOG_STALL_SECONDS", "3"))
_beat = _real_time.monotonic()


def _heartbeat():
    global _beat
    while True:
        _beat = _real_time.monotonic()
        eventlet.sleep(0.5)


def _monitor(main_ident):
    reported = False
    while True:
        _real_time.sleep(1)
        stalled = _real_time.monotonic() - _beat
        if stalled >= STALL_SECONDS and not reported:
            frame = sys._current_frames().get(main_ident)
            stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
            # plain stderr write: the logging module may be what's stuck
            sys.stderr.write(f"\n[WATCHDOG] app frozen for {stalled:.0f}s - stuck here:\n{stack}\n")
            sys.stderr.flush()
            reported = True
        elif stalled < STALL_SECONDS and reported:
            sys.stderr.write(f"[WATCHDOG] app running again\n")
            sys.stderr.flush()
            reported = False


def start():
    if os.getenv("WATCHDOG", "on").lower() == "off":
        return
    main_ident = _real_threading.get_ident()
    eventlet.spawn(_heartbeat)
    t = _real_threading.Thread(target=_monitor, args=(main_ident,), daemon=True, name="watchdog")
    t.start()
    logger.info(f"🐕 Watchdog on - will report any freeze over {STALL_SECONDS:.0f}s")
