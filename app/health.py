"""
health.py  -  Spots cameras that have stopped hearing properly.

Three checks, run every few minutes for each camera:

  no_audio   No recording has succeeded for HEALTH_NO_AUDIO_MIN minutes
             (stream down, go2rtc lost it, camera offline).
  silent     Recordings arrive but are near-silent: the median raw peak level
             (measured before Birdwatch boosts the volume) is below
             HEALTH_SILENT_PEAK_DB. A switched-off mic sends -inf.
  no_birds   In daylight, no birds for HEALTH_NO_BIRDS_HOURS while the other
             cameras heard plenty (median at least HEALTH_BUSY_MIN). This is
             the one that catches a dead or blocked mic that only sends hiss.

Results go to /api/health, the dashboard and map (Socket.IO 'health' event)
and Home Assistant (one "problem" binary sensor per camera over MQTT).
"""

import logging
import math
import os
import re
import sqlite3
import statistics
import time
from collections import deque
from datetime import datetime, timedelta

import eventlet
from eventlet import tpool

import database

logger = logging.getLogger(__name__)

NO_AUDIO_MIN    = float(os.getenv("HEALTH_NO_AUDIO_MIN", "10"))
SILENT_PEAK_DB  = float(os.getenv("HEALTH_SILENT_PEAK_DB", "-60"))
NO_BIRDS_HOURS  = float(os.getenv("HEALTH_NO_BIRDS_HOURS", "2"))
BUSY_MIN        = int(os.getenv("HEALTH_BUSY_MIN", "10"))
CHECK_EVERY_S   = 300
LEVEL_WINDOW_S  = 1800             # levels are judged over the last 30 minutes

_cameras: list[str] = []
_levels: dict[str, deque] = {}     # camera -> (time, rms_db, peak_db)
_last_ok: dict[str, float] = {}    # camera -> time of last good recording
_status: dict[str, dict] = {}
_started = time.time()
_is_paused = lambda: False
_publish = None                    # fn(camera, status) -> MQTT
_emit = None                       # fn(all_statuses) -> Socket.IO


# ── Called by capture.py ─────────────────────────────────────────────────────

_LEVEL_RE = re.compile(r"(RMS|Peak) level dB:\s*(-?inf|-?[\d.]+)")


def parse_levels(stderr: str):
    """Pull the overall RMS and peak level (dB) out of ffmpeg's astats output."""
    i = stderr.rfind("Overall")
    if i < 0:
        return None, None
    found = dict((k, float(v)) for k, v in _LEVEL_RE.findall(stderr[i:]))
    return found.get("RMS"), found.get("Peak")


def record_chunk(camera: str, rms_db, peak_db):
    now = time.time()
    _last_ok[camera] = now
    if rms_db is not None and peak_db is not None:
        q = _levels.setdefault(camera, deque(maxlen=400))
        q.append((now, rms_db, peak_db))


# ── Checks ───────────────────────────────────────────────────────────────────

def _sun_elevation(lat: float, lon: float) -> float:
    """Approximate solar elevation in degrees (NOAA formula), for 'is it daytime?'."""
    now = datetime.utcnow()
    doy = now.timetuple().tm_yday
    g = 2 * math.pi / 365 * (doy - 1 + (now.hour - 12) / 24)
    eq = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                   - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    minutes = now.hour * 60 + now.minute + now.second / 60
    ha = math.radians((minutes + eq + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1, min(1, cz))))


def _detections_since(since_iso: str) -> dict:
    with database._lock:
        conn = sqlite3.connect(database.DB_PATH)
        rows = conn.execute("SELECT camera, COUNT(*) FROM detections WHERE timestamp >= ? GROUP BY camera",
                            (since_iso,)).fetchall()
        conn.close()
    return dict(rows)


def _fmt_db(v):
    return "no signal at all" if v is None or v == -math.inf else f"peak {v:.0f} dB"


def evaluate() -> dict:
    now = time.time()
    paused = _is_paused()
    lat = float(os.getenv("LATITUDE", "54.93"))
    lon = float(os.getenv("LONGITUDE", "-2.73"))
    daylight = _sun_elevation(lat, lon) > 5
    since = (datetime.now(tz=database.LONDON) - timedelta(hours=NO_BIRDS_HOURS)).isoformat()
    counts = tpool.execute(_detections_since, since)

    result = {}
    for cam in _cameras:
        recent = [x for x in _levels.get(cam, ()) if now - x[0] < LEVEL_WINDOW_S]
        rms = statistics.median(x[1] for x in recent) if recent else None
        peak = statistics.median(x[2] for x in recent) if recent else None
        last = _last_ok.get(cam)
        others = [counts.get(c, 0) for c in _cameras if c != cam]
        others_median = statistics.median(others) if others else 0
        n = counts.get(cam, 0)

        issues = []
        if not paused and now - _started > NO_AUDIO_MIN * 60:
            if last is None or now - last > NO_AUDIO_MIN * 60:
                mins = int((now - (last or _started)) / 60)
                issues.append({"code": "no_audio",
                               "message": f"No audio recorded for {mins} min - is the stream down?"})
            elif len(recent) >= 20 and peak is not None and peak < SILENT_PEAK_DB:
                issues.append({"code": "silent",
                               "message": f"Microphone is silent ({_fmt_db(peak)}) - is audio switched off?"})
        if (not paused and daylight and now - _started > NO_BIRDS_HOURS * 3600
                and n == 0 and others_median >= BUSY_MIN and not issues):
            hrs = f"{NO_BIRDS_HOURS:g} h"
            issues.append({"code": "no_birds",
                           "message": f"No birds for {hrs} while the other cameras heard {int(others_median)} each"
                                      f" - check its microphone"})

        result[cam] = {
            "ok": not issues,
            "issues": issues,
            "rms_db": None if rms is None or rms == -math.inf else round(rms, 1),
            "peak_db": None if peak is None or peak == -math.inf else round(peak, 1),
            "recordings_30min": len(recent),
            "last_recording_s": None if last is None else int(now - last),
            "detections_window": n,
        }
    return result


def _loop():
    eventlet.sleep(60)
    while True:
        try:
            new = evaluate()
            for cam, st in new.items():
                old = _status.get(cam)
                if old is None or old["ok"] != st["ok"] or [i["code"] for i in old["issues"]] != [i["code"] for i in st["issues"]]:
                    if st["ok"]:
                        logger.info(f"🩺 [{cam}] audio OK again" if old else f"🩺 [{cam}] audio OK")
                    else:
                        for i in st["issues"]:
                            logger.warning(f"🩺 [{cam}] {i['message']}")
                if _publish:
                    try:
                        _publish(cam, st)
                    except Exception as e:
                        logger.debug(f"health publish failed: {e}")
            _status.clear()
            _status.update(new)
            if _emit:
                _emit(get_status())
        except Exception as e:
            logger.error(f"health check failed: {e}")
        eventlet.sleep(CHECK_EVERY_S)


def get_status() -> dict:
    return {"checked": True if _status else False, "cameras": dict(_status),
            "thresholds": {"no_audio_min": NO_AUDIO_MIN, "silent_peak_db": SILENT_PEAK_DB,
                           "no_birds_hours": NO_BIRDS_HOURS, "busy_min": BUSY_MIN}}


def start(cameras: list[str], is_paused=None, publish=None, emit=None):
    global _cameras, _is_paused, _publish, _emit
    _cameras = list(cameras)
    _is_paused = is_paused or _is_paused
    _publish, _emit = publish, emit
    eventlet.spawn(_loop)
