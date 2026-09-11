"""
capture.py  –  Grabs rolling audio chunks from a stream via ffmpeg.
Supports HTTP (go2rtc /api/stream.mp4) and RTSP URLs.
Each completed chunk is passed to registered callbacks for analysis.
Handles stream drops and reconnects automatically.
"""

import os
import subprocess
import threading
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioCapture:
    def __init__(
        self,
        camera_url: str,
        camera_name: str,
        chunk_seconds: int = 15,
        output_dir: str = "/data/audio",
    ):
        self.camera_url = camera_url
        self.camera_name = camera_name
        self.chunk_seconds = chunk_seconds
        self.output_dir = Path(output_dir) / camera_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._paused = False
        self._callbacks: list = []
        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def add_callback(self, cb):
        self._callbacks.append(cb)

    def start(self):
        self._running = True
        self._paused = False
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"capture-{self.camera_name}",
        )
        self._thread.start()
        logger.info(f"[{self.camera_name}] Capture thread started")

    def stop(self):
        self._running = False
        logger.info(f"[{self.camera_name}] Capture thread stopping")

    def pause(self):
        self._paused = True
        logger.info(f"[{self.camera_name}] Capture paused")

    def resume(self):
        self._paused = False
        logger.info(f"[{self.camera_name}] Capture resumed")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _capture_loop(self):
        consecutive_failures = 0

        while self._running:
            if self._paused:
                time.sleep(2)
                continue

            output_file = self.output_dir / f"chunk_{int(time.time())}.wav"

            try:
                # Audio normalisation — target loudness from env var
                # Set AUDIO_LOUDNESS_TARGET=off to disable
                import main as _main
                loudness_target = getattr(_main, 'AUDIO_LOUDNESS_TARGET', os.getenv("AUDIO_LOUDNESS_TARGET", "-14"))
                audio_filters = []
                if loudness_target.lower() != "off":
                    audio_filters.append(f"loudnorm=I={loudness_target}:TP=-1.5:LRA=11")

                # Build ffmpeg command — no RTSP-specific flags needed for HTTP
                cmd = [
                    "ffmpeg",
                    "-loglevel", "error",
                    "-i", self.camera_url,
                    "-vn",                   # audio only
                    "-acodec", "pcm_s16le",  # raw PCM — BirdNET requirement
                    "-ar", "48000",          # 48 kHz sample rate
                    "-ac", "1",              # mono
                    "-t", str(self.chunk_seconds),
                ]
                if audio_filters:
                    cmd += ["-af", ",".join(audio_filters)]
                cmd += ["-y", str(output_file)]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.chunk_seconds + 30,
                )

                if result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                    consecutive_failures = 0
                    for cb in self._callbacks:
                        try:
                            cb(str(output_file), self.camera_name)
                        except Exception as e:
                            logger.error(f"[{self.camera_name}] Callback error: {e}")
                    self._cleanup_old_chunks()
                else:
                    err = result.stderr.decode(errors="replace")[:300]
                    logger.warning(f"[{self.camera_name}] ffmpeg failed (rc={result.returncode}): {err}")
                    output_file.unlink(missing_ok=True)
                    consecutive_failures += 1
                    time.sleep(min(5 * consecutive_failures, 60))

            except subprocess.TimeoutExpired:
                logger.warning(f"[{self.camera_name}] ffmpeg capture timed out")
                output_file.unlink(missing_ok=True)
                consecutive_failures += 1
                time.sleep(10)
            except Exception as e:
                logger.error(f"[{self.camera_name}] Unexpected capture error: {e}")
                output_file.unlink(missing_ok=True)
                time.sleep(10)

    def _cleanup_old_chunks(self, max_age_hours: float = 24, hard_cap: int = 500):
        """Delete chunks older than max_age_hours; also enforce a hard_cap on count."""
        chunks = sorted(self.output_dir.glob("chunk_*.wav"), key=lambda f: f.stat().st_mtime)
        cutoff = time.time() - max_age_hours * 3600
        for f in chunks:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
        # Hard cap — trim oldest if still over limit
        remaining = sorted(self.output_dir.glob("chunk_*.wav"), key=lambda f: f.stat().st_mtime)
        for old in remaining[:-hard_cap] if len(remaining) > hard_cap else []:
            old.unlink(missing_ok=True)
