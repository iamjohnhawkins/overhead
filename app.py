"""Overhead: a sketchbook page of the aircraft near home, from a local ADS-B feed.

    python app.py            # uses config.toml
    python app.py --config other.toml
"""

import argparse
import logging
import math
import time
import tomllib
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, send_file, send_from_directory

import sketch
from aircraft_sizes import category_for, size_for
from enrich import Enricher

ROOT = Path(__file__).parent
PROBE_PATHS = [
    "/data/aircraft.json",
    "/tar1090/data/aircraft.json",
    "/dump1090-fa/data/aircraft.json",
    "/skyaware/data/aircraft.json",
    "/dump1090/data/aircraft.json",
]

log = logging.getLogger("overhead")


def load_config(path: Path) -> dict:
    cfg = {
        "adsb_url": "http://localhost:8080",
        "radius_km": 35,
        "refresh_seconds": 20,
        "host": "0.0.0.0",
        "port": 8035,
        "place_name": "Above the house",
        "show_on_ground": False,
        "cutout_model": "isnet-general-use",
        "style": "watercolour",
    }
    if path.exists():
        cfg.update(tomllib.loads(path.read_text()))
    else:
        log.warning("%s not found, using defaults", path)
    return cfg


class Feed:
    """Finds and reads the decoder's aircraft.json (and receiver.json for home)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.url: str | None = base if base.endswith(".json") else None
        self.http = requests.Session()

    def _discover(self) -> str:
        for path in PROBE_PATHS:
            url = self.base + path
            try:
                r = self.http.get(url, timeout=3)
                if r.ok and "aircraft" in r.json():
                    log.info("using ADS-B feed at %s", url)
                    return url
            except (requests.RequestException, ValueError):
                pass
        raise RuntimeError(f"no aircraft.json found under {self.base}")

    def aircraft(self) -> list[dict]:
        if not self.url:
            self.url = self._discover()
        r = self.http.get(self.url, timeout=5)
        r.raise_for_status()
        return r.json().get("aircraft", [])

    def receiver_location(self) -> tuple[float, float] | None:
        if not self.url:
            self.url = self._discover()
        try:
            data = self.http.get(self.url.replace("aircraft.json", "receiver.json"), timeout=5).json()
            if data.get("lat") is not None and data.get("lon") is not None:
                return float(data["lat"]), float(data["lon"])
        except (requests.RequestException, ValueError):
            pass
        return None


def distance_bearing(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    """Great-circle distance (km) and initial bearing (deg) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    dist = 2 * 6371.0 * math.asin(math.sqrt(a))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return dist, (math.degrees(math.atan2(y, x)) + 360) % 360


def create_app(cfg: dict) -> Flask:
    app = Flask(__name__, static_folder=None)
    sketch.MODEL = cfg["cutout_model"]
    feed = Feed(cfg["adsb_url"])
    enricher = Enricher(ROOT / "cache", cfg["style"])
    state = {"home": None}

    def home() -> tuple[float, float]:
        if state["home"] is None:
            if "home_lat" in cfg and "home_lon" in cfg:
                state["home"] = (float(cfg["home_lat"]), float(cfg["home_lon"]))
            else:
                state["home"] = feed.receiver_location()
                if state["home"] is None:
                    raise RuntimeError("set home_lat/home_lon in config.toml (receiver.json has no location)")
        return state["home"]

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "static", "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(ROOT / "static", name)

    @app.get("/img/<hex_id>.png")
    def image(hex_id):
        path = enricher.image_path(hex_id.lower())
        if not hex_id.isalnum() or not path.exists():
            abort(404)
        return send_file(path, max_age=86400)

    @app.get("/api/sky")
    def sky():
        try:
            hlat, hlon = home()
            raw = feed.aircraft()
        except Exception as e:
            log.warning("feed error: %s", e)
            return jsonify(error=str(e)), 502

        planes = []
        for ac in raw:
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is None or lon is None or ac.get("seen_pos", 0) > 60:
                continue
            # Skip surface vehicles, obstacles and ground stations.
            if (ac.get("category") or "").startswith("C") or ac.get("t") in ("TWR", "GND"):
                continue
            dist, bearing = distance_bearing(hlat, hlon, lat, lon)
            if dist > cfg["radius_km"]:
                continue

            hex_id = ac["hex"].lower().lstrip("~")
            callsign = (ac.get("flight") or "").strip() or None
            frame, route = enricher.lookup(hex_id, callsign)
            # A drawing in an old style is being redone: hold until it's ready.
            drawn = frame.get("has_image") and frame.get("render") == enricher.render_key

            icao_type = ac.get("t") or frame.get("icao_type")
            span, length = size_for(icao_type, ac.get("category"))
            alt = ac.get("alt_baro", ac.get("altitude"))
            if alt == "ground" and not cfg["show_on_ground"]:
                continue
            planes.append({
                "hex": hex_id,
                "callsign": callsign,
                "registration": ac.get("r") or frame.get("registration"),
                "icao_type": icao_type,
                "category": category_for(icao_type, ac.get("category")),
                "type_name": ac.get("desc") or frame.get("type_name"),
                "operator": route.get("airline") or ac.get("ownOp") or frame.get("owner"),
                "origin": route.get("origin"),
                "destination": route.get("destination"),
                "altitude_ft": 0 if alt == "ground" else alt,
                "on_ground": alt == "ground",
                "vertical_rate": ac.get("baro_rate", ac.get("geom_rate")),
                "speed_kt": ac.get("gs"),
                "track": ac.get("track"),
                "distance_km": round(dist, 2),
                "bearing": round(bearing, 1),
                "wingspan_m": span,
                "length_m": length,
                "image": f"/img/{hex_id}.png?v={frame.get('render')}" if drawn else None,
                "image_aspect": frame.get("image_aspect"),
                "photo_credit": frame.get("photo_credit"),
                "photo_link": frame.get("photo_link"),
            })

        planes.sort(key=lambda p: p["distance_km"])
        return jsonify(
            generated=time.time(),
            home={"lat": hlat, "lon": hlon},
            radius_km=cfg["radius_km"],
            refresh_seconds=cfg["refresh_seconds"],
            place_name=cfg["place_name"],
            aircraft=planes,
        )

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    create_app(cfg).run(host=cfg["host"], port=cfg["port"], threaded=True)


if __name__ == "__main__":
    main()
