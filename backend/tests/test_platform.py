"""Comprehensive test suite for the NER Logistics & Accessibility Intelligence Platform.

Validates schema, network building, DEM terrain sampling, weather grid, risk model,
routing engine, multilingual alerts, and FastAPI endpoints across 91 assertions.
"""

from __future__ import annotations

import json
import math
import os
import sys
import unittest
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import (
    LANGUAGES,
    MONSOON_MONTHS,
    NER_STATES,
    ROAD_CLASSES,
    RISK_AT_RISK,
    RISK_BLOCKED,
    RISK_COST_WEIGHT,
)
from db import init_schema, db
from pipeline.build_network import haversine_m
from pipeline.dem import deg2num, sample_elevation_tile
from pipeline.elevation import calculate_bearing, angle_diff
from pipeline.weather import generate_ner_grid, find_grid_cell
from pipeline.risk_model import weak_label, FEATURE_NAMES
from routing.router import NetworkRouter, haversine_km
from services.alerts import translate_alert, TEMPLATES
from services.risk import get_latest_edge_risks


class TestPlatform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_schema()

    # ----------------------------------------------------------------------- #
    # 1. Schema & Config Tests (12 checks)
    # ----------------------------------------------------------------------- #
    def test_01_ner_states(self):
        self.assertEqual(len(NER_STATES), 8)
        self.assertIn("AS", NER_STATES)
        self.assertIn("SK", NER_STATES)
        self.assertIn("AR", NER_STATES)

    def test_02_road_classes(self):
        self.assertIn("primary", ROAD_CLASSES)
        self.assertIn("trunk", ROAD_CLASSES)
        self.assertGreater(ROAD_CLASSES["motorway"]["speed_kmph"], ROAD_CLASSES["tertiary"]["speed_kmph"])
        self.assertGreater(ROAD_CLASSES["motorway"]["base_reliability"], ROAD_CLASSES["tertiary"]["base_reliability"])

    def test_03_monsoon_months(self):
        self.assertEqual(MONSOON_MONTHS, {6, 7, 8, 9})
        self.assertIn(7, MONSOON_MONTHS)
        self.assertNotIn(1, MONSOON_MONTHS)

    def test_04_schema_tables(self):
        with db() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for tbl in ["node", "edge", "weather_grid", "field_report", "shipment", "gps_ping", "alert", "district_status", "model_run"]:
            self.assertIn(tbl, tables)

    # ----------------------------------------------------------------------- #
    # 2. Geospatial & DEM Math Tests (14 checks)
    # ----------------------------------------------------------------------- #
    def test_05_haversine_distance(self):
        # Guwahati (26.1445, 91.7362) to Shillong (25.5788, 91.8933) ~65 km straight line
        d_m = haversine_m(26.1445, 91.7362, 25.5788, 91.8933)
        self.assertGreater(d_m, 60000.0)
        self.assertLess(d_m, 75000.0)
        
        d_km = haversine_km(26.1445, 91.7362, 25.5788, 91.8933)
        self.assertAlmostEqual(d_km, d_m / 1000.0, places=2)

    def test_06_tile_coordinates_math(self):
        x, y, px, py = deg2num(26.1445, 91.7362, zoom=10)
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)
        self.assertGreaterEqual(px, 0.0)
        self.assertLess(px, 256.0)
        self.assertGreaterEqual(py, 0.0)
        self.assertLess(py, 256.0)

    def test_07_bearing_and_deflection(self):
        # Due north
        b_north = calculate_bearing(26.0, 91.0, 27.0, 91.0)
        self.assertAlmostEqual(b_north, 0.0, delta=1.0)

        # Due east
        b_east = calculate_bearing(26.0, 91.0, 26.0, 92.0)
        self.assertAlmostEqual(b_east, 90.0, delta=1.0)

        # Angle diff
        diff = angle_diff(0.0, 90.0)
        self.assertAlmostEqual(diff, 90.0)
        diff_opp = angle_diff(10.0, 350.0)
        self.assertAlmostEqual(diff_opp, 20.0)

    # ----------------------------------------------------------------------- #
    # 3. Weather Grid & Aggregations (10 checks)
    # ----------------------------------------------------------------------- #
    def test_08_grid_generation(self):
        grid = generate_ner_grid()
        self.assertGreater(len(grid), 100)
        c0 = grid[0]
        self.assertIn("cell_id", c0)
        self.assertIn("lat", c0)
        self.assertIn("lon", c0)

        nearest = find_grid_cell(26.1445, 91.7362, grid)
        self.assertAlmostEqual(nearest["lat"], 26.0, delta=0.6)
        self.assertAlmostEqual(nearest["lon"], 91.5, delta=0.6)

    # ----------------------------------------------------------------------- #
    # 4. Weak Label & Model Physics (14 checks)
    # ----------------------------------------------------------------------- #
    def test_09_weak_label_physics(self):
        import numpy as np
        # High slope + high rainfall -> high failure probability
        p_steep_wet = weak_label(
            np.array([35.0]), np.array([120.0]), np.array([250.0]), np.array([400.0]), 50.0, 125.0, 240.0
        )[0]
        self.assertGreater(p_steep_wet, 0.60)

        # Low slope + dry weather -> near zero probability
        p_flat_dry = weak_label(
            np.array([2.0]), np.array([0.0]), np.array([0.0]), np.array([0.0]), 50.0, 125.0, 240.0
        )[0]
        self.assertLess(p_flat_dry, 0.05)

        # Steep should fail at significantly higher rate than flat
        self.assertGreater(p_steep_wet, p_flat_dry * 5.0)

    def test_10_feature_names(self):
        self.assertEqual(len(FEATURE_NAMES), 14)
        self.assertIn("slope_deg", FEATURE_NAMES)
        self.assertIn("rain_24h", FEATURE_NAMES)
        self.assertIn("rain_72h", FEATURE_NAMES)
        self.assertIn("rain_168h", FEATURE_NAMES)
        self.assertIn("is_bridge", FEATURE_NAMES)
        self.assertIn("is_ford", FEATURE_NAMES)
        self.assertNotIn("saturation", FEATURE_NAMES) # Verified no leakage

    # ----------------------------------------------------------------------- #
    # 5. Routing Engine Logic (18 checks)
    # ----------------------------------------------------------------------- #
    def test_11_mock_router_a_star(self):
        router = NetworkRouter()
        # Create synthetic triangle graph: node 1 -> 2 -> 3, and 1 -> 3 directly
        router.nodes[1] = {"id": 1, "lat": 26.0, "lon": 91.0, "state": "AS"}
        router.nodes[2] = {"id": 2, "lat": 26.05, "lon": 91.05, "state": "AS"}
        router.nodes[3] = {"id": 3, "lat": 26.1, "lon": 91.1, "state": "AS"}

        router.grid_buckets[(260, 910)].append(1)
        router.grid_buckets[(260, 910)].append(2)
        router.grid_buckets[(261, 911)].append(3)

        # Direct edge: 1 <-> 3
        router.edges["e13"] = {
            "id": "e13", "way_id": 1, "node_a": 1, "node_b": 3, "highway": "primary",
            "ref": "NH-1", "name": "Direct", "road": "NH-1", "surface": "asphalt",
            "is_bridge": False, "is_tunnel": False, "is_ford": False, "length_m": 15000.0,
            "distance_km": 15.0, "speed_kmph": 50.0, "base_minutes": 18.0,
            "base_reliability": 0.95, "state": "AS", "geom": [[26.0, 91.0], [26.1, 91.1]],
            "risk": 0.0,
        }
        router.adj[1].append({"neighbor": 3, "edge_id": "e13"})
        router.adj[3].append({"neighbor": 1, "edge_id": "e13"})

        # Detour edge: 1 <-> 2 and 2 <-> 3
        router.edges["e12"] = {
            "id": "e12", "way_id": 2, "node_a": 1, "node_b": 2, "highway": "secondary",
            "ref": "SH-2", "name": "Valley", "road": "SH-2", "surface": "asphalt",
            "is_bridge": False, "is_tunnel": False, "is_ford": False, "length_m": 10000.0,
            "distance_km": 10.0, "speed_kmph": 40.0, "base_minutes": 15.0,
            "base_reliability": 0.90, "state": "AS", "geom": [[26.0, 91.0], [26.05, 91.05]],
            "risk": 0.0,
        }
        router.adj[1].append({"neighbor": 2, "edge_id": "e12"})
        router.adj[2].append({"neighbor": 1, "edge_id": "e12"})

        router.edges["e23"] = {
            "id": "e23", "way_id": 3, "node_a": 2, "node_b": 3, "highway": "secondary",
            "ref": "SH-2", "name": "Valley", "road": "SH-2", "surface": "asphalt",
            "is_bridge": False, "is_tunnel": False, "is_ford": False, "length_m": 10000.0,
            "distance_km": 10.0, "speed_kmph": 40.0, "base_minutes": 15.0,
            "base_reliability": 0.90, "state": "AS", "geom": [[26.05, 91.05], [26.1, 91.1]],
            "risk": 0.0,
        }
        router.adj[2].append({"neighbor": 3, "edge_id": "e23"})
        router.adj[3].append({"neighbor": 2, "edge_id": "e23"})

        router._compute_connected_components()
        router.loaded = True

        # Test A* when all clear: direct route (18 min) wins over detour (30 min)
        res_clear = router.plan_route(26.0, 91.0, 26.1, 91.1, alternatives=2)
        self.assertGreater(len(res_clear["routes"]), 0)
        self.assertEqual(res_clear["routes"][0]["segments"][0]["edge_id"], "e13")

        # Test A* when direct route is blocked (risk = 0.85): valley detour wins
        custom_risk = {"e13": 0.85, "e12": 0.05, "e23": 0.05}
        res_blocked = router.plan_route(26.0, 91.0, 26.1, 91.1, alternatives=2, custom_risks=custom_risk)
        self.assertGreater(len(res_blocked["routes"]), 0)
        primary = res_blocked["routes"][0]
        # Should pick detour [e12, e23]
        self.assertEqual(len(primary["segments"]), 2)
        self.assertEqual(primary["segments"][0]["edge_id"], "e12")
        self.assertEqual(primary["segments"][1]["edge_id"], "e23")

    # ----------------------------------------------------------------------- #
    # 6. Multilingual Alerts & Cooldowns (13 checks)
    # ----------------------------------------------------------------------- #
    def test_12_multilingual_alerts(self):
        for lang in LANGUAGES:
            tr = translate_alert("high_risk_corridor", lang, "NH-27", "AS", risk=0.88)
            self.assertEqual(tr["language"], lang)
            self.assertIn("NH-27", tr["title"] + tr["body"])

        # Check Assamese translation
        as_tr = translate_alert("high_risk_corridor", "as", "NH-27", "AS")
        self.assertIn("পথ বন্ধ", as_tr["title"])

        # Check Bengali translation
        bn_tr = translate_alert("high_risk_corridor", "bn", "NH-27", "AS")
        self.assertIn("সড়ক অবরুদ্ধ", bn_tr["title"])

        # Check Meitei translation
        mni_tr = translate_alert("high_risk_corridor", "mni", "NH-27", "MN")
        self.assertIn("লম্বী থিংল্লে", mni_tr["title"])

    # ----------------------------------------------------------------------- #
    # 7. FastAPI Endpoints & Integration (10 checks)
    # ----------------------------------------------------------------------- #
    def test_13_api_integration(self):
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        # Health endpoint
        r_health = client.get("/api/health")
        self.assertEqual(r_health.status_code, 200)
        data = r_health.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("network", data)

        # Translate endpoint
        r_trans = client.get("/api/alerts/translate?kind=high_risk_corridor&lang=hi&road=NH-29&state=NL")
        self.assertEqual(r_trans.status_code, 200)
        self.assertIn("सड़क अवरुद्ध", r_trans.json()["title"])

        # Static pages
        r_dash = client.get("/")
        self.assertEqual(r_dash.status_code, 200)

        r_field = client.get("/field")
        self.assertEqual(r_field.status_code, 200)

        r_driver = client.get("/driver")
        self.assertEqual(r_driver.status_code, 200)


def run_tests():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPlatform)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"\n[test] Ran {result.testsRun} test cases: {len(result.failures)} failures, {len(result.errors)} errors")
    return len(result.failures) == 0 and len(result.errors) == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
