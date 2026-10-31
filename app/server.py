"""
server.py  –  Flask web server + WebSocket (SocketIO) for the live dashboard.
Also exposes a /api/control endpoint so Node-RED can pause/resume detection.
"""

import os
import logging
from flask import Flask, render_template, jsonify, request, send_file
from pathlib import Path
from flask_socketio import SocketIO
import database

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.config["SECRET_KEY"] = "birdwatch-key-change-me"
# ping_timeout 60 (default 20): a busy moment no longer shows "disconnected"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", logger=False,
                    engineio_logger=False, ping_interval=25, ping_timeout=60)

# Injected by main.py
_pause_fn = None
_resume_fn = None
_status_fn = None


def register_controls(pause_fn, resume_fn, status_fn):
    global _pause_fn, _resume_fn, _status_fn
    _pause_fn = pause_fn
    _resume_fn = resume_fn
    _status_fn = status_fn


def emit_detection(data: dict):
    """Push a live detection event to all connected dashboard clients."""
    socketio.emit("detection", data)


# ── HTML ──────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    state = _status_fn() if _status_fn else "unknown"
    return jsonify({"status": "ok", "state": state})


@app.route("/api/control", methods=["POST"])
def api_control():
    """
    Node-RED calls this to pause / resume detection without stopping the container.
    POST body: { "action": "pause" | "resume" }
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action", "").lower()

    if action == "pause" and _pause_fn:
        _pause_fn()
        return jsonify({"ok": True, "state": "paused"})
    elif action == "resume" and _resume_fn:
        _resume_fn()
        return jsonify({"ok": True, "state": "running"})
    else:
        return jsonify({"ok": False, "error": "Unknown action. Use 'pause' or 'resume'."}), 400


@app.route("/api/detections")
def api_detections():
    limit  = min(int(request.args.get("limit", 50)), 500)
    camera = request.args.get("camera", None)
    if camera:
        return jsonify(database.get_recent_detections_by_camera(camera, limit))
    return jsonify(database.get_recent_detections(limit))


@app.route("/api/detections/today")
def api_detections_today():
    return jsonify(database.get_today_detections())


@app.route("/api/detections/today/count")
def api_detections_today_count():
    return jsonify({"count": database.get_today_count()})


@app.route("/api/species")
def api_species():
    return jsonify(database.get_today_species())


@app.route("/api/species/all")
def api_species_all():
    return jsonify(database.get_all_time_species())


@app.route("/api/activity")
def api_activity():
    return jsonify(database.get_hourly_activity())


@app.route("/api/cameras")
def api_cameras():
    return jsonify(database.get_cameras())


@app.route("/api/config")
def api_config():
    return jsonify({
        "feed_cap": int(os.getenv("FEED_CAP", "50")), "feed_default": int(os.getenv("FEED_DEFAULT", "7")),
    })


@app.route("/api/sightings")
def api_sightings():
    return jsonify({"sightings": database.get_today_sightings()})


@app.route("/api/species/<path:common_name>/detections")
def api_species_detections(common_name):
    return jsonify(database.get_species_detections_today(common_name))


@app.route("/api/species-hourly")
def api_species_hourly():
    n = min(int(request.args.get('n', 12)), 100)
    bucket = int(request.args.get('bucket', 60))
    start_h = int(request.args.get('start', 0))
    end_h = int(request.args.get('end', 23))
    return jsonify(database.get_species_bucketed(top_n=n, bucket_minutes=bucket,
                                                  start_hour=start_h, end_hour=end_h))


@app.route("/api/loudness", methods=["GET", "POST"])
def api_loudness():
    import main as _main
    if request.method == "POST":
        val = request.json.get("target", "-14")
        _main.AUDIO_LOUDNESS_TARGET = str(val)
        return jsonify({"target": _main.AUDIO_LOUDNESS_TARGET})
    return jsonify({"target": getattr(_main, "AUDIO_LOUDNESS_TARGET", "-14")})


# ── Map page ──────────────────────────────────────────────────────────────────

@app.route("/map")
def map_page():
    return render_template("map.html")


@app.route("/api/map/layout", methods=["GET", "PUT"])
def api_map_layout():
    import birdmap
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        try:
            birdmap.save_layout(body.get("cameras", []), body.get("north"))
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"Bad layout: {e}"}), 400
        return jsonify({"ok": True})
    layout = birdmap.get_layout()
    layout["latitude"] = float(os.getenv("LATITUDE", "54.93"))     # same defaults as main.py
    layout["longitude"] = float(os.getenv("LONGITUDE", "-2.73"))
    layout["known_cameras"] = database.get_cameras()
    return jsonify(layout)


@app.route("/api/map/background", methods=["GET", "POST"])
def api_map_background():
    import birdmap
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file"}), 400
        try:
            birdmap.save_background(f)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True})
    path = birdmap.background_path()
    if not path:
        return jsonify({"error": "no background image"}), 404
    resp = send_file(str(path))
    resp.headers["Cache-Control"] = "public, max-age=604800"   # URL carries a version
    return resp


@app.route("/api/map/timeline")
def api_map_timeline():
    import birdmap
    return jsonify(birdmap.get_timeline(request.args.get("date")))


@app.route("/api/map/stats")
def api_map_stats():
    import birdmap
    return jsonify(birdmap.get_stats(request.args.get("period", "today"),
                                     request.args.get("species") or None))


# ── Run ───────────────────────────────────────────────────────────────────────

@app.route("/api/taxa-urls")
def api_taxa_urls():
    """Return stored iNaturalist URLs for all known species."""
    import images as img_module
    try:
        import json
        if img_module.TAXA_FILE.exists():
            return app.response_class(
                img_module.TAXA_FILE.read_text(),
                mimetype="application/json"
            )
    except Exception:
        pass
    return jsonify({})


@app.route("/api/audio/<path:filename>")
def api_audio(filename: str):
    """Serve a captured audio chunk.

    Chunks are stored under /data/audio/<camera_name>/<chunk>.wav.
    The DB only stores the bare filename, so we search all subdirectories.
    """
    from pathlib import Path
    from eventlet import tpool

    def _find():                       # real thread: searching the shared folder is slow
        path = Path("/data/audio") / filename
        if path.exists() and path.suffix == ".wav":
            return path
        name = Path(filename).name     # bare filename: search the camera folders
        for wav in Path("/data/audio").rglob(name):
            if wav.suffix == ".wav":
                return wav
        return None

    found = tpool.execute(_find)
    if found:
        return send_file(str(found), mimetype="audio/wav")
    return jsonify({"error": "not found"}), 404


@app.route("/api/bird-image/<path:name>")
def api_bird_image(name: str):
    """Serve cached bird image, or 404 if not yet downloaded."""
    import images as img_module
    path = img_module.get_local_path(name)
    if path.exists():
        resp = send_file(str(path), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    resp = jsonify({"error": "not found"})
    resp.headers["Cache-Control"] = "no-store"
    return resp, 404


def run_server(host: str = "0.0.0.0", port: int = 8080):
    logger.info(f"Dashboard available at http://{host}:{port}")
    socketio.run(app, host=host, port=port, log_output=False)
