#!/usr/bin/env python3
"""One-time fetch of a static OpenStreetMap background for the GPS track map.

Downloads and stitches OSM tiles covering a small area around a center point,
then bakes a PNG + a bounds JSON into app/assets/ so the dashboard renders the
map fully offline at runtime (no API key, no internet on-track).

Run once on a machine with internet, e.g. for the UF test area (the defaults):

    python -m tools.fetch_map

or for your actual venue:

    python -m tools.fetch_map --lat 30.1234 --lon -81.5678 --zoom 16 --radius 2

OSM tile usage policy: this is a light, one-time fetch with an identifying
User-Agent. Keep --radius/--zoom modest (a few dozen tiles); don't script it into
bulk downloads. See https://operations.osmfoundation.org/policies/tiles/
"""
import os
import sys
import json
import argparse
import urllib.request

# Qt needs a platform plugin to load image formats; offscreen has no display dep.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from app.mercator import TILE, lonlat_to_tile, world_px_to_lonlat  # noqa: E402

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "FlareTelemetryCollector/1.0 (one-time track-map fetch)"


def fetch_tile(z: int, x: int, y: int) -> bytes:
    req = urllib.request.Request(
        TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=29.6436, help="center latitude (default: UF)")
    ap.add_argument("--lon", type=float, default=-82.3549, help="center longitude (default: UF)")
    ap.add_argument("--zoom", type=int, default=16, help="OSM zoom level (default: 16)")
    ap.add_argument("--radius", type=int, default=2,
                    help="tiles out from center; grid is (2*r+1)^2 (default: 2 -> 25 tiles)")
    ap.add_argument("--rx", type=int, default=None,
                    help="tiles left/right of center for a wide/landscape map (default: --radius)")
    ap.add_argument("--ry", type=int, default=None,
                    help="tiles up/down of center (default: --radius)")
    args = ap.parse_args()

    QGuiApplication(sys.argv)  # registers Qt image (PNG) plugins

    rx = args.rx if args.rx is not None else args.radius
    ry = args.ry if args.ry is not None else args.radius
    cx, cy = lonlat_to_tile(args.lat, args.lon, args.zoom)
    x0, x1 = cx - rx, cx + rx
    y0, y1 = cy - ry, cy + ry
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    total = cols * rows

    canvas = QImage(cols * TILE, rows * TILE, QImage.Format_RGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    try:
        n = 0
        for xt in range(x0, x1 + 1):
            for yt in range(y0, y1 + 1):
                try:
                    tile = QImage.fromData(fetch_tile(args.zoom, xt, yt))
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED z{args.zoom} x{xt} y{yt}: {exc}", file=sys.stderr)
                    return 1
                painter.drawImage((xt - x0) * TILE, (yt - y0) * TILE, tile)
                n += 1
                print(f"  tile {n}/{total}  z{args.zoom} x{xt} y{yt}")
    finally:
        painter.end()

    assets = os.path.join(os.path.dirname(__file__), "..", "app", "assets")
    assets = os.path.abspath(assets)
    os.makedirs(assets, exist_ok=True)
    png_path = os.path.join(assets, "track_map.png")
    if not canvas.save(png_path, "PNG"):
        print(f"failed to write {png_path}", file=sys.stderr)
        return 1

    # Image top-left / bottom-right in world pixels -> human-readable bounds.
    x0px, y0px = x0 * TILE, y0 * TILE
    x1px, y1px = (x1 + 1) * TILE, (y1 + 1) * TILE
    north, west = world_px_to_lonlat(x0px, y0px, args.zoom)
    south, east = world_px_to_lonlat(x1px, y1px, args.zoom)

    meta = {
        "image": "track_map.png",
        "zoom": args.zoom,
        "origin_px": [x0px, y0px],                     # world-pixel of image top-left
        "size_px": [cols * TILE, rows * TILE],
        "bounds": {"north": north, "south": south, "east": east, "west": west},
        "center": {"lat": args.lat, "lon": args.lon},
    }
    meta_path = os.path.join(assets, "track_map.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nwrote {png_path}  ({cols * TILE}x{rows * TILE}px)")
    print(f"wrote {meta_path}")
    print(f"bounds  N {north:.5f}  S {south:.5f}  W {west:.5f}  E {east:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
