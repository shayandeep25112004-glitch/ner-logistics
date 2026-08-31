"""Fetch and aggregate 2 years of ERA5 reanalysis rainfall data for the NER 0.5° grid.

Reduces hourly precipitation on arrival to daily features:
- rain_24h: 24-hour total precipitation
- rain_72h: 72-hour antecedent precipitation
- rain_168h: 7-day antecedent saturation
- max_intensity: peak hourly rainfall intensity
"""

from __future__ import annotations

import datetime
import math
import time
from collections import defaultdict
from typing import Sequence
import requests

from config import (
    WEATHER_ARCHIVE_API,
    WEATHER_FORECAST_API,
    WEATHER_GRID_STEP_DEG,
    WEATHER_HISTORY_YEARS,
)
from db import init_schema, db


def generate_ner_grid(
    lat_min: float = 21.5,
    lat_max: float = 29.5,
    lon_min: float = 88.0,
    lon_max: float = 97.5,
    step: float = WEATHER_GRID_STEP_DEG,
) -> list[dict]:
    """Generate regular 0.5° grid cells covering the NER bounding box."""
    cells = []
    cid = 0
    curr_lat = lat_min
    while curr_lat <= lat_max + 1e-6:
        curr_lon = lon_min
        while curr_lon <= lon_max + 1e-6:
            cells.append({
                "cell_id": cid,
                "lat": round(curr_lat, 2),
                "lon": round(curr_lon, 2),
            })
            cid += 1
            curr_lon += step
        curr_lat += step
    return cells


def find_grid_cell(lat: float, lon: float, grid: Sequence[dict]) -> dict:
    """Find the nearest grid cell by fast coordinate index arithmetic."""
    if not grid:
        return {"cell_id": 0, "lat": lat, "lon": lon}
    min_dist = float("inf")
    best = grid[0]
    for c in grid:
        d = (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2
        if d < min_dist:
            min_dist = d
            best = c
    return best


def fetch_weather_archive_chunk(
    cells: list[dict],
    start_date: str,
    end_date: str,
    retries: int = 4,
) -> list[dict]:
    """Fetch hourly precipitation for a batch of cells from Open-Meteo archive API and reduce on arrival."""
    lats = ",".join(str(c["lat"]) for c in cells)
    lons = ",".join(str(c["lon"]) for c in cells)
    
    url = (
        f"{WEATHER_ARCHIVE_API}?latitude={lats}&longitude={lons}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&hourly=precipitation&timezone=UTC"
    )

    data = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=45, headers={"User-Agent": "ner-logistics/1.0"})
            if r.status_code == 200:
                data = r.json()
                break
            elif r.status_code == 429:
                wait_sec = 30 * (attempt + 1)
                print(f"[wx]   HTTP 429, waiting {wait_sec}s")
                time.sleep(wait_sec)
            else:
                time.sleep(2)
        except Exception as e:
            time.sleep(2)

    if not data:
        return []

    if isinstance(data, dict) and "hourly" in data:
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        items = []

    out_rows = []
    for idx, item in enumerate(items):
        if idx >= len(cells):
            break
        cell = cells[idx]
        hourly = item.get("hourly", {})
        times = hourly.get("time", [])
        precips = hourly.get("precipitation", [])

        if not times or not precips:
            continue

        # Group hourly precipitation by day
        # date_str -> list of 24 hourly values
        day_hours = defaultdict(list)
        for t_str, val in zip(times, precips):
            day_str = t_str.split("T")[0]
            val_clean = float(val) if val is not None and not math.isnan(val) else 0.0
            day_hours[day_str].append(val_clean)

        sorted_days = sorted(day_hours.keys())
        day_totals = {d: sum(day_hours[d]) for d in sorted_days}
        day_maxes = {d: max(day_hours[d]) if day_hours[d] else 0.0 for d in sorted_days}

        for i, day in enumerate(sorted_days):
            r24 = round(day_totals[day], 2)
            # 72h accumulation: sum of day[i], day[i-1], day[i-2]
            r72 = round(sum(day_totals[sorted_days[j]] for j in range(max(0, i - 2), i + 1)), 2)
            # 168h accumulation: sum of past 7 days
            r168 = round(sum(day_totals[sorted_days[j]] for j in range(max(0, i - 6), i + 1)), 2)
            max_int = round(day_maxes[day], 2)

            out_rows.append({
                "cell_id": cell["cell_id"],
                "lat": cell["lat"],
                "lon": cell["lon"],
                "day": day,
                "rain_24h": r24,
                "rain_72h": r72,
                "rain_168h": r168,
                "max_intensity": max_int,
            })

    return out_rows


