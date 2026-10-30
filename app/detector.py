"""
detector.py  –  Analyses a WAV file with BirdNET and returns detections.

BirdNET runs in its own process (detector_worker.py), started once and reused.
The app runs on eventlet (everything shares one thread), and BirdNET's audio
preparation is plain Python, so running it inside the app froze the web page,
the live Socket.IO link and MQTT while each chunk was analysed. Talking to a
separate process over a pipe lets eventlet keep serving everything else.

Same interface as before: BirdDetector(lat, lon, min_conf).analyze(path).
"""

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

import eventlet

logger = logging.getLogger(__name__)

WORKER = str(Path(__file__).with_name("detector_worker.py"))
STARTUP_TIMEOUT = 300     # first start can download/load the model
ANALYSE_TIMEOUT = 120     # one 15 s chunk normally takes a few seconds


class BirdDetector:
    def __init__(self, latitude: float, longitude: float, min_confidence: float = 0.7):
        self.latitude = latitude
        self.longitude = longitude
        self.min_confidence = min_confidence
        self._lock = threading.Lock()   # one request at a time on the pipe
        self._proc = None
        logger.info("Loading BirdNET model in its own process (this takes ~20 s on first run)…")
        self._start()
        logger.info("✅ BirdNET model loaded")

    # ── worker process ────────────────────────────────────────────────────────
    def _start(self):
        self._proc = subprocess.Popen(
            [sys.executable, "-u", WORKER,
             str(self.latitude), str(self.longitude), str(self.min_confidence)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,   # stderr -> container log
            text=True, bufsize=1,
        )
        with eventlet.Timeout(STARTUP_TIMEOUT):
            line = self._proc.stdout.readline()
        if not line or not json.loads(line).get("ready"):
            raise RuntimeError("BirdNET worker failed to start - see the log above")

    def _stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        self._proc = None

    # ── public ────────────────────────────────────────────────────────────────
    def analyze(self, audio_path: str) -> list[dict]:
        """Analyse a WAV file. Safe to call from any capture thread."""
        if not Path(audio_path).exists():
            logger.warning(f"Audio file not found: {audio_path}")
            return []

        with self._lock:
            for attempt in (1, 2):                   # one automatic restart if the worker died
                try:
                    if self._proc is None or self._proc.poll() is not None:
                        logger.warning("BirdNET worker not running - starting it")
                        self._start()
                    self._proc.stdin.write(json.dumps({"path": audio_path}) + "\n")
                    self._proc.stdin.flush()
                    with eventlet.Timeout(ANALYSE_TIMEOUT):
                        line = self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("worker closed its output")
                    reply = json.loads(line)
                    if not reply.get("ok"):
                        logger.error(f"BirdNET analysis failed on {audio_path}: {reply.get('error')}")
                        return []
                    return reply["detections"]
                except (Exception, eventlet.Timeout) as e:
                    logger.error(f"BirdNET worker problem ({e!r}) - restarting it"
                                 + (", retrying this chunk" if attempt == 1 else ""))
                    self._stop()
            return []
