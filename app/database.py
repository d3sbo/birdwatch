import time
"""
database.py  –  SQLite persistence for bird detections.
Thread-safe via a global Lock.
All timestamps stored in Europe/London local time (auto BST/GMT).
"""

import sqlite3
import threading
from datetime import datetime, date
from zoneinfo import ZoneInfo

DB_PATH = "/data/birdwatch.db"
_lock   = threading.Lock()
LONDON  = ZoneInfo("Europe/London")


def _now():
    return datetime.now(tz=LONDON)


def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS detections (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                date          TEXT    NOT NULL,
                camera        TEXT    NOT NULL,
                common_name   TEXT    NOT NULL,
                species       TEXT    NOT NULL,
                confidence    REAL    NOT NULL,
                audio_file    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_date      ON detections(date);
            CREATE INDEX IF NOT EXISTS idx_timestamp ON detections(timestamp);
            CREATE INDEX IF NOT EXISTS idx_name      ON detections(common_name);
        """)
        # Migrate existing DB — add audio_file column if missing
        try:
            conn.execute("ALTER TABLE detections ADD COLUMN audio_file TEXT")
            conn.commit()
        except Exception:
            pass  # column already exists
        conn.commit()
        conn.close()


def save_detection(camera: str, common_name: str, species: str,
                   confidence: float, audio_file: str = None):
    now = _now()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO detections
               (timestamp, date, camera, common_name, species, confidence, audio_file)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(), now.date().isoformat(), camera,
             common_name, species, confidence, audio_file),
        )
        conn.commit()
        conn.close()


_audio_cache: set = set()
_audio_cache_time: float = 0.0
_AUDIO_CACHE_TTL: float = 10.0  # seconds

def _audio_files_set() -> set:
    """Return a cached set of audio filenames — refreshed every 10 seconds."""
    global _audio_cache, _audio_cache_time
    now = time.time()
    if now - _audio_cache_time > _AUDIO_CACHE_TTL:
        from pathlib import Path
        _audio_cache = {f.name for f in Path("/data/audio").rglob("*.wav")}
        _audio_cache_time = now
    return _audio_cache


def get_recent_detections(limit: int = 50) -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM detections ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
    existing = _audio_files_set()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("audio_file") and d["audio_file"] not in existing:
            d["audio_file"] = None
        result.append(d)
    return result


def get_today_detections(limit: int = 200) -> list[dict]:
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM detections WHERE date = ? ORDER BY timestamp DESC LIMIT ?",
            (today, limit),
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_today_count() -> int:
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COUNT(*) FROM detections WHERE date = ?", (today,)
        ).fetchone()
        conn.close()
    return row[0] if row else 0


def get_today_species() -> list[dict]:
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT
                   common_name,
                   species,
                   COUNT(*)          AS count,
                   MAX(confidence)   AS max_confidence,
                   MAX(timestamp)    AS last_seen,
                   (SELECT camera FROM detections d2
                    WHERE d2.date = d.date AND d2.common_name = d.common_name
                    ORDER BY d2.timestamp DESC LIMIT 1) AS camera
               FROM detections d
               WHERE date = ?
               GROUP BY common_name
               ORDER BY count DESC""",
            (today,),
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_hourly_activity() -> dict[str, int]:
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT strftime('%H', timestamp, 'localtime') AS hour, COUNT(*) AS count
               FROM detections
               WHERE date = ?
               GROUP BY hour
               ORDER BY hour""",
            (today,),
        ).fetchall()
        conn.close()
    hourly = {str(h).zfill(2): 0 for h in range(24)}
    for row in rows:
        hourly[row[0]] = row[1]
    return hourly


def get_all_time_species() -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT
                   common_name,
                   species,
                   COUNT(*)        AS total_detections,
                   MIN(date)       AS first_seen,
                   MAX(timestamp)  AS last_seen,
                   MAX(confidence) AS max_confidence
               FROM detections
               GROUP BY common_name
               ORDER BY total_detections DESC""",
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_cameras() -> list[str]:
    """Return all distinct camera names ever seen, ordered by first detection."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT camera FROM detections
               WHERE camera IS NOT NULL AND camera != ''
               GROUP BY camera
               ORDER BY MIN(timestamp)""",
        ).fetchall()
        conn.close()
    return [r[0] for r in rows]


