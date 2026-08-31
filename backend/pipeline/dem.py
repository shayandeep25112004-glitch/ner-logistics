"""AWS Terrain Tiles (Copernicus GLO-90 DEM) downloader and sampler.

Downloads Terrarium-format elevation tiles from AWS Terrain Tiles (public S3, no API key,
no quota) at zoom 10 and samples elevations locally via bilinear interpolation.
Falls back to Open-Meteo elevation API if a tile cannot be decoded or fetched.
"""

from __future__ import annotations

import io
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence
import requests

from config import DATA_DIR, ELEVATION_API, ELEVATION_BATCH

TILE_DIR = DATA_DIR / "processed" / "dem_tiles"
TILE_DIR.mkdir(parents=True, exist_ok=True)

AWS_TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ZOOM = 10

# In-memory tile cache to avoid reading disk repeatedly
_TILE_CACHE: dict[tuple[int, int, int], list[list[float]]] = {}


def deg2num(lat_deg: float, lon_deg: float, zoom: int = ZOOM) -> tuple[int, int, float, float]:
    """Convert lat/lon in degrees to tile x, y indices and fractional pixel offsets (0..255)."""
    lat_rad = math.radians(lat_deg)
    n = 1 << zoom
    xtile_f = (lon_deg + 180.0) / 360.0 * n
    ytile_f = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    x = int(math.floor(xtile_f))
    y = int(math.floor(ytile_f))
    px = (xtile_f - x) * 256.0
    py = (ytile_f - y) * 256.0
    return x, y, px, py


def get_tile_path(z: int, x: int, y: int) -> Path:
    return TILE_DIR / f"{z}_{x}_{y}.png"


def download_tile(z: int, x: int, y: int, timeout: int = 15) -> bytes | None:
    p = get_tile_path(z, x, y)
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes()
    url = AWS_TILE_URL.format(z=z, x=x, y=y)
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "ner-logistics/1.0"})
            if r.status_code == 200 and len(r.content) > 0:
                p.write_bytes(r.content)
                return r.content
            elif r.status_code == 404:
                return None
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


def parse_terrarium_png(png_bytes: bytes) -> list[list[float]] | None:
    """Decode a 256x256 Terrarium PNG into a 256x256 float matrix of elevation in metres.
    Terrarium elevation formula: (R * 256 + G + B / 256) - 32768
    """
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = im.size
        pixels = list(im.getdata())
        grid = []
        for row in range(h):
            row_vals = []
            for col in range(w):
                r, g, b = pixels[row * w + col]
                ele = (r * 256.0 + g + b / 256.0) - 32768.0
                row_vals.append(ele)
            grid.append(row_vals)
        return grid
    except Exception:
        return None


def get_tile_grid(z: int, x: int, y: int) -> list[list[float]] | None:
    key = (z, x, y)
    if key in _TILE_CACHE:
        return _TILE_CACHE[key]
    data = download_tile(z, x, y)
    if not data:
        return None
    grid = parse_terrarium_png(data)
    if grid:
        _TILE_CACHE[key] = grid
    return grid


def sample_elevation_tile(lat: float, lon: float) -> float | None:
    """Bilinear sample of elevation from zoom 10 AWS Terrain Tiles."""
    x, y, px, py = deg2num(lat, lon, ZOOM)
    grid = get_tile_grid(ZOOM, x, y)
    if not grid:
        return None
    
    # Bilinear interpolation inside 256x256 grid
    x0 = int(math.floor(px))
    y0 = int(math.floor(py))
    x1 = min(x0 + 1, 255)
    y1 = min(y0 + 1, 255)
    x0 = max(0, min(x0, 255))
    y0 = max(0, min(y0, 255))
    
    dx = px - x0
    dy = py - y0
    
    q00 = grid[y0][x0]
    q10 = grid[y0][x1]
    q01 = grid[y1][x0]
    q11 = grid[y1][x1]
    
    val = (q00 * (1 - dx) * (1 - dy) +
           q10 * dx * (1 - dy) +
           q01 * (1 - dx) * dy +
           q11 * dx * dy)
    return round(val, 1)


def fetch_open_meteo_batch(coords: Sequence[tuple[float, float]]) -> list[float | None]:
    """Fallback batch fetcher for Open-Meteo elevation API (max 100 coords per call)."""
    if not coords:
        return []
    lats = ",".join(f"{c[0]:.5f}" for c in coords)
    lons = ",".join(f"{c[1]:.5f}" for c in coords)
    try:
        r = requests.get(f"{ELEVATION_API}?latitude={lats}&longitude={lons}", timeout=20)
        if r.status_code == 200:
            data = r.json()
            return data.get("elevation", [None] * len(coords))
    except Exception:
        pass
    return [None] * len(coords)


def sample_elevation(lat: float, lon: float) -> float:
    """Get elevation in metres for a coordinate point."""
    ele = sample_elevation_tile(lat, lon)
    if ele is not None:
        return ele
    res = fetch_open_meteo_batch([(lat, lon)])
    if res and res[0] is not None:
        return float(res[0])
    # Fallback estimate based on typical NER elevation
    return 150.0


def preload_ner_tiles(bbox: tuple[float, float, float, float] = (21.5, 87.5, 30.0, 97.5), max_workers: int = 16) -> int:
    """Preload all zoom 10 AWS Terrain Tiles for the NER bounding box."""
    min_lat, min_lon, max_lat, max_lon = bbox
    min_x, max_y, _, _ = deg2num(min_lat, min_lon, ZOOM)
    max_x, min_y, _, _ = deg2num(max_lat, max_lon, ZOOM)
    
    tiles = []
    for x in range(min_x, max_x + 1):
        for y in range(min_y, max_y + 1):
            p = get_tile_path(ZOOM, x, y)
            if not (p.exists() and p.stat().st_size > 0):
                tiles.append((ZOOM, x, y))
    
    if not tiles:
        return 0
    
    downloaded = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(download_tile, z, x, y): (z, x, y) for z, x, y in tiles}
        for f in as_completed(futures):
            res = f.result()
            if res:
                downloaded += 1
    return downloaded
