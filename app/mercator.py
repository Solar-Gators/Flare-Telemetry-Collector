"""Web Mercator (EPSG:3857) helpers — the projection OpenStreetMap tiles use.

Shared by tools/fetch_map.py (which stitches a static OSM background) and the
GUI's TrackMap widget (which plots the GPS trail onto that background). Using the
same projection in both places is what makes the trail line up with the map.

Coordinates are in "world pixels": the whole earth at a given zoom is a square of
TILE * 2**zoom pixels, origin at the top-left (lon -180, lat +85.05).
"""
import math

TILE = 256  # OSM tile edge, pixels


def lonlat_to_world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """(lat, lon)° -> (x, y) world-pixel coords at `zoom`."""
    n = TILE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def world_px_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """(x, y) world-pixel coords at `zoom` -> (lat, lon)°. Inverse of the above."""
    n = TILE * (2 ** zoom)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def lonlat_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """(lat, lon)° -> integer (tile_x, tile_y) containing that point at `zoom`."""
    x, y = lonlat_to_world_px(lat, lon, zoom)
    return int(x // TILE), int(y // TILE)
