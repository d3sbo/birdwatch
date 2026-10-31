"""
birdmap.py  –  Data behind the Map page: where each camera is, which way it
faces, the background image, and detection counts / camera-to-camera movements.

Camera positions are stored as fractions of the background image (0..1), so a
higher- or lower-resolution copy of the same image keeps everything in place.
Headings are degrees clockwise from the top of the image; MAP_NORTH (a setting)
says which compass bearing the top of the image points to.

Everything private (the image and the layout) lives in /data/map, never in the
repo. On first run, /data/map/layout.json (if present) seeds the layout table.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from eventlet import tpool

import database
from database import DB_PATH, _lock, _offload, LONDON

MAP_DIR = Path("/data/map")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# How "moved from camera A to camera B" is decided (see get_stats)
MOVE_MIN_S = 15     # sooner than this, both mics are just hearing the same bird
MOVE_MAX_S = 120    # later than this, it's probably a different visit


# ── Tables ───────────────────────────────────────────────────────────────────

@_offload
def init_map():
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS camera_layout (
                name     TEXT PRIMARY KEY,
                x        REAL NOT NULL,      -- 0..1 across the image
                y        REAL NOT NULL,      -- 0..1 down the image
                heading  REAL NOT NULL,      -- degrees clockwise from image-up
                fov      REAL NOT NULL,      -- lens width, degrees
                range    REAL NOT NULL,      -- how far it hears, as a fraction of image width
                model    TEXT
            );
            CREATE TABLE IF NOT EXISTS map_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        empty = conn.execute("SELECT COUNT(*) FROM camera_layout").fetchone()[0] == 0
        seed = MAP_DIR / "layout.json"
        if empty and seed.exists():
            try:
                data = json.loads(seed.read_text())
                _write_layout(conn, data.get("cameras", data if isinstance(data, list) else []))
                if "north" in data:
                    conn.execute("INSERT OR REPLACE INTO map_settings VALUES ('north', ?)", (str(data["north"]),))
            except Exception as e:
                print(f"map: could not import {seed}: {e}")
        conn.commit()
        conn.close()


def _write_layout(conn, cameras):
    conn.execute("DELETE FROM camera_layout")
    for c in cameras:
        conn.execute(
            "INSERT INTO camera_layout (name, x, y, heading, fov, range, model) VALUES (?,?,?,?,?,?,?)",
            (str(c["name"]), float(c["x"]), float(c["y"]), float(c["heading"]) % 360,
             max(5.0, min(360.0, float(c["fov"]))), max(0.005, min(1.0, float(c["range"]))),
             c.get("model")),
        )


# ── Layout ───────────────────────────────────────────────────────────────────

def background_path():
    for ext in IMAGE_EXTS:
        p = MAP_DIR / f"background{ext}"
        if p.exists():
            return p
    return None


@_offload
def get_layout() -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cams = [dict(r) for r in conn.execute("SELECT * FROM camera_layout ORDER BY rowid")]
        north = conn.execute("SELECT value FROM map_settings WHERE key='north'").fetchone()
        conn.close()
    bg = background_path()
    return {
        "cameras": cams,
        "north": float(north[0]) if north else 0.0,
        "background": int(bg.stat().st_mtime) if bg else None,   # used as a cache-buster
    }


@_offload
def save_layout(cameras: list, north: float = None):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        _write_layout(conn, cameras)
        if north is not None:
            conn.execute("INSERT OR REPLACE INTO map_settings VALUES ('north', ?)", (str(float(north) % 360),))
        conn.commit()
        conn.close()
    _stats_cache.clear()


def save_background(file_storage) -> None:
    ext = Path(file_storage.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError("Use a JPG, PNG or WebP image")
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    for old in IMAGE_EXTS:
        (MAP_DIR / f"background{old}").unlink(missing_ok=True)
    file_storage.save(str(MAP_DIR / f"background{ext}"))


# ── Stats ────────────────────────────────────────────────────────────────────

PERIODS = {"today": 0, "week": 6, "month": 29, "all": None}
_stats_cache: dict = {}


def get_stats(period: str = "today", species: str = None) -> dict:
    """Detections per camera, species list, and camera-to-camera movements.

    A movement A -> B is counted when a species is heard on camera B, B hasn't
    heard it in the last MOVE_MAX_S seconds, and the last camera that did hear
    it was A, between MOVE_MIN_S and MOVE_MAX_S seconds earlier (and A hasn't
    heard it since). Two mics hearing the same bird at once doesn't count -
    that's one bird within earshot of both, which the dashboard already groups.
    """
    period = period if period in PERIODS else "today"
    key = (period, species or "")
    ttl = 20 if period == "today" else 300
    hit = _stats_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = tpool.execute(_compute_stats, period, species)
    _stats_cache[key] = (time.time(), value)
    return value


def _compute_stats(period, species):
    days = PERIODS[period]
    where, args = [], []
    if days is not None:
        since = (datetime.now(tz=LONDON).date() - timedelta(days=days)).isoformat()
        where.append("date >= ?")
        args.append(since)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        conn = sqlite3.connect(DB_PATH)
        species_rows = conn.execute(
            f"SELECT common_name, COUNT(*) FROM detections {clause} GROUP BY common_name ORDER BY 2 DESC",
            args).fetchall()
        if species:
            sp_clause = clause + (" AND " if clause else "WHERE ") + "common_name = ?"
            sp_args = args + [species]
        else:
            sp_clause, sp_args = clause, args
        cam_rows = conn.execute(
            f"SELECT camera, COUNT(*) FROM detections {sp_clause} GROUP BY camera", sp_args).fetchall()
        top_rows = conn.execute(
            f"""SELECT camera, common_name, COUNT(*) AS n FROM detections {clause}
                GROUP BY camera, common_name ORDER BY camera, n DESC""", args).fetchall()
        rows = conn.execute(
            f"SELECT common_name, camera, timestamp FROM detections {sp_clause} ORDER BY common_name, timestamp",
            sp_args).fetchall()
        conn.close()

    top = {}
    for cam, name, n in top_rows:
        lst = top.setdefault(cam, [])
        if len(lst) < 5:
            lst.append([name, n])

    moves = {}
    cur_species, last_seen, last_cam = None, {}, None
    for name, cam, ts in rows:
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except Exception:
            continue
        if name != cur_species:
            cur_species, last_seen, last_cam = name, {}, None
        prev_here = last_seen.get(cam)
        if last_cam and last_cam != cam and (prev_here is None or t - prev_here > MOVE_MAX_S):
            gap = t - last_seen[last_cam]
            if MOVE_MIN_S <= gap <= MOVE_MAX_S:
                k = (last_cam, cam)
                moves[k] = moves.get(k, 0) + 1
        last_seen[cam] = t
        last_cam = cam

    return {
        "period": period,
        "species_filter": species,
        "cameras": {c: n for c, n in cam_rows},
        "species": [[n, c] for n, c in species_rows],
        "top": top,
        "moves": [{"from": a, "to": b, "count": n} for (a, b), n in sorted(moves.items(), key=lambda kv: -kv[1])],
    }