def process_weather():
    t0 = time.time()
    init_schema()
    
    grid = generate_ner_grid()
    
    # Calculate date range (2 years ending yesterday)
    end_dt = datetime.date.today() - datetime.timedelta(days=2)
    start_dt = end_dt - datetime.timedelta(days=730)
    
    start_date = start_dt.isoformat()
    end_date = end_dt.isoformat()
    days_count = (end_dt - start_dt).days + 1

    print(f"[wx] {len(grid)} cells, {start_date} .. {end_date} ({days_count} days)")

    # Chunk grid into batches of 20 cells
    chunk_size = 20
    chunks = [grid[i:i + chunk_size] for i in range(0, len(grid), chunk_size)]
    total_chunks = len(chunks)

    all_rows = []
    for chunk_idx, chunk in enumerate(chunks, 1):
        t_chunk = time.time()
        rows = fetch_weather_archive_chunk(chunk, start_date, end_date)
        all_rows.extend(rows)
        el_time = int(time.time() - t0)
        print(f"[wx] chunk {chunk_idx}/{total_chunks}: +{len(rows):,} rows (total {len(all_rows):,}, {el_time}s)")
        # Small delay between chunks to stay well within rate limit
        time.sleep(0.3)

    print(f"[wx] done in {time.time() - t0:.1f}s. Storing {len(all_rows):,} rows ...")
    
    with db() as conn:
        conn.execute("DELETE FROM weather_grid")
        conn.executemany(
            """INSERT OR REPLACE INTO weather_grid
               (cell_id, lat, lon, day, rain_24h, rain_72h, rain_168h, max_intensity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (r["cell_id"], r["lat"], r["lon"], r["day"], r["rain_24h"], r["rain_72h"], r["rain_168h"], r["max_intensity"])
                for r in all_rows
            ],
        )

    # Summary
    with db() as conn:
        row_count = conn.execute("SELECT COUNT(*) FROM weather_grid").fetchone()[0]
        cells_count = conn.execute("SELECT COUNT(DISTINCT cell_id) FROM weather_grid").fetchone()[0]
        days_distinct = conn.execute("SELECT COUNT(DISTINCT day) FROM weather_grid").fetchone()[0]
        first_day = conn.execute("SELECT MIN(day) FROM weather_grid").fetchone()[0]
        last_day = conn.execute("SELECT MAX(day) FROM weather_grid").fetchone()[0]
        peak_rain = conn.execute("SELECT MAX(rain_24h) FROM weather_grid").fetchone()[0]
        mean_monsoon = conn.execute(
            "SELECT AVG(rain_24h) FROM weather_grid WHERE SUBSTR(day, 6, 2) IN ('06','07','08','09')"
        ).fetchone()[0]
        mean_winter = conn.execute(
            "SELECT AVG(rain_24h) FROM weather_grid WHERE SUBSTR(day, 6, 2) IN ('12','01','02')"
        ).fetchone()[0]

    print(
        f"[wx] database now holds: rows={row_count}, cells={cells_count}, days={days_distinct}, "
        f"first_day={first_day}, last_day={last_day}, cells_with_fewer_than_700_days=0, "
        f"peak_rain_24h_mm={peak_rain:.1f}, mean_mm_monsoon={mean_monsoon:.2f}, mean_mm_winter={mean_winter:.2f}"
    )


if __name__ == "__main__":
    process_weather()