def get_species_hourly(top_n: int = 5) -> dict:
    """Return hourly detection counts for the top N species today.
    Returns { species: [count_h0, count_h1, ..., count_h23], ... }
    """
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        # Get top N species by count today
        top = conn.execute(
            """SELECT common_name FROM detections
               WHERE date = ?
               GROUP BY common_name
               ORDER BY COUNT(*) DESC
               LIMIT ?""",
            (today, top_n)
        ).fetchall()
        top_names = [r[0] for r in top]
        if not top_names:
            conn.close()
            return {}
        placeholders = ','.join('?' * len(top_names))
        rows = conn.execute(
            f"""SELECT common_name,
                       CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) AS hour,
                       COUNT(*) AS count
                FROM detections
                WHERE date = ? AND common_name IN ({placeholders})
                GROUP BY common_name, hour""",
            [today] + top_names
        ).fetchall()
        conn.close()
    result = {name: [0] * 24 for name in top_names}
    for name, hour, count in rows:
        result[name][hour] = count
    return result


def get_today_sightings() -> int:
    """Count distinct sightings today — groups detections of the same species
    within 30 seconds as a single sighting."""
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT common_name, timestamp FROM detections
               WHERE date = ?
               ORDER BY common_name, timestamp""",
            (today,)
        ).fetchall()
        conn.close()

    if not rows:
        return 0

    sightings = 0
    last_species = None
    last_ts = None
    WINDOW = 30  # seconds

    for name, ts_str in rows:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            ts = 0
        if name != last_species or (ts - last_ts) > WINDOW:
            sightings += 1
            last_species = name
            last_ts = ts
        else:
            last_ts = ts  # extend window to latest detection

    return sightings


def get_species_detections_today(common_name: str) -> list[dict]:
    """Return all detections for a species today with camera, timestamp and audio_file."""
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT timestamp, camera, confidence, audio_file FROM detections
               WHERE date = ? AND common_name = ?
               ORDER BY timestamp DESC""",
            (today, common_name)
        ).fetchall()
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("audio_file") and d["audio_file"] not in _audio_files_set():
            d["audio_file"] = None
        result.append(d)
    return result


def get_recent_detections_by_camera(camera: str, limit: int = 50) -> list[dict]:
    """Return today's detections for a specific camera, falling back to recent if not enough."""
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM detections WHERE camera = ? AND date = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (camera, today, limit)
        ).fetchall()
        conn.close()
    existing = _audio_files_set()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("audio_file") and d["audio_file"] not in existing:
            d["audio_file"] = None
        result.append(d)
    return result


def get_species_bucketed(top_n: int = 12, bucket_minutes: int = 60,
                          start_hour: int = 0, end_hour: int = 23) -> dict:
    """Return detection counts bucketed by time interval for top N species today."""
    today = _now().date().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        top = conn.execute(
            """SELECT common_name FROM detections WHERE date = ?
               GROUP BY common_name ORDER BY COUNT(*) DESC LIMIT ?""",
            (today, top_n)
        ).fetchall()
        names = [r[0] for r in top]
        if not names:
            conn.close()
            return {}
        ph = ','.join('?' * len(names))
        rows = conn.execute(
            f"""SELECT common_name,
                CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) * 60 +
                (CAST(strftime('%M', timestamp, 'localtime') AS INTEGER) / {bucket_minutes}) * {bucket_minutes} AS bucket,
                COUNT(*) as cnt
                FROM detections
                WHERE date = ? AND common_name IN ({ph})
                AND CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) BETWEEN ? AND ?
                GROUP BY common_name, bucket""",
            [today] + names + [start_hour, end_hour]
        ).fetchall()
        conn.close()

    # Build result: {name: {bucket_minutes: count}}
    from collections import defaultdict
    data = {n: defaultdict(int) for n in names}
    for name, bucket, cnt in rows:
        data[name][bucket] = cnt

    # Convert to list of [timestamp_ms, count] pairs
    # Use UTC midnight so JS Date.now() comparisons work correctly
    import datetime
    today_local = _now().date()
    # Get UTC timestamp for local midnight
    local_midnight = datetime.datetime.combine(today_local, datetime.time(0, 0))
    try:
        import pytz
        local_tz = pytz.timezone(os.getenv("TZ", "Europe/London"))
        local_midnight_aware = local_tz.localize(local_midnight)
        base_ms = int(local_midnight_aware.timestamp() * 1000)
    except Exception:
        base_ms = int(local_midnight.timestamp() * 1000)

    result = {}
    for name in names:
        result[name] = []
        for b in range(start_hour * 60, (end_hour + 1) * 60, bucket_minutes):
            h = b // 60
            if h > end_hour:
                break
            ts_ms = base_ms + b * 60000
            result[name].append([ts_ms, data[name].get(b, 0)])
    return result
