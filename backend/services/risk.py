"""Live disruption risk scoring service and state/district connectivity rollup.

Scores all network edges using the trained gradient-boosting model with live weather forecasts,
applies field report overrides, updates district connectivity indices, and triggers alerts.
"""

from __future__ import annotations

import datetime
import json
import time
from collections import defaultdict
from typing import Sequence
import joblib
import numpy as np

from config import (
    MODEL_PATH,
    MONSOON_MONTHS,
    NER_STATES,
    RISK_AT_RISK,
    RISK_BLOCKED,
)
from db import db
from routing.router import get_router
from services.alerts import evaluate_alerts
from services.imd_client import get_live_forecast

# Global in-process cache of latest edge risks: edge_id -> float
_LATEST_RISKS: dict[str, float] = {}


def load_disruption_model():
    if MODEL_PATH.exists():
        return joblib.load(MODEL_PATH)
    return None


def refresh_risk_scores() -> dict:
    """Run full live risk rescoring pass across all edges."""
    t0 = time.time()
    router = get_router()
    model = load_disruption_model()

    with db() as conn:
        rows = conn.execute(
            """SELECT e.id, e.is_bridge, e.is_ford, e.base_reliability, e.length_m, e.state,
                      n.slope_deg, n.tri, n.curvature, n.ele_m
               FROM edge e LEFT JOIN node n ON e.node_a = n.id"""
        ).fetchall()
        field_reports = [
            dict(r)
            for r in conn.execute("SELECT edge_id, category, severity FROM field_report WHERE edge_id IS NOT NULL").fetchall()
        ]

    if not rows:
        return {"status": "error", "message": "No edges in database"}

    # Live forecast for state centroids
    state_coords = [
        (28.2, 94.7), # AR
        (26.2, 92.9), # AS
        (25.5, 91.3), # ML
        (24.8, 93.9), # MN
        (23.1, 92.9), # MZ
        (26.1, 94.5), # NL
        (27.5, 88.5), # SK
        (23.8, 91.2), # TR
    ]
    forecasts = get_live_forecast(state_coords)
    state_keys = list(NER_STATES.keys())
    state_wx = {}
    for i, st in enumerate(state_keys):
        state_wx[st] = forecasts[i] if i < len(forecasts) else {
            "rain_24h": 10.0, "rain_72h": 25.0, "rain_168h": 50.0, "max_intensity": 3.0
        }

    now = datetime.datetime.now()
    month = now.month
    is_monsoon = 1 if month in MONSOON_MONTHS else 0

    # Build feature matrix
    n_edges = len(rows)
    X = np.zeros((n_edges, 14), dtype=np.float32)
    edge_ids = [None] * n_edges
    edge_states = [None] * n_edges
    edge_lengths = np.zeros(n_edges, dtype=np.float32)

    for i, r in enumerate(rows):
        eid = r["id"]
        edge_ids[i] = eid
        st = r["state"] or "AS"
        edge_states[i] = st
        l_km = (r["length_m"] or 0.0) / 1000.0
        edge_lengths[i] = l_km
        wx = state_wx.get(st, {"rain_24h": 5.0, "rain_72h": 15.0, "rain_168h": 35.0, "max_intensity": 2.0})

        slope = r["slope_deg"] or 0.0
        tri = r["tri"] or 0.0
        curv = r["curvature"] or 0.0
        ele = r["ele_m"] or 0.0
        r24 = wx["rain_24h"]
        r72 = wx["rain_72h"]
        r168 = wx["rain_168h"]
        max_int = wx["max_intensity"]
        br = r["is_bridge"] or 0
        fo = r["is_ford"] or 0
        base_rel = r["base_reliability"] or 0.95

        X[i] = [slope, tri, curv, ele, r24, r72, r168, max_int, month, is_monsoon, br, fo, base_rel, l_km]

    # Predict probabilities
    if model is not None:
        probs = model.predict_proba(X)[:, 1]
    else:
        # Fallback heuristic if model file not present yet
        probs = np.clip((X[:, 0] / 45.0) * 0.4 + (X[:, 4] / 100.0) * 0.5, 0.01, 0.95)

    _LATEST_RISKS.clear()
    scored_edges = []

    # Map field reports for instant pin overrides
    report_pins = {}
    for fr in field_reports:
        cat = fr["category"]
        eid = fr["edge_id"]
        if cat in ("landslide", "flood", "blocked", "road_damage"):
            report_pins[eid] = 1.0
        elif cat == "clear":
            report_pins[eid] = 0.0

    # Direct State Rollup statistics counters
    state_stats = defaultdict(lambda: {
        "tot": 0, "open": 0, "risk": 0, "blocked": 0, "km": 0.0, "risk_sum": 0.0, "br_risk": 0
    })

    for i, r in enumerate(rows):
        eid = edge_ids[i]
        p = float(probs[i])
        if eid in report_pins:
            p = report_pins[eid]
        
        _LATEST_RISKS[eid] = p
        if router and eid in router.edges:
            router.edges[eid].risk = p

        st = edge_states[i]
        km = edge_lengths[i]
        is_br = r["is_bridge"] or 0
        s = state_stats[st]
        s["tot"] += 1
        s["km"] += km
        s["risk_sum"] += p
        if p >= RISK_BLOCKED:
            s["blocked"] += 1
        elif p >= RISK_AT_RISK:
            s["risk"] += 1
            if is_br:
                s["br_risk"] += 1
        else:
            s["open"] += 1

    district_rows = []
    for st, s in state_stats.items():
        tot = s["tot"]
        n_open = s["open"]
        n_risk = s["risk"]
        n_block = s["blocked"]
        tot_km = s["km"]
        mean_r = s["risk_sum"] / tot if tot else 0.0
        br_risk = s["br_risk"]
        conn_idx = (n_open + 0.5 * n_risk) / tot if tot else 1.0

        district_rows.append({
            "district": st,
            "state": st,
            "segments_total": tot,
            "segments_open": n_open,
            "segments_at_risk": n_risk,
            "segments_blocked": n_block,
            "mean_risk": round(mean_r, 4),
            "bridges_at_risk": br_risk,
            "network_km": round(tot_km, 1),
            "connectivity_index": round(conn_idx, 4),
            "updated_at": now.isoformat(),
        })

    with db() as conn:
        conn.execute("DELETE FROM district_status")
        conn.executemany(
            """INSERT OR REPLACE INTO district_status
               (district, state, segments_total, segments_open, segments_at_risk,
                segments_blocked, mean_risk, bridges_at_risk, network_km, connectivity_index, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    d["district"],
                    d["state"],
                    d["segments_total"],
                    d["segments_open"],
                    d["segments_at_risk"],
                    d["segments_blocked"],
                    d["mean_risk"],
                    d["bridges_at_risk"],
                    d["network_km"],
                    d["connectivity_index"],
                    d["updated_at"],
                )
                for d in district_rows
            ],
        )

    # Trigger alerts
    evaluate_alerts(scored_edges)

    return {
        "status": "ok",
        "scored_segments": n_edges,
        "elapsed_seconds": round(time.time() - t0, 2),
        "mean_risk": round(float(np.mean(probs)), 4),
        "segments_at_risk": int(np.sum(probs >= RISK_AT_RISK)),
        "segments_blocked": int(np.sum(probs >= RISK_BLOCKED)),
    }


def get_latest_edge_risks() -> dict[str, float]:
    """Get mapping of edge_id to current risk float."""
    if not _LATEST_RISKS:
        refresh_risk_scores()
    return _LATEST_RISKS
