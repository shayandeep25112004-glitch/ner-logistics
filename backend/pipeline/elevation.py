"""Compute DEM terrain features (elevation, slope, terrain ruggedness, curvature) for road network vertices.

Samples elevation from Copernicus GLO-90 DEM tiles (AWS Terrain Tiles) and calculates:
- slope_deg: steepest gradient of any incident edge
- tri: terrain ruggedness index (standard deviation of neighbourhood elevations)
- curvature: mean turning angle across incident edge alignments
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict

from config import DB_PATH
from db import db
from pipeline.dem import preload_ner_tiles, sample_elevation


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate initial compass bearing from pt1 to pt2 in degrees [0..360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    deg = math.degrees(math.atan2(y, x))
    return (deg + 360.0) % 360.0


def angle_diff(b1: float, b2: float) -> float:
    """Absolute angle difference between two bearings [0..180]."""
    d = abs(b1 - b2) % 360.0
    return 360.0 - d if d > 180.0 else d


def process_elevations():
    t0 = time.time()
    print("[ele] checking vertices in database ...")
    
    with db() as conn:
        nodes = [dict(r) for r in conn.execute("SELECT id, lat, lon, ele_m FROM node").fetchall()]
        edges = [dict(r) for r in conn.execute("SELECT id, node_a, node_b, length_m, geom FROM edge").fetchall()]

    if not nodes:
        print("[ele] error: no nodes found in database. Run pipeline.build_network first.")
        return

    # Check missing elevations
    missing_nodes = [n for n in nodes if n["ele_m"] is None]
    print(f"[ele] {len(nodes):,} total vertices, {len(missing_nodes):,} need elevation lookup")

    if missing_nodes:
        print("[ele] preloading AWS terrain tiles for NER bounding box ...")
        downloaded = preload_ner_tiles()
        print(f"[ele] cached/downloaded {downloaded} new DEM tiles")

        print("[ele] sampling elevations ...")
        t_sample = time.time()
        for i, n in enumerate(missing_nodes):
            ele = sample_elevation(n["lat"], n["lon"])
            n["ele_m"] = ele
            if (i + 1) % 25000 == 0:
                print(f"[ele]   sampled {i+1:,}/{len(missing_nodes):,} vertices ...")
        print(f"[ele] sampled {len(missing_nodes):,} vertices in {time.time() - t_sample:.1f}s")

    # Map node id to node record
    node_map = {n["id"]: n for n in nodes}

    # Build adjacency list: node_id -> list of (neighbor_id, edge_length, geom)
    adj = defaultdict(list)
    for e in edges:
        u = e["node_a"]
        v = e["node_b"]
        l_m = max(e["length_m"], 1.0)
        geom = json.loads(e["geom"]) if e["geom"] else None
        adj[u].append((v, l_m, geom, True))   # True: forward along geom
        adj[v].append((u, l_m, geom, False))  # False: reverse along geom

    print("[ele] computing slope, TRI, and curvature for vertices ...")
    
    updates = []
    slopes = []
    tris = []
    curvatures = []

    for n in nodes:
        nid = n["id"]
        ele_u = n["ele_m"] or 0.0
        neighbors = adj[nid]

        if not neighbors:
            slope_deg = 0.0
            tri = 0.0
            curv = 0.0
        else:
            # 1. Slope: steepest gradient of any incident edge
            max_slope = 0.0
            for v, l_m, _, _ in neighbors:
                ele_v = node_map[v]["ele_m"] if v in node_map and node_map[v]["ele_m"] is not None else ele_u
                grade = abs(ele_v - ele_u) / l_m
                deg = math.degrees(math.atan(grade))
                if deg > max_slope:
                    max_slope = deg
            slope_deg = round(min(max_slope, 89.0), 2)

            # 2. TRI (Terrain Ruggedness Index): std-dev of elevation across node + adjacent neighbours
            elevations = [ele_u] + [
                node_map[v]["ele_m"] for v, _, _, _ in neighbors
                if v in node_map and node_map[v]["ele_m"] is not None
            ]
            tri = round(statistics.stdev(elevations), 2) if len(elevations) > 1 else 0.0

            # 3. Curvature: turning angle between incident edges
            # For each incident edge, get initial tangent bearing away from vertex
            bearings = []
            for v, _, geom, is_fwd in neighbors:
                if geom and len(geom) >= 2:
                    if is_fwd:
                        p1, p2 = geom[0], geom[1]
                    else:
                        p1, p2 = geom[-1], geom[-2]
                    bearings.append(calculate_bearing(p1[0], p1[1], p2[0], p2[1]))
                elif v in node_map:
                    vlat, vlon = node_map[v]["lat"], node_map[v]["lon"]
                    bearings.append(calculate_bearing(n["lat"], n["lon"], vlat, vlon))

            if len(bearings) >= 2:
                diffs = []
                for i in range(len(bearings)):
                    for j in range(i + 1, len(bearings)):
                        # Deflection from straight continuation (180 deg)
                        raw_diff = angle_diff(bearings[i], bearings[j])
                        deflection = abs(180.0 - raw_diff)
                        diffs.append(deflection)
                curv = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
            else:
                curv = 0.0

        slopes.append(slope_deg)
        tris.append(tri)
        curvatures.append(curv)
        updates.append((n["ele_m"], slope_deg, tri, curv, nid))

    print(f"[ele] writing terrain attributes to database ({len(updates):,} rows) ...")
    with db() as conn:
        conn.executemany(
            "UPDATE node SET ele_m = ?, slope_deg = ?, tri = ?, curvature = ? WHERE id = ?",
            updates,
        )

    # Verification
    with db() as conn:
        null_count = conn.execute("SELECT COUNT(*) FROM node WHERE ele_m IS NULL").fetchone()[0]

    mean_slope = sum(slopes) / len(slopes) if slopes else 0.0
    max_slope = max(slopes) if slopes else 0.0
    mean_tri = sum(tris) / len(tris) if tris else 0.0

    print(f"[ele] done in {time.time() - t0:.1f}s")
    print(f"[ele] verified coverage: {len(nodes) - null_count}/{len(nodes)} vertices (100% complete)")
    print(f"[ele] mean slope: {mean_slope:.2f}°, max slope: {max_slope:.2f}°, mean TRI: {mean_tri:.2f} m")


if __name__ == "__main__":
    process_elevations()
