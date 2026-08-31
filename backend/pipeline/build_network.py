"""Build junction-collapsed routable road graph from OpenStreetMap .osm.pbf extracts.

Parses extracts for the 8 NER states, collapses interior shape points at junctions,
removes border-overlap duplicates, and stores vertices & edges in SQLite.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
import requests

from config import (
    NER_STATES,
    OSM_EXTRACTS,
    OSM_EXTRACT_URL,
    RAW_DIR,
    ROAD_CLASSES,
)
from db import init_schema, db


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two WGS84 coordinate pairs."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def download_extracts() -> dict[str, Path]:
    """Download the 8 NER .osm.pbf extracts into data/raw/ if not already present."""
    paths = {}
    for st_code, fname in OSM_EXTRACTS.items():
        dst = RAW_DIR / fname
        paths[st_code] = dst
        if dst.exists() and dst.stat().st_size > 100000:
            continue
        url = OSM_EXTRACT_URL.format(fname=fname)
        print(f"[net] downloading {st_code} from {url} ...", flush=True)
        for attempt in range(3):
            try:
                r = requests.get(url, stream=True, timeout=60, headers={"User-Agent": "ner-logistics/1.0"})
                if r.status_code == 200:
                    with open(dst, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1048576):
                            if chunk:
                                f.write(chunk)
                    print(f"[net] downloaded {st_code} ({dst.stat().st_size / (1024*1024):.1f} MB)", flush=True)
                    break
            except Exception as e:
                print(f"[net] retry {st_code} ({e})", flush=True)
                time.sleep(2)
    return paths


class OsmGraphBuilder:
    def __init__(self):
        self.state_file_sizes: dict[str, int] = {}
        # Count node occurrences to determine junctions
        self.node_counts: dict[int, int] = defaultdict(int)
        self.way_endpoints: set[int] = set()
        self.ford_nodes: set[int] = set()
        
        # Parsed ways per state
        # state -> list of dict(way_id, highway, tags, node_ids, coords)
        self.state_ways: dict[str, list[dict]] = defaultdict(list)
        self.node_coords: dict[int, tuple[float, float]] = {}

    def parse_state(self, state_code: str, pbf_path: Path):
        import osmium
        
        if not pbf_path.exists():
            return
        
        self.state_file_sizes[state_code] = pbf_path.stat().st_size
        mb = pbf_path.stat().st_size / (1024 * 1024)
        print(f"[net] parsing {state_code} ({pbf_path.name}, {mb:.1f} MB)", flush=True)

        class NodeAndWayCollector(osmium.SimpleHandler):
            def __init__(self, builder, st):
                super().__init__()
                self.builder = builder
                self.st = st
                self.way_count = 0
                self.segment_count = 0

            def way(self, w):
                tags = dict(w.tags)
                hw = tags.get("highway")
                if hw not in ROAD_CLASSES:
                    return
                
                nodes = list(w.nodes)
                if len(nodes) < 2:
                    return
                
                valid_nodes = []
                coords = []
                for n in nodes:
                    if n.location.valid():
                        valid_nodes.append(n.ref)
                        coords.append((round(n.location.lat, 6), round(n.location.lon, 6)))
                        self.builder.node_coords[n.ref] = (round(n.location.lat, 6), round(n.location.lon, 6))
                        self.builder.node_counts[n.ref] += 1
                
                if len(valid_nodes) < 2:
                    return
                
                self.way_count += 1
                self.segment_count += len(valid_nodes) - 1
                self.builder.way_endpoints.add(valid_nodes[0])
                self.builder.way_endpoints.add(valid_nodes[-1])
                
                self.builder.state_ways[self.st].append({
                    "way_id": w.id,
                    "highway": hw,
                    "tags": tags,
                    "nodes": valid_nodes,
                    "coords": coords,
                })

        handler = NodeAndWayCollector(self, state_code)
        try:
            handler.apply_file(str(pbf_path), locations=True, idx="flex_mem")
        except Exception as e:
            # Fallback if flex_mem fails
            handler.apply_file(str(pbf_path), locations=True)
            
        print(f"[net]   {state_code}: {handler.way_count:,} ways, {handler.segment_count:,} shape-point segments", flush=True)

    def build_edges(self) -> tuple[dict[int, dict], list[dict]]:
        """Collapse internal shape points at junctions and deduplicate overlapping candidate edges."""
        # A node is a vertex if:
        # 1. it is an endpoint of any way, OR
        # 2. it is referenced by more than 1 way/segment, OR
        # 3. it appears multiple times in the same way (hairpin/roundabout).
        is_vertex = set(self.way_endpoints)
        for nid, count in self.node_counts.items():
            if count > 1:
                is_vertex.add(nid)

        candidate_edges_by_pair: dict[tuple[int, int], list[dict]] = defaultdict(list)
        nodes_dict: dict[int, dict] = {}
        
        # Sort states by file size ascending (smallest state extract first for correct attribution)
        sorted_states = sorted(self.state_ways.keys(), key=lambda s: self.state_file_sizes.get(s, 0))

        raw_segments_count = 0
        for st in sorted_states:
            for w in self.state_ways[st]:
                nodes = w["nodes"]
                coords = w["coords"]
                tags = w["tags"]
                hw = w["highway"]
                way_id = w["way_id"]
                
                is_bridge = 1 if tags.get("bridge") in ("yes", "viaduct", "cantilever") else 0
                is_tunnel = 1 if tags.get("tunnel") in ("yes", "culvert") else 0
                name = tags.get("name")
                ref = tags.get("ref")
                surface = tags.get("surface")
                
                # Check way-level or node-level fords
                is_ford = 1 if tags.get("ford") in ("yes", "stepping_stones") else 0
                
                sub_nodes = [nodes[0]]
                sub_coords = [coords[0]]
                sub_length = 0.0
                sub_ford = is_ford or (nodes[0] in self.ford_nodes)
                
                for i in range(1, len(nodes)):
                    raw_segments_count += 1
                    nid = nodes[i]
                    c = coords[i]
                    prev_c = coords[i-1]
                    
                    seg_len = haversine_m(prev_c[0], prev_c[1], c[0], c[1])
                    sub_length += seg_len
                    sub_nodes.append(nid)
                    sub_coords.append(c)
                    if nid in self.ford_nodes:
                        sub_ford = 1
                    
                    if nid in is_vertex or i == len(nodes) - 1:
                        u = sub_nodes[0]
                        v = sub_nodes[-1]
                        
                        if u != v: # Drop self-loops
                            speed = ROAD_CLASSES[hw]["speed_kmph"]
                            base_min = (sub_length / 1000.0) / speed * 60.0
                            base_rel = ROAD_CLASSES[hw]["base_reliability"]
                            
                            edge_rec = {
                                "id": f"w{way_id}#{u}-{v}",
                                "way_id": way_id,
                                "node_a": u,
                                "node_b": v,
                                "highway": hw,
                                "ref": ref,
                                "name": name,
                                "surface": surface,
                                "is_bridge": is_bridge,
                                "is_tunnel": is_tunnel,
                                "is_ford": sub_ford,
                                "length_m": round(sub_length, 1),
                                "speed_kmph": speed,
                                "base_minutes": round(base_min, 2),
                                "base_reliability": base_rel,
                                "state": st,
                                "geom": json.dumps(sub_coords),
                            }
                            
                            pair_key = (min(u, v), max(u, v))
                            candidate_edges_by_pair[pair_key].append(edge_rec)
                            
                            # Record vertex nodes
                            for vid, (vlat, vlon) in [(u, sub_coords[0]), (v, sub_coords[-1])]:
                                if vid not in nodes_dict:
                                    nodes_dict[vid] = {
                                        "id": vid,
                                        "lat": vlat,
                                        "lon": vlon,
                                        "state": st,
                                    }
                        
                        # Reset for next subsegment
                        sub_nodes = [nid]
                        sub_coords = [c]
                        sub_length = 0.0
                        sub_ford = is_ford or (nid in self.ford_nodes)

        # De-duplicate candidate edges covering the same physical junction pair
        final_edges: list[dict] = []
        for pair_key, candidates in candidate_edges_by_pair.items():
            if len(candidates) == 1:
                final_edges.append(candidates[0])
            else:
                # Prefer the candidate with non-null ref, name, bridge, or shortest name/cleanest
                best = max(candidates, key=lambda e: (
                    1 if e["ref"] else 0,
                    1 if e["name"] else 0,
                    e["is_bridge"],
                    e["is_ford"],
                    -len(e["state"]), # Prefer earlier/smaller state
                ))
                final_edges.append(best)

        return nodes_dict, final_edges


def build_network_main():
    t0 = time.time()
    init_schema()
    
    extract_paths = download_extracts()
    builder = OsmGraphBuilder()
    
    for st_code in ["AR", "AS", "ML", "MN", "MZ", "NL", "SK", "TR"]:
        p = extract_paths.get(st_code)
        if p and p.exists():
            builder.parse_state(st_code, p)
            
    print(f"[net] {sum(len(w['nodes'])-1 for st in builder.state_ways.values() for w in st):,} raw segments -> resolving vertices ...", flush=True)
    nodes_dict, edges = builder.build_edges()
    
    print(f"[net] {len(edges):,} edges, {len(nodes_dict):,} nodes in {time.time() - t0:.1f}s", flush=True)
    print(f"[net] writing to SQLite ...", flush=True)
    
    # Insert nodes and edges into SQLite
    with db() as conn:
        conn.execute("DELETE FROM edge")
        conn.execute("DELETE FROM node")
        
        # Batch insert nodes
        node_rows = [(n["id"], n["lat"], n["lon"], n["state"]) for n in nodes_dict.values()]
        conn.executemany("INSERT OR REPLACE INTO node (id, lat, lon, state) VALUES (?, ?, ?, ?)", node_rows)
        
        # Batch insert edges
        edge_rows = [
            (
                e["id"], e["way_id"], e["node_a"], e["node_b"], e["highway"], e["ref"], e["name"],
                e["surface"], e["is_bridge"], e["is_tunnel"], e["is_ford"], e["length_m"],
                e["speed_kmph"], e["base_minutes"], e["base_reliability"], e["state"], e["geom"]
            )
            for e in edges
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO edge
               (id, way_id, node_a, node_b, highway, ref, name, surface, is_bridge, is_tunnel,
                is_ford, length_m, speed_kmph, base_minutes, base_reliability, state, geom)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            edge_rows,
        )

    # Compute summary stats
    tot_km = sum(e["length_m"] for e in edges) / 1000.0
    bridges = sum(e["is_bridge"] for e in edges)
    fords = sum(e["is_ford"] for e in edges)
    
    state_counts = defaultdict(int)
    for e in edges:
        state_counts[e["state"]] += 1

    summary = {
        "edges": len(edges),
        "nodes": len(nodes_dict),
        "network_km": round(tot_km, 1),
        "bridges": bridges,
        "fords": fords,
        "null_geom": 0,
        "edges_by_state": dict(state_counts),
    }
    print("[net]", json.dumps(summary, indent=2), flush=True)
    for st, c in sorted(state_counts.items(), key=lambda x: -x[1]):
        print(f"[net]   {st}: {c:,} edges", flush=True)
    print(f"[net] done in {time.time() - t0:.1f}s: {len(edges):,} edges / {len(nodes_dict):,} nodes", flush=True)


if __name__ == "__main__":
    build_network_main()
