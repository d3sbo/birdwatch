"""
detector_worker.py  –  BirdNET in its own process.

Started once by detector.py. It loads the model, prints READY, then answers one
request per line on stdin:
    {"path": "/data/audio/Cam/chunk_123.wav"}
with one line of JSON on its protocol channel:
    {"ok": true, "detections": [...]}

Why a separate process: BirdNET does a lot of its work (reading and preparing
the audio) in plain Python, and Python only runs one thread of Python at a time.
Inside the main app that froze the web page, the live Socket.IO link and MQTT
for every chunk. In its own process it can't hold anything else up.
"""

import inspect
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# birdnetlib prints progress ("read_audio_data ...") to stdout. Keep a private
# copy of the real stdout for replies and send everything else to stderr, so the
# chatter still reaches the container log but can't corrupt a reply.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

from birdnetlib import Recording            # noqa: E402  (after the redirect)
from birdnetlib.analyzer import Analyzer     # noqa: E402

LONDON = ZoneInfo("Europe/London")

# Species always allowed through, even when BirdNET's location/date filter
# leaves them out. That filter is built from eBird checklists, where night
# birds are badly under-reported, so UK owls never make the list.
# Scientific names, comma-separated. Set ALWAYS_INCLUDE= (empty) to disable.
ALWAYS_INCLUDE = {
    s.strip() for s in os.getenv(
        "ALWAYS_INCLUDE",
        "Strix aluco,Tyto alba,Athene noctua,Asio otus,Asio flammeus",
    ).split(",") if s.strip()
}
# Confidence needed for those species (defaults to MIN_CONFIDENCE if unset).
ALWAYS_INCLUDE_MIN_CONF = os.getenv("ALWAYS_INCLUDE_MIN_CONFIDENCE", "").strip()

# Older birdnetlib versions can't return the detections the location filter
# removed; on those the always-include list simply has no effect.
_SUPPORTS_ALL = "return_all_detections" in inspect.signature(Recording.__init__).parameters


def _reply(obj):
    _proto.write(json.dumps(obj) + "\n")
    _proto.flush()


def main():
    lat, lon, min_conf = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    extra = ALWAYS_INCLUDE if _SUPPORTS_ALL else set()
    extra_conf = float(ALWAYS_INCLUDE_MIN_CONF) if ALWAYS_INCLUDE_MIN_CONF else min_conf
    floor = min(min_conf, extra_conf) if extra else min_conf
    if ALWAYS_INCLUDE and not _SUPPORTS_ALL:
        print("WARNING: this birdnetlib can't do ALWAYS_INCLUDE - ignoring it", flush=True)
    print(f"Always-include species: {sorted(extra) or 'none'} (min confidence {extra_conf})", flush=True)
    analyzer = Analyzer()
    _reply({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            path = json.loads(line)["path"]
            kwargs = {"return_all_detections": True} if extra else {}
            recording = Recording(analyzer, path, lat=lat, lon=lon,
                                  date=datetime.now(tz=LONDON), min_conf=floor, **kwargs)
            recording.analyze()
            results, seen = [], set()
            for det in recording.detections:
                name = det["common_name"]
                conf = float(det["confidence"])
                if det["scientific_name"] in extra:
                    if conf < extra_conf:            # always-include species: own threshold
                        continue
                elif conf < min_conf or not det.get("is_predicted_for_location_and_date", True):
                    continue                         # everyone else: filtered exactly as before
                if name in seen:
                    continue
                seen.add(name)
                results.append({
                    "common_name": name,
                    "species": det["scientific_name"],
                    "confidence": round(float(det["confidence"]), 3),
                    "start_time": det["start_time"],
                    "end_time": det["end_time"],
                })
            _reply({"ok": True, "detections": results})
        except Exception as e:                       # never let one bad file kill the worker
            _reply({"ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
