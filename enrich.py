"""Look up airframe details, routes and attributed photos, caching to disk.

Sources (all free, no key):
  - planespotters.net public API: photos with photographer credit
  - adsbdb.com: registration/type/operator, and callsign -> route
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

from sketch import STYLES, cutout

log = logging.getLogger(__name__)

USER_AGENT = "overhead/1.0 (personal ADS-B display)"
# Bump when the drawing style changes so cached airframes are redrawn.
RENDER_VERSION = 4
ROUTE_TTL = 6 * 3600
MISS_TTL = 24 * 3600


class Enricher:
    def __init__(self, cache_dir: Path, style: str = "watercolour"):
        self.draw_fn = STYLES[style]
        self.render_key = f"{RENDER_VERSION}-{style}"
        self.cache_dir = cache_dir
        self.img_dir = cache_dir / "img"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.photo_dir = cache_dir / "photos"
        self.photo_dir.mkdir(exist_ok=True)
        self.db_path = cache_dir / "airframes.json"
        self.routes_path = cache_dir / "routes.json"
        self.airframes = self._load(self.db_path)
        self.routes = self._load(self.routes_path)
        self.lock = threading.Lock()
        self.pending: set[str] = set()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self):
        with self.lock:
            self.db_path.write_text(json.dumps(self.airframes))
            self.routes_path.write_text(json.dumps(self.routes))

    def _get_json(self, url: str):
        try:
            r = self.http.get(url, timeout=10)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("fetch failed %s: %s", url, e)
        return None

    # --- public -----------------------------------------------------------

    def lookup(self, hex_id: str, callsign: str | None) -> tuple[dict, dict]:
        """Return whatever is cached now; schedule fetches for anything missing."""
        now = time.time()
        frame = self.airframes.get(hex_id)
        route = self.routes.get(callsign) if callsign else None

        need_frame = (
            frame is None
            or (frame.get("miss") and now - frame["fetched"] > MISS_TTL)
            or (not frame.get("miss") and frame.get("render") != self.render_key)
        )
        need_route = bool(callsign) and (route is None or now - route["fetched"] > ROUTE_TTL)
        if need_frame or need_route:
            key = f"{hex_id}|{callsign}"
            with self.lock:
                if key not in self.pending:
                    self.pending.add(key)
                    self.pool.submit(self._fetch, key, hex_id, callsign, need_frame, need_route)
        return frame or {}, route or {}

    def image_path(self, hex_id: str) -> Path:
        return self.img_dir / f"{hex_id}.png"

    # --- background work --------------------------------------------------

    def _fetch(self, key, hex_id, callsign, need_frame, need_route):
        try:
            frame = self._fetch_airframe(hex_id) if need_frame else None
            route = self._fetch_route(callsign) if need_route else None
            with self.lock:
                if frame is not None:
                    self.airframes[hex_id] = frame
                if route is not None:
                    self.routes[callsign] = route
            self._save()
        except Exception:
            log.exception("enrichment failed for %s", key)
        finally:
            with self.lock:
                self.pending.discard(key)

    def _fetch_airframe(self, hex_id: str) -> dict:
        frame: dict = {"fetched": time.time(), "render": self.render_key}

        data = self._get_json(f"https://api.adsbdb.com/v0/aircraft/{hex_id}")
        ac = (data or {}).get("response", {}).get("aircraft") if isinstance(data, dict) else None
        if isinstance(ac, dict):
            frame.update(
                registration=ac.get("registration"),
                icao_type=ac.get("icao_type"),
                type_name=" ".join(filter(None, [ac.get("manufacturer"), ac.get("type")])),
                owner=ac.get("registered_owner"),
            )

        photo = self._fetch_planespotters(hex_id)
        if not photo and isinstance(ac, dict) and ac.get("url_photo"):
            photo = {"src": ac["url_photo"], "credit": "airport-data.com", "link": ac["url_photo"]}

        aspect = self._draw(hex_id, photo["src"]) if photo else None
        if aspect:
            frame["image_aspect"] = aspect
            frame["photo_credit"] = photo["credit"]
            frame["photo_link"] = photo["link"]
            frame["has_image"] = True
        if not isinstance(ac, dict) and not photo:
            frame["miss"] = True
        return frame

    def _fetch_planespotters(self, hex_id: str) -> dict | None:
        data = self._get_json(f"https://api.planespotters.net/pub/photos/hex/{hex_id}")
        photos = (data or {}).get("photos") or []
        if not photos:
            return None
        p = photos[0]
        src = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
        if not src:
            return None
        return {
            "src": src,
            "credit": f"© {p.get('photographer', 'unknown')} / planespotters.net",
            "link": p.get("link", "https://www.planespotters.net"),
        }

    def _draw(self, hex_id: str, url: str) -> float | None:
        """Cut out and ink the photo; returns the drawing's width/height."""
        raw = self.photo_dir / f"{hex_id}.jpg"
        try:
            if not raw.exists():
                r = self.http.get(url, timeout=15)
                r.raise_for_status()
                raw.write_bytes(r.content)
            drawing = self.draw_fn(cutout(Image.open(raw)), seed=hex_id)
            drawing.save(self.image_path(hex_id), optimize=True)
            return round(drawing.width / drawing.height, 3)
        except Exception as e:
            log.warning("could not draw %s: %s", hex_id, e)
            return None

    def _fetch_route(self, callsign: str) -> dict:
        route: dict = {"fetched": time.time()}
        data = self._get_json(f"https://api.adsbdb.com/v0/callsign/{callsign}")
        fr = (data or {}).get("response", {}).get("flightroute") if isinstance(data, dict) else None
        if isinstance(fr, dict):
            def place(a):
                a = a or {}
                return {"iata": a.get("iata_code"), "icao": a.get("icao_code"),
                        "city": a.get("municipality") or a.get("name")}

            route.update(
                origin=place(fr.get("origin")),
                destination=place(fr.get("destination")),
                airline=(fr.get("airline") or {}).get("name"),
            )
        return route
