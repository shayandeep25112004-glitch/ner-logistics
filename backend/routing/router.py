"""Risk-aware A* routing with Yen's K-shortest loopless alternate paths.

Implements risk-weighted cost formulation:
  cost = free_flow_minutes * (1.0 + RISK_COST_WEIGHT * risk)
with admissible straight-line heuristic and divergence filtering.
"""

from __future__ import annotations

import heapq
import json
import math
import time
from collections import defaultdict
from typing import Sequence

from config import (
    ALTERNATE_ROUTES_K,
    RISK_AT_RISK,
    RISK_BLOCKED,
    RISK_COST_WEIGHT,
    ROAD_CLASSES,
)
from db import db

MAX_SPEED_KMPH = 80.0
BLOCKED_PENALTY_MINUTES = 2000.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


class NetworkRouter:
    def __init__(self):
        self.nodes: dict[int, dict] = {}
        self.edges: dict[str, dict] = {}
        self.adj: dict[int, list[dict]] = defaultdict(list)
        self.components: dict[int, int] = {}
        self.grid_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.loaded = False

    def load_graph(self):
        """Load nodes and edges from SQLite into in-memory routing graph."""
        t0 = time.time()
        with db() as conn:
            nodes_rows = conn.execute("SELECT id, lat, lon, state FROM node").fetchall()
            edges_rows = conn.execute(
                """SELECT id, way_id, node_a, node_b, highway, ref, name, surface,
                          is_bridge, is_tunnel, is_ford, length_m, speed_kmph,
                          base_minutes, base_reliability, state, geom
                   FROM edge"""
            ).fetchall()

        self.nodes.clear()
        self.edges.clear()
        self.adj.clear()
        self.grid_buckets.clear()

        for r in nodes_rows:
            nid = r["id"]
            lat = r["lat"]
            lon = r["lon"]
            self.nodes[nid] = {
                "id": nid,
                "lat": lat,
                "lon": lon,
                "state": r["state"],
            }
            # 0.1 deg spatial hash bucket (~11 km)
            bx = int(math.floor(lat * 10))
            by = int(math.floor(lon * 10))
            self.grid_buckets[(bx, by)].append(nid)

        for r in edges_rows:
            eid = r["id"]
            u = r["node_a"]
            v = r["node_b"]
            geom = json.loads(r["geom"]) if r["geom"] else None
            
            road_label = r["ref"] or r["name"] or r["highway"] or "road"
            edge_data = {
                "id": eid,
                "way_id": r["way_id"],
                "node_a": u,
                "node_b": v,
                "highway": r["highway"],
                "ref": r["ref"],
                "name": r["name"],
                "road": road_label,
                "surface": r["surface"],
                "is_bridge": bool(r["is_bridge"]),
                "is_tunnel": bool(r["is_tunnel"]),
                "is_ford": bool(r["is_ford"]),
                "length_m": r["length_m"],
                "distance_km": r["length_m"] / 1000.0,
                "speed_kmph": r["speed_kmph"],
                "base_minutes": r["base_minutes"],
                "base_reliability": r["base_reliability"] or 0.9,
                "state": r["state"],
                "geom": geom,
                "risk": 0.0, # Will be set by live risk scoring
            }
            self.edges[eid] = edge_data
            self.adj[u].append({"neighbor": v, "edge_id": eid})
            self.adj[v].append({"neighbor": u, "edge_id": eid})

        self._compute_connected_components()
        self.loaded = True
        print(f"[router] loaded {len(self.edges):,} edges / {len(self.nodes):,} nodes in {time.time() - t0:.2f}s")

    def _compute_connected_components(self):
        """Identify connected components in the graph."""
        visited = set()
        comp_id = 0
        self.components.clear()
        for nid in self.nodes:
            if nid in visited:
                continue
            comp_id += 1
            queue = [nid]
            visited.add(nid)
            while queue:
                curr = queue.pop()
                self.components[curr] = comp_id
                for link in self.adj.get(curr, []):
                    nbr = link["neighbor"]
                    if nbr not in visited:
                        visited.add(nbr)
                        queue.append(nbr)

    def snap_node(self, lat: float, lon: float) -> tuple[int, float]:
        """Find the nearest graph node to a coordinate and return (node_id, snap_distance_m)."""
        if not self.nodes:
            raise RuntimeError("Graph is empty. Build network first.")

        bx = int(math.floor(lat * 10))
        by = int(math.floor(lon * 10))

        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                candidates.extend(self.grid_buckets.get((bx + dx, by + dy), []))

        if not candidates:
            candidates = list(self.nodes.keys())

        best_nid = candidates[0]
        best_dist = float("inf")
        for nid in candidates:
            n = self.nodes[nid]
            d = haversine_km(lat, lon, n["lat"], n["lon"])
            if d < best_dist:
                best_dist = d
                best_nid = nid

        return best_nid, round(best_dist * 1000.0, 1)

    def edge_cost(self, edge_id: str, custom_risks: dict[str, float] | None = None) -> tuple[float, bool]:
        """Compute traversal cost (minutes) and blocked flag for an edge."""
        edge = self.edges[edge_id]
        risk = custom_risks.get(edge_id, edge.get("risk", 0.0)) if custom_risks else edge.get("risk", 0.0)
        base_min = edge["base_minutes"]

        is_blocked = risk >= RISK_BLOCKED
        cost = base_min * (1.0 + RISK_COST_WEIGHT * risk)
        if is_blocked:
            cost += BLOCKED_PENALTY_MINUTES
        return cost, is_blocked

    def a_star(
        self,
        start_node: int,
        target_node: int,
        forbidden_edges: set[str] | None = None,
        custom_risks: dict[str, float] | None = None,
    ) -> dict | None:
        """Run A* shortest path from start_node to target_node."""
        if start_node not in self.nodes or target_node not in self.nodes:
            return None
        if start_node == target_node:
            return {"edge_ids": [], "cost": 0.0, "distance_km": 0.0, "free_flow_min": 0.0}

        target_lat = self.nodes[target_node]["lat"]
        target_lon = self.nodes[target_node]["lon"]

        def heuristic(nid: int) -> float:
            n = self.nodes[nid]
            dist_km = haversine_km(n["lat"], n["lon"], target_lat, target_lon)
            return (dist_km / MAX_SPEED_KMPH) * 60.0

        pq = [(heuristic(start_node), 0.0, start_node, [])]
        best_g = {start_node: 0.0}
        visited = set()

        while pq:
            f, g, u, path_edges = heapq.heappop(pq)
            if u == target_node:
                # Path found
                tot_dist = sum(self.edges[eid]["distance_km"] for eid in path_edges)
                tot_free_flow = sum(self.edges[eid]["base_minutes"] for eid in path_edges)
                return {
                    "edge_ids": path_edges,
                    "cost": g,
                    "distance_km": round(tot_dist, 2),
                    "free_flow_min": round(tot_free_flow, 1),
                }

            if u in visited and best_g.get(u, float("inf")) < g:
                continue
            visited.add(u)

            for link in self.adj.get(u, []):
                eid = link["edge_id"]
                if forbidden_edges and eid in forbidden_edges:
                    continue
                v = link["neighbor"]
                ecost, _ = self.edge_cost(eid, custom_risks)
                new_g = g + ecost

                if new_g < best_g.get(v, float("inf")):
                    best_g[v] = new_g
                    heapq.heappush(pq, (new_g + heuristic(v), new_g, v, path_edges + [eid]))

        return None

    def route_overlap(self, path_a: list[str], path_b: list[str]) -> float:
        """Compute Jaccard / shared edge fraction between two paths [0..1]."""
        if not path_a or not path_b:
            return 0.0
        set_a = set(path_a)
        set_b = set(path_b)
        shared = len(set_a.intersection(set_b))
        return shared / min(len(set_a), len(set_b))

    def format_route(
        self,
        edge_ids: list[str],
        rank: int,
        custom_risks: dict[str, float] | None = None,
    ) -> dict:
        """Format route dictionary with polyline geometry, segments, and risk indicators."""
        coords = []
        segments = []
        tot_dist_km = 0.0
        tot_free_flow = 0.0
        tot_risk_adj = 0.0
        max_risk = 0.0
        passes_blocked = False

        for eid in edge_ids:
            e = self.edges[eid]
            risk = custom_risks.get(eid, e.get("risk", 0.0)) if custom_risks else e.get("risk", 0.0)
            if risk > max_risk:
                max_risk = risk
            if risk >= RISK_BLOCKED:
                passes_blocked = True

            cost, _ = self.edge_cost(eid, custom_risks)
            tot_risk_adj += cost
            tot_dist_km += e["distance_km"]
            tot_free_flow += e["base_minutes"]

            segments.append({
                "edge_id": eid,
                "road": e["road"],
                "highway": e["highway"],
                "km": round(e["distance_km"], 2),
                "risk": round(risk, 4),
                "is_bridge": e["is_bridge"],
                "is_ford": e["is_ford"],
            })

            geom = e["geom"]
            if geom:
                coords.extend(geom)

        # Simplify duplicate consecutive coordinates in polyline
        clean_coords = []
        for c in coords:
            if not clean_coords or clean_coords[-1] != c:
                clean_coords.append(c)

        risk_level = "blocked" if passes_blocked else "at_risk" if max_risk >= RISK_AT_RISK else "clear"

        return {
            "rank": rank,
            "distance_km": round(tot_dist_km, 2),
            "free_flow_minutes": round(tot_free_flow, 1),
            "risk_adjusted_minutes": round(tot_risk_adj, 1),
            "max_segment_risk": round(max_risk, 4),
            "risk_level": risk_level,
            "passes_blocked_segment": passes_blocked,
            "polyline": clean_coords,
            "segments": segments,
        }

    def plan_route(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
        alternatives: int = ALTERNATE_ROUTES_K,
        custom_risks: dict[str, float] | None = None,
    ) -> dict:
        """Find optimal route and Yen's K-divergent alternates between origin and destination."""
        t0 = time.time()
        if not self.loaded:
            self.load_graph()

        start_nid, snap1_m = self.snap_node(lat1, lon1)
        target_nid, snap2_m = self.snap_node(lat2, lon2)

        # Connected component check
        c1 = self.components.get(start_nid)
        c2 = self.components.get(target_nid)
        if c1 is not None and c2 is not None and c1 != c2:
            diag = (
                "origin and destination are in different connected components of the road graph — "
                "no route exists in the current dataset (state extracts are clipped at borders, so corridors leaving the NER are severed)"
            )
            return {
                "routes": [],
                "computed_in_ms": int((time.time() - t0) * 1000),
                "diagnostics": diag,
                "model_note": "A* with Yen K-alternates",
                "snap_distance_m": [snap1_m, snap2_m],
            }

        # 1. Primary route via A*
        primary = self.a_star(start_nid, target_nid, custom_risks=custom_risks)
        if not primary or not primary["edge_ids"]:
            return {
                "routes": [],
                "computed_in_ms": int((time.time() - t0) * 1000),
                "diagnostics": "No route could be found between specified coordinates.",
                "model_note": "A* with Yen K-alternates",
                "snap_distance_m": [snap1_m, snap2_m],
            }

        primary_edges = primary["edge_ids"]
        found_paths = [primary_edges]

        # 2. Yen's K-shortest paths with capped spur points (24)
        if alternatives > 1 and len(primary_edges) > 1:
            candidate_heap = []
            spur_cap = min(24, len(primary_edges) - 1)

            # Node chain along primary route
            node_chain = [start_nid]
            curr = start_nid
            for eid in primary_edges:
                e = self.edges[eid]
                nxt = e["node_b"] if e["node_a"] == curr else e["node_a"]
                node_chain.append(nxt)
                curr = nxt

            for i in range(spur_cap):
                spur_node = node_chain[i]
                root_path = primary_edges[:i]

                forbidden = set()
                for p in found_paths:
                    if len(p) > i and p[:i] == root_path:
                        forbidden.add(p[i])

                spur_res = self.a_star(spur_node, target_nid, forbidden_edges=forbidden, custom_risks=custom_risks)
                if spur_res and spur_res["edge_ids"]:
                    total_candidate = root_path + spur_res["edge_ids"]
                    c_cost = spur_res["cost"] + sum(self.edge_cost(eid, custom_risks)[0] for eid in root_path)
                    heapq.heappush(candidate_heap, (c_cost, total_candidate))

            # Select candidate paths that diverge by >= 15% (overlap <= 85%)
            while candidate_heap and len(found_paths) < alternatives:
                _, cand_path = heapq.heappop(candidate_heap)
                is_divergent = True
                for p in found_paths:
                    if self.route_overlap(cand_path, p) > 0.85:
                        is_divergent = False
                        break
                if is_divergent:
                    found_paths.append(cand_path)

        formatted_routes = [
            self.format_route(p, idx + 1, custom_risks)
            for idx, p in enumerate(found_paths)
        ]

        diag = ""
        if len(formatted_routes) == 1 and alternatives > 1:
            diag = (
                "only one route returned: no alternative diverges from the primary by the 15% minimum, "
                "which is the expected answer for a single-corridor district"
            )

        return {
            "routes": formatted_routes,
            "computed_in_ms": int((time.time() - t0) * 1000),
            "diagnostics": diag,
            "model_note": "A* with Yen K-alternates",
            "snap_distance_m": [snap1_m, snap2_m],
        }


# Global router singleton instance
_GLOBAL_ROUTER: NetworkRouter | None = None


def get_router() -> NetworkRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _GLOBAL_ROUTER = NetworkRouter()
        _GLOBAL_ROUTER.load_graph()
    return _GLOBAL_ROUTER
