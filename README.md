# Overhead

A web page showing the aircraft near home as a page from a spotter's
sketchbook: each one cut out of its photo, painted in a crisp pen-and-wash
watercolour (or `style = "soft"` for a looser wash, `"ink"` for pen and ink) at a size
relative to its real length, with handwritten notes (callsign, type, route,
where it is). Refreshes every 20 s.

- Positions come from your own ADS-B decoder's `aircraft.json` (readsb, tar1090,
  dump1090-fa / PiAware; the path is auto-detected).
- Registration, type and route come from [adsbdb.com](https://www.adsbdb.com).
- Airline logos come from pics.avs.io and flags from [flagcdn.com](https://flagcdn.com),
  cached in `cache/badges/`. The flag shows the country of registration,
  worked out from the aircraft's transponder address (`countries.py`).
- Photos come from [planespotters.net](https://www.planespotters.net), credited to
  their photographer on the page. The aircraft is cut out with
  [rembg](https://github.com/danielgatis/rembg) and painted with Pillow, locally,
  once per airframe (about 1 s on a Mac, several on a Pi), cached in `cache/`.
- The first run downloads the cut-out model (~180 MB) to `~/.rembg/models`.
  On a Pi you need 64-bit Raspberry Pi OS for onnxruntime; on an older or
  low-memory Pi set `cutout_model = "u2netp"` (~5 MB, rougher edges).

## Run it

```sh
git clone … overhead && cd overhead
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # set adsb_url, and home_lat/home_lon if needed
.venv/bin/python app.py
```

Then open `http://<pi>:8035/`. If it runs on the same Pi as the decoder,
`adsb_url = "http://localhost"` (or `http://localhost:8080` for adsb.im/readsb)
is usually right.

Your location is read from the decoder's `receiver.json`; set `home_lat` and
`home_lon` in `config.toml` if that file has none.

## Start on boot

```sh
sudo cp overhead.service /etc/systemd/system/   # edit User/paths first if not /home/pi/overhead
sudo systemctl enable --now overhead
```

## Notes

- Until its photo has been fetched and painted (usually by the next refresh),
  or if no photo exists, an aircraft is shown as a washed-in silhouette:
  airliner, light aircraft or helicopter, depending on its ADS-B category.
- In a busy sky every drawing is shrunk by the same factor, so relative sizes
  stay true. Lengths come from `aircraft_sizes.py`. Unknown types fall back to
  the ADS-B size category.
- Changing the drawing style? Bump `RENDER_VERSION` in `enrich.py` and every
  cached airframe is redrawn from its saved photo.
- `show_on_ground = true` includes taxiing and parked aircraft.
