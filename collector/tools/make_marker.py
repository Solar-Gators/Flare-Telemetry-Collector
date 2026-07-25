#!/usr/bin/env python3
"""Turn a logo image (e.g. albert.jpg) into a transparent, cropped map marker.

JPEGs have no alpha, so a "transparent" logo actually ships with a solid/
checkerboard background baked in. This flood-fills the light background from the
image border inward (stopping at the logo's dark outline, so interior whites like
teeth/eyes survive), then crops to the logo and writes app/assets/gator.png,
which the dashboard's TrackMap uses as the current-position marker.

    python -m tools.make_marker                 # albert.jpg -> app/assets/gator.png
    python -m tools.make_marker --src logo.png --thresh 180
"""
import os
import sys
import argparse
from collections import deque

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QGuiApplication, QImage, qRgba  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="albert.jpg", help="source logo image")
    ap.add_argument("--out", default=os.path.join("app", "assets", "gator.png"))
    ap.add_argument("--thresh", type=int, default=190,
                    help="a pixel counts as background if min(R,G,B) >= this (default 190)")
    args = ap.parse_args()

    QGuiApplication(sys.argv)

    img = QImage(args.src)
    if img.isNull():
        print(f"could not load {args.src}", file=sys.stderr)
        return 1
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()

    def is_bg(x, y):
        c = img.pixelColor(x, y)
        return min(c.red(), c.green(), c.blue()) >= args.thresh

    # BFS flood fill from every border pixel; clear alpha on connected background.
    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if not seen[i] and is_bg(x, y):
                seen[i] = 1; q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if not seen[i] and is_bg(x, y):
                seen[i] = 1; q.append((x, y))

    transparent = qRgba(0, 0, 0, 0)
    cleared = 0
    while q:
        x, y = q.popleft()
        img.setPixel(x, y, transparent)
        cleared += 1
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                i = ny * w + nx
                if not seen[i] and is_bg(nx, ny):
                    seen[i] = 1; q.append((nx, ny))

    # Crop to the remaining (opaque) logo so the marker isn't mostly empty.
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 0:
                minx = min(minx, x); maxx = max(maxx, x)
                miny = min(miny, y); maxy = max(maxy, y)
    if maxx < 0:
        print("nothing left after background removal — try a lower --thresh", file=sys.stderr)
        return 1
    cropped = img.copy(QRect(minx, miny, maxx - minx + 1, maxy - miny + 1))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if not cropped.save(args.out, "PNG"):
        print(f"failed to write {args.out}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}  {cropped.width()}x{cropped.height()}  "
          f"(cleared {cleared} bg px of {w*h})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
