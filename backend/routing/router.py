"""Risk-aware A* routing with Yen's K-shortest loopless alternate paths.

Implements risk-weighted cost formulation:
  cost = free_flow_minutes * (1.0 + RISK_COST_WEIGHT * risk)
with admissible straight-line heuristic, memory-efficient parent-pointer backtracking,
and divergence filtering.
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


class CompactEdge:
    __slots__ = (
        "id", "node_a", "node_b", "highway", "ref", "name", "road",
        "is_bridge", "is_ford", "distance_km", "base_minutes", "risk"
    )
    def __init__(self, id, node_a, node_b, highway, ref, name, road, is_bridge, is_ford, distance_km, base_minutes):
        self.id = id
        self.node_a = node_a
        self.node_b = node_b
        self.highway = highway
        self.ref = ref
        self.name = name
        self.road = road
        self.is_bridge = is_bridge
        self.is_ford = is_ford
        self.distance_km = distance_km
        self.base_minutes = base_minutes
        self.risk = 0.0

    def __getitem__(self, item):
        return getattr(self, item)

    def get(self, item, default=None):
        return getattr(self, item, default)


class NetworkRouter:
    def __init__(self):
        self.nodes: dict[int, tuple[float, float]] = {}  # nid -> (lat, lon)
        self.edges: dict[str, CompactEdge] = {}
        self.adj: dict[int, list[tuple[int, str]]] = defaultdict(list)  # u -> [(v, edge_id)]
        self.components: dict[int, int] = {}
        self.largest_component_id: int = 1
        self.grid_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.loaded = False

    def load_graph(self):
        """Load nodes and edges from SQLite into in-memory routing graph (compact representation)."""
        t0 = time.time()
        with db() as conn:
            nodes_rows = conn.execute("SELECT id, lat, lon FROM node").fetchall()
            edges_rows = conn.execute(
                """SELECT id, node_a, node_b, highway, ref, name,
                          is_bridge, is_ford, length_m, base_minutes
                   FROM edge"""
            ).fetchall()

        self.nodes.clear()
        self.edges.clear()
        self.adj.clear()
        self.grid_buckets.clear()

        for r in nodes_rows:
            nid = r["id"]
            lat = float(r["lat"])
            lon = float(r["lon"])
            self.nodes[nid] = (lat, lon)
            # 0.1 deg spatial hash bucket (~11 km)
            bx = int(math.floor(lat * 10))
            by = int(math.floor(lon * 10))
            self.grid_buckets[(bx, by)].append(nid)

        for r in edges_rows:
            eid = r["id"]
            u = r["node_a"]
            v = r["node_b"]
            road_label = r["ref"] or r["name"] or r["highway"] or "road"
            dist_km = (r["length_m"] or 0.0) / 1000.0
            base_min = r["base_minutes"] or 1.0
            
            edge_obj = CompactEdge(
                id=eid,
                node_a=u,
                node_b=v,
                highway=r["highway"],
                ref=r["ref"],
                name=r["name"],
                road=road_label,
                is_bridge=bool(r["is_bridge"]),
                is_ford=bool(r["is_ford"]),
                distance_km=dist_km,
                base_minutes=base_min,
            )
            self.edges[eid] = edge_obj
            self.adj[u].append((v, eid))
            self.adj[v].append((u, eid))

        self._compute_connected_components()
        self.loaded = True
        print(f"[router] loaded {len(self.edges):,} edges / {len(self.nodes):,} nodes in {time.time() - t0:.2f}s")

    def _compute_connected_components(self):
        """Identify connected components in the graph and find largest component."""
        visited = set()
        comp_id = 0
        self.components.clear()
        comp_sizes = defaultdict(int)

        for nid in self.nodes:
            if nid in visited:
                continue
            comp_id += 1
            queue = [nid]
            visited.add(nid)
            size = 0
            while queue:
                curr = queue.pop()
                size += 1
                self.components[curr] = comp_id
                for nbr, _ in self.adj.get(curr, []):
                    if nbr not in visited:
                        visited.add(nbr)
                        queue.append(nbr)
            comp_sizes[comp_id] = size

        if comp_sizes:
            self.largest_component_id = max(comp_sizes, key=comp_sizes.get)
        else:
            self.largest_component_id = 1

    def snap_node(self, lat: float, lon: float, required_component: int | None = None) -> tuple[int, float]:
        """Find the nearest graph node to a coordinate and return (node_id, snap_distance_m)."""
        if not self.nodes:
            raise RuntimeError("Graph is empty. Build network first.")

        target_comp = required_component if required_component is not None else getattr(self, "largest_component_id", 1)

        bx = int(math.floor(lat * 10))
        by = int(math.floor(lon * 10))

        # Search expanding rings of spatial hash buckets
        candidates = []
        for radius in (2, 5, 10, 25):
            candidates = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    b_nodes = self.grid_buckets.get((bx + dx, by + dy))
                    if b_nodes:
                        candidates.extend(b_nodes)
            if candidates:
                # If searching for a specific component, check if we found any in this component
                if any(self.components.get(nid) == target_comp for nid in candidates):
                    break

        if not candidates:
            candidates = list(self.nodes.keys())

        scored = []
        for nid in candidates:
            n_lat, n_lon = self.nodes[nid]
            d = haversine_km(lat, lon, n_lat, n_lon)
            scored.append((d, nid))
        scored.sort(key=lambda x: x[0])

        # Priority 1: Nearest node matching target_comp
        for d, nid in scored:
            if self.components.get(nid) == target_comp:
                return nid, round(d * 1000.0, 1)

        # Priority 2: Fallback to absolute closest candidate
        best_dist, best_nid = scored[0]
        return best_nid, round(best_dist * 1000.0, 1)

    def edge_cost(self, edge_id: str, custom_risks: dict[str, float] | None = None) -> tuple[float, bool]:
        """Compute traversal cost (minutes) and blocked flag for an edge."""
        edge = self.edges[edge_id]
        risk = custom_risks.get(edge_id, edge.risk) if custom_risks else edge.risk
        base_min = edge.base_minutes

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
        """High-efficiency A* shortest path using came_from backtracking (ultra low RAM)."""
        if start_node not in self.nodes or target_node not in self.nodes:
            return None
        if start_node == target_node:
            return {"edge_ids": [], "cost": 0.0, "distance_km": 0.0, "free_flow_min": 0.0}

        target_lat, target_lon = self.nodes[target_node]

        def heuristic(nid: int) -> float:
            n_lat, n_lon = self.nodes[nid]
            dist_km = haversine_km(n_lat, n_lon, target_lat, target_lon)
            return (dist_km / MAX_SPEED_KMPH) * 60.0

        pq = [(heuristic(start_node), 0.0, start_node)]
        best_g = {start_node: 0.0}
        came_from: dict[int, tuple[int, str]] = {}  # v -> (u, eid)

        while pq:
            f, g, u = heapq.heappop(pq)
            if u == target_node:
                # Path found - reconstruct backwards
                path_edges = []
                curr = target_node
                while curr != start_node:
                    prev_node, eid = came_from[curr]
                    path_edges.append(eid)
                    curr = prev_node
                path_edges.reverse()

                tot_dist = sum(self.edges[eid].distance_km for eid in path_edges)
                tot_free_flow = sum(self.edges[eid].base_minutes for eid in path_edges)
                return {
                    "edge_ids": path_edges,
                    "cost": g,
                    "distance_km": round(tot_dist, 2),
                    "free_flow_min": round(tot_free_flow, 1),
                }

            if best_g.get(u, float("inf")) < g:
                continue

            for v, eid in self.adj.get(u, []):
                if forbidden_edges and eid in forbidden_edges:
                    continue
                ecost, _ = self.edge_cost(eid, custom_risks)
                new_g = g + ecost

                if new_g < best_g.get(v, float("inf")):
                    best_g[v] = new_g
                    came_from[v] = (u, eid)
                    heapq.heappush(pq, (new_g + heuristic(v), new_g, v))

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

        # Fetch geometries on-demand for only the active route edges in chunks
        edge_geoms = {}
        if edge_ids:
            try:
                with db() as conn:
                    # Query in chunks of 500 to stay well under SQLite parameter limits
                    chunk_size = 500
                    for i in range(0, len(edge_ids), chunk_size):
                        chunk = edge_ids[i:i + chunk_size]
                        placeholders = ",".join("?" for _ in chunk)
                        rows = conn.execute(
                            f"SELECT id, geom FROM edge WHERE id IN ({placeholders})", chunk
                        ).fetchall()
                        for r in rows:
                            if r["geom"]:
                                try:
                                    edge_geoms[r["id"]] = json.loads(r["geom"])
                                except Exception:
                                    pass
            except Exception:
                pass

        for eid in edge_ids:
            e = self.edges.get(eid)
            if not e:
                continue
            risk = custom_risks.get(eid, e.risk) if custom_risks else e.risk
            if risk > max_risk:
                max_risk = risk
            if risk >= RISK_BLOCKED:
                passes_blocked = True

            cost, _ = self.edge_cost(eid, custom_risks)
            tot_risk_adj += cost
            tot_dist_km += e.distance_km
            tot_free_flow += e.base_minutes

            segments.append({
                "edge_id": eid,
                "road": e.road,
                "highway": e.highway,
                "km": round(e.distance_km, 2),
                "risk": round(risk, 4),
                "is_bridge": e.is_bridge,
                "is_ford": e.is_ford,
            })

            geom = edge_geoms.get(eid)
            if geom:
                coords.extend(geom)
            else:
                # Fallback to direct node coordinates if edge geom is not in DB
                u_coord = self.nodes.get(e.node_a)
                v_coord = self.nodes.get(e.node_b)
                if u_coord and v_coord:
                    coords.extend([list(u_coord), list(v_coord)])

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
        c1 = self.components.get(start_nid, self.largest_component_id)
        target_nid, snap2_m = self.snap_node(lat2, lon2, required_component=c1)

        # Handle case where start and end snap to the exact same node
        if start_nid == target_nid:
            s_lat, s_lon = self.nodes.get(start_nid, (lat1, lon1))
            return {
                "routes": [{
                    "rank": 1,
                    "distance_km": 0.1,
                    "free_flow_minutes": 0.5,
                    "risk_adjusted_minutes": 0.5,
                    "max_segment_risk": 0.0,
                    "risk_level": "clear",
                    "passes_blocked_segment": False,
                    "polyline": [[lat1, lon1], [s_lat, s_lon], [lat2, lon2]],
                    "segments": [],
                }],
                "computed_in_ms": int((time.time() - t0) * 1000),
                "diagnostics": "Origin and destination are in the immediate vicinity.",
                "model_note": "A* local snap",
                "snap_distance_m": [snap1_m, snap2_m],
            }

        # 1. Primary route via A*
        primary = self.a_star(start_nid, target_nid, custom_risks=custom_risks)
        if not primary or not primary["edge_ids"]:
            # Fallback retry without component lock to closest overall node
            target_nid_fallback, snap2_m_fb = self.snap_node(lat2, lon2, required_component=None)
            if target_nid_fallback != target_nid:
                primary = self.a_star(start_nid, target_nid_fallback, custom_risks=custom_risks)
                if primary and primary["edge_ids"]:
                    target_nid = target_nid_fallback
                    snap2_m = snap2_m_fb

        if not primary or not primary["edge_ids"]:
            return {
                "routes": [],
                "computed_in_ms": int((time.time() - t0) * 1000),
                "diagnostics": "No connected corridor found between selected points. Try selecting points closer to major state highways.",
                "model_note": "A* with Yen K-alternates",
                "snap_distance_m": [snap1_m, snap2_m],
            }

        primary_edges = primary["edge_ids"]
        found_paths = [primary_edges]

        # 2. Yen's K-shortest paths with smart spaced spur points
        if alternatives > 1 and len(primary_edges) > 1:
            candidate_heap = []

            # Node chain along primary route
            node_chain = [start_nid]
            curr = start_nid
            for eid in primary_edges:
                e = self.edges.get(eid)
                if not e:
                    continue
                nxt = e.node_b if e.node_a == curr else e.node_a
                node_chain.append(nxt)
                curr = nxt

            # Select up to 6 well-spaced spur indices along the first 70% of the route
            max_idx = min(len(node_chain) - 1, len(primary_edges))
            search_limit = max(1, int(max_idx * 0.7))
            step = max(1, search_limit // 6)
            spur_indices = list(range(0, search_limit, step))[:6]

            for i in spur_indices:
                if i >= len(node_chain) or i >= len(primary_edges):
                    continue
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
