"""
images.py  –  Downloads bird photos and caches them to disk.
Uses iNaturalist API (more permissive than Wikipedia).
Serves images via Flask at /api/bird-image/<name>
"""

import json
import logging
import re
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

IMAGE_DIR = Path("/data/images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

TAXA_FILE = IMAGE_DIR / "taxa_urls.json"
_taxa_lock = threading.Lock()


def _save_taxon_url(common_name: str, url: str):
    """Persist iNaturalist URL for a species."""
    with _taxa_lock:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if TAXA_FILE.exists():
            try:
                data = json.loads(TAXA_FILE.read_text())
            except Exception:
                pass
        data[common_name] = url
        TAXA_FILE.write_text(json.dumps(data))
        logger.debug(f"💾 Saved taxon URL for {common_name}")


def get_taxon_url(common_name: str) -> str | None:
    """Return stored iNaturalist URL or None."""
    try:
        if TAXA_FILE.exists():
            data = json.loads(TAXA_FILE.read_text())
            return data.get(common_name)
    except Exception:
        pass
    return None


HEADERS = {
    "User-Agent": "BirdWatch/1.0 (home bird detection; contact: birdwatch@localhost)"
}


def _safe_filename(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name) + ".jpg"


def get_local_path(common_name: str) -> Path:
    return IMAGE_DIR / _safe_filename(common_name)


def has_image(common_name: str) -> bool:
    return get_local_path(common_name).exists()


def _find_image_url(common_name: str, species: str) -> str | None:
    """Try iNaturalist first, then Wikipedia as fallback."""

    # 1. iNaturalist taxa API
    #    Use 'q' (not 'taxon_name' — that parameter is ignored by the API).
    #    Fetch a few candidates so we can find an exact scientific-name match.
    try:
        resp = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": species, "rank": "species", "per_page": 5},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            # Prefer the result whose scientific name matches exactly.
            taxon = next(
                (r for r in results if r.get("name", "").lower() == species.lower()),
                results[0] if results else None,
            )
            if taxon:
                returned_name = taxon.get("name", "")
                taxon_id = taxon.get("id")
                # Only persist the URL when we're sure it's the right species.
                if taxon_id and returned_name.lower() == species.lower():
                    taxon_slug = returned_name.replace(" ", "-")
                    _save_taxon_url(common_name, f"https://www.inaturalist.org/taxa/{taxon_id}-{taxon_slug}")
                photo = taxon.get("default_photo", {})
                url = photo.get("medium_url") or photo.get("square_url")
                if url:
                    return url
    except Exception as e:
        logger.debug(f"iNaturalist lookup failed for {common_name}: {e}")

    time.sleep(1)

    # 2. Wikipedia fallback
    for term in [common_name, species]:
        try:
            resp = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": term,
                    "prop": "pageimages",
                    "format": "json",
                    "pithumbsize": 200,
                },
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                pages = list(resp.json()["query"]["pages"].values())
                url = pages[0].get("thumbnail", {}).get("source")
                if url:
                    return url
            time.sleep(2)
        except Exception:
            continue

    return None


def _fetch(common_name: str, species: str):
    """Download and save image for a species. Blocking call."""
    need_image = not has_image(common_name)
    need_taxon = get_taxon_url(common_name) is None
    if not need_image and not need_taxon:
        return
    try:
        url = _find_image_url(common_name, species)
        if not url:
            logger.debug(f"No image found for {common_name}")
            return
        if need_image:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200 and resp.content:
                get_local_path(common_name).write_bytes(resp.content)
                logger.info(f"🖼  Downloaded image for {common_name}")
            else:
                logger.warning(f"Image download failed for {common_name}: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Image fetch error for {common_name}: {e}")


def download_image(common_name: str, species: str):
    """Fire-and-forget download for new detections (non-blocking)."""
    if has_image(common_name) and get_taxon_url(common_name) is not None:
        return
    threading.Thread(
        target=_fetch,
        args=(common_name, species),
        daemon=True,
        name=f"imgdl-{common_name[:20]}"
    ).start()
