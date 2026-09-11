"""
main.py  –  BirdWatch entry point.

Startup sequence:
  1. Load config from environment variables
  2. Initialise SQLite DB
  3. Load BirdNET model (GPU)
  4. Connect to MQTT broker (optional)
  5. Start one AudioCapture thread per camera
  6. Start Flask/SocketIO web server (blocking)
"""

# Must be first — eventlet needs to patch stdlib before anything else imports
# threading, socket, etc. Without this, socketio.emit() from background
# capture threads silently fails and the live feed never updates.
import eventlet
eventlet.monkey_patch()

import os
import sys
import signal
import logging
import threading
import time
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("birdwatch")

# ── Local modules ─────────────────────────────────────────────────────────────
import database
import images as img_module
from capture import AudioCapture
from detector import BirdDetector
import server
from mqtt_client import MQTTClient


# ── Config ────────────────────────────────────────────────────────────────────
LATITUDE      = float(os.getenv("LATITUDE",       "54.93"))
LONGITUDE     = float(os.getenv("LONGITUDE",      "-2.73"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.70"))
CHUNK_SECONDS  = int(os.getenv("CHUNK_SECONDS",   "15"))

MQTT_HOST     = os.getenv("MQTT_HOST",     "")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER     = os.getenv("MQTT_USER",     "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_PREFIX   = os.getenv("MQTT_TOPIC_PREFIX", "birdwatch")


def load_cameras() -> list[dict]:
    cameras = []
    i = 1
    while True:
        url  = os.getenv(f"CAMERA_{i}_URL")
        name = os.getenv(f"CAMERA_{i}_NAME", f"Camera_{i}")
        if not url:
            break
        cameras.append({"url": url, "name": name})
        i += 1
    return cameras


# ── State ─────────────────────────────────────────────────────────────────────
_paused = False
_captures: list[AudioCapture] = []


def pause_all():
    global _paused
    _paused = True
    for cap in _captures:
        cap.pause()
    logger.info("⏸  Detection paused")
    if mqtt:
        mqtt.publish_status("paused")


def resume_all():
    global _paused
    _paused = False
    for cap in _captures:
        cap.resume()
    logger.info("▶  Detection resumed")
    if mqtt:
        mqtt.publish_status("running")


def get_status() -> str:
    return "paused" if _paused else "running"


# ── Audio cleanup ────────────────────────────────────────────────────────────
AUDIO_RETAIN_HOURS = float(os.getenv("AUDIO_RETAIN_HOURS", "24"))
AUDIO_LOUDNESS_TARGET = os.getenv("AUDIO_LOUDNESS_TARGET", "-14")

def _cleanup_old_audio():
    """Delete audio chunks older than AUDIO_RETAIN_HOURS."""
    cutoff = time.time() - AUDIO_RETAIN_HOURS * 3600
    for f in Path("/data/audio").rglob("chunk_*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except Exception:
            pass


# ── Detection callback ────────────────────────────────────────────────────────
def on_audio_chunk(audio_path: str, camera_name: str):
    detections = detector.analyze(audio_path)
    audio_filename = Path(audio_path).name

    if detections:
        for det in detections:
            logger.info(f"🐦 [{camera_name}] {det['common_name']} — {det['confidence']:.0%}")
            database.save_detection(camera_name, det["common_name"], det["species"],
                                    det["confidence"], audio_file=audio_filename)
            img_module.download_image(det["common_name"], det["species"])

            audio_exists = any(Path("/data/audio").rglob(audio_filename))
            event = {
                "camera":      camera_name,
                "common_name": det["common_name"],
                "species":     det["species"],
                "confidence":  det["confidence"],
                "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%S"),
                "audio_file":  audio_filename if audio_exists else None,
            }
            server.emit_detection(event)
            if mqtt:
                mqtt.publish_detection(event)
        _cleanup_old_audio()
    else:
        # No detections — delete the chunk immediately
        Path(audio_path).unlink(missing_ok=True)


# ── MQTT command handler ──────────────────────────────────────────────────────
def on_mqtt_command(command: str):
    if command == "pause":
        pause_all()
    elif command == "resume":
        resume_all()
    elif command == "stop":
        logger.info("Stop command received via MQTT — shutting down")
        os.kill(os.getpid(), signal.SIGTERM)
    else:
        logger.warning(f"Unknown MQTT command: {command}")


# ── Shutdown ──────────────────────────────────────────────────────────────────
def shutdown(sig, frame):
    logger.info(f"Signal {sig} received — shutting down cleanly…")
    for cap in _captures:
        cap.stop()
    if mqtt and mqtt._connected:
        mqtt.publish_status("offline")
    sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    # Ensure data directories exist
    Path("/data/audio").mkdir(parents=True, exist_ok=True)
    database.init_db()

    # Pre-download images for all known species in a background thread
    def _preload_images():
        time.sleep(5)  # wait for everything to settle
        known = database.get_all_time_species()
        logger.info(f"🖼  Pre-loading images for {len(known)} known species...")
        for s in known:
            if not img_module.has_image(s["common_name"]) or img_module.get_taxon_url(s["common_name"]) is None:
                img_module._fetch(s["common_name"], s["species"])
                time.sleep(1)  # 1s gap between requests

    threading.Thread(target=_preload_images, daemon=True, name="img-preload").start()

    # Validate cameras
    cameras = load_cameras()
    if not cameras:
        logger.error("No cameras configured!  Set CAMERA_1_URL (and optionally CAMERA_1_NAME).")
        sys.exit(1)

    logger.info(f"🎥 {len(cameras)} camera(s) configured")
    logger.info(f"📍 Location: {LATITUDE}, {LONGITUDE}")
    logger.info(f"🎯 Min confidence: {MIN_CONFIDENCE:.0%}  |  Chunk: {CHUNK_SECONDS}s")

    # Load BirdNET (slow on first run — downloads model weights)
    detector = BirdDetector(LATITUDE, LONGITUDE, MIN_CONFIDENCE)

    # MQTT
    mqtt = None
    if MQTT_HOST:
        mqtt = MQTTClient(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, MQTT_PREFIX)
        mqtt.on_command(on_mqtt_command)
        mqtt.start()
    else:
        logger.info("MQTT_HOST not set — MQTT disabled")

    # Register pause/resume with the web server
    server.register_controls(pause_all, resume_all, get_status)

    # Start capture threads
    for cam in cameras:
        logger.info(f"Starting capture: {cam['name']}")
        cap = AudioCapture(cam["url"], cam["name"], CHUNK_SECONDS)
        cap.add_callback(on_audio_chunk)
        cap.start()
        _captures.append(cap)

    # Start web server (blocks forever)
    server.run_server()
