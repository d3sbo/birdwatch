"""
detector.py  –  Wraps birdnetlib to analyse a WAV file and return detections.
The Analyzer is loaded once at startup (heavy model load) and reused.
TFLite interpreter is not thread-safe — a lock serialises concurrent calls.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo('Europe/London')

from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer

logger = logging.getLogger(__name__)


class BirdDetector:
    def __init__(self, latitude: float, longitude: float, min_confidence: float = 0.7):
        self.latitude = latitude
        self.longitude = longitude
        self.min_confidence = min_confidence
        self._lock = threading.Lock()   # TFLite interpreter is not thread-safe

        logger.info("Loading BirdNET model (this takes ~20 s on first run)…")
        self.analyzer = Analyzer()
        logger.info("✅ BirdNET model loaded")

    def analyze(self, audio_path: str) -> list[dict]:
        """
        Analyse a WAV file and return a list of detections.
        Serialised via lock — safe to call from multiple threads.
        """
        if not Path(audio_path).exists():
            logger.warning(f"Audio file not found: {audio_path}")
            return []

        with self._lock:
            try:
                recording = Recording(
                    self.analyzer,
                    audio_path,
                    lat=self.latitude,
                    lon=self.longitude,
                    date=datetime.now(tz=LONDON),
                    min_conf=self.min_confidence,
                )
                recording.analyze()

                results = []
                seen: set[str] = set()

                for det in recording.detections:
                    name = det["common_name"]
                    if name in seen:
                        continue
                    seen.add(name)
                    results.append(
                        {
                            "common_name": name,
                            "species": det["scientific_name"],
                            "confidence": round(float(det["confidence"]), 3),
                            "start_time": det["start_time"],
                            "end_time": det["end_time"],
                        }
                    )

                return results

            except Exception as e:
                logger.error(f"BirdNET analysis failed on {audio_path}: {e}")
                return []
