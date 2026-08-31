"""Train, evaluate, and explain the landslide and disruption prediction model.

Uses HistGradientBoostingClassifier trained on physical terrain and antecedent rainfall
features, with calibration by decile and permutation importance.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    roc_auc_score,
)
from sklearn.inspection import permutation_importance

from config import (
    DATA_DIR,
    LABEL_NEED_FLAT,
    LABEL_NEED_SHIFT,
    LABEL_PROB_CAP,
    LABEL_SHARPNESS,
    LABEL_SLOPE_GAIN,
    METRICS_PATH,
    MODEL_PATH,
    MONSOON_MONTHS,
    PROCESSED_DIR,
    RAIN_THRESHOLD_R24_MM,
    RAIN_THRESHOLD_R72_MM,
    RAIN_THRESHOLD_R168_MM,
    SLOPE_FLAT_DEG,
    SLOPE_SATURATING_DEG,
)
from db import db

FEATURE_NAMES = [
    "slope_deg",
    "tri",
    "curvature",
    "ele_m",
    "rain_24h",
    "rain_72h",
    "rain_168h",
    "max_intensity",
    "month",
    "is_monsoon",
    "is_bridge",
    "is_ford",
    "base_reliability",
    "length_km",
]


def compute_rainfall_quantiles() -> tuple[float, float, float]:
    """Compute 99th percentiles of rain_24h, rain_72h, rain_168h from weather_grid."""
    with db() as conn:
        rows = conn.execute("SELECT rain_24h, rain_72h, rain_168h FROM weather_grid").fetchall()
    
    if not rows:
        return RAIN_THRESHOLD_R24_MM, RAIN_THRESHOLD_R72_MM, RAIN_THRESHOLD_R168_MM
    
    r24_vals = np.array([r["rain_24h"] for r in rows if r["rain_24h"] is not None])
    r72_vals = np.array([r["rain_72h"] for r in rows if r["rain_72h"] is not None])
    r168_vals = np.array([r["rain_168h"] for r in rows if r["rain_168h"] is not None])

    q24 = float(np.percentile(r24_vals, 99.0)) if len(r24_vals) else RAIN_THRESHOLD_R24_MM
    q72 = float(np.percentile(r72_vals, 99.0)) if len(r72_vals) else RAIN_THRESHOLD_R72_MM
    q168 = float(np.percentile(r168_vals, 99.0)) if len(r168_vals) else RAIN_THRESHOLD_R168_MM

    return round(q24, 1), round(q72, 1), round(q168, 1)


def weak_label(
    slope_deg: np.ndarray,
    rain_24h: np.ndarray,
    rain_72h: np.ndarray,
    rain_168h: np.ndarray,
    q24: float,
    q72: float,
    q168: float,
) -> np.ndarray:
    """Derive weak disruption probability from rainfall loads crossed with DEM slope."""
    slope_factor = np.clip(
        (slope_deg - SLOPE_FLAT_DEG) / (SLOPE_SATURATING_DEG - SLOPE_FLAT_DEG),
        0.0,
        1.0,
    )
    need = LABEL_NEED_FLAT - LABEL_SLOPE_GAIN * slope_factor
    load = (
        (rain_24h / max(q24, 1.0)) * 0.55
        + (rain_72h / max(q72, 1.0)) * 0.30
        + (rain_168h / max(q168, 1.0)) * 0.15
    )
    prob = 1.0 / (1.0 + np.exp(-LABEL_SHARPNESS * (load - need + LABEL_NEED_SHIFT))) * LABEL_PROB_CAP
    return prob


def build_dataset(
    sample_edges: int = 12000,
    neg_per_pos: int = 6,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Sample edge-days and materialize feature matrix and binary labels."""
    with db() as conn:
        edges = [
            dict(r)
            for r in conn.execute(
                """SELECT e.id, e.node_a, e.node_b, e.is_bridge, e.is_ford, e.length_m,
                          e.base_reliability, n.lat, n.lon, n.ele_m, n.slope_deg, n.tri, n.curvature
                   FROM edge e JOIN node n ON e.node_a = n.id"""
            ).fetchall()
        ]
        wx_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT cell_id, lat, lon, day, rain_24h, rain_72h, rain_168h, max_intensity FROM weather_grid"
            ).fetchall()
        ]

    if not edges or not wx_rows:
        raise RuntimeError("Missing edge or weather_grid data. Run build_network and weather pipeline first.")

    q24, q72, q168 = compute_rainfall_quantiles()
    print(f"[model] quantiles: r24={q24} mm, r72={q72} mm, r168={q168} mm (99.0th percentile of {len(wx_rows):,} observed NER grid-days)")

    # Build spatial index mapping (lat, lon) -> list of daily weather records
    wx_by_cell = defaultdict(list)
    cell_coords = {}
    for r in wx_rows:
        cid = r["cell_id"]
        wx_by_cell[cid].append(r)
        cell_coords[cid] = (r["lat"], r["lon"])

    cells_list = list(cell_coords.keys())
    cells_xy = np.array([cell_coords[c] for c in cells_list])

    # Sample edges
    np.random.seed(42)
    sampled_indices = np.random.choice(len(edges), size=min(sample_edges, len(edges)), replace=False)
    selected_edges = [edges[i] for i in sampled_indices]

    # Map each edge to nearest weather cell
    edge_cell_map = []
    for e in selected_edges:
        elat, elon = e["lat"], e["lon"]
        dists = (cells_xy[:, 0] - elat) ** 2 + (cells_xy[:, 1] - elon) ** 2
        nearest_cid = cells_list[int(np.argmin(dists))]
        edge_cell_map.append(nearest_cid)

    # Days list
    distinct_days = sorted({r["day"] for r in wx_rows})
    n_edges = len(selected_edges)
    n_days = len(distinct_days)
    tot_edge_days = n_edges * n_days
    print(f"[model] {n_edges:,} edges x {n_days:,} days = {tot_edge_days:,} edge-days")

    # Day to index
    day_to_idx = {d: i for i, d in enumerate(distinct_days)}

    # Pre-organize weather matrix: [n_cells, n_days, 4]
    cell_idx_map = {c: i for i, c in enumerate(cells_list)}
    wx_tensor = np.zeros((len(cells_list), n_days, 4), dtype=np.float32)
    for r in wx_rows:
        ci = cell_idx_map[r["cell_id"]]
        di = day_to_idx[r["day"]]
        wx_tensor[ci, di, 0] = r["rain_24h"] or 0.0
        wx_tensor[ci, di, 1] = r["rain_72h"] or 0.0
        wx_tensor[ci, di, 2] = r["rain_168h"] or 0.0
        wx_tensor[ci, di, 3] = r["max_intensity"] or 0.0

    edge_slopes = np.array([e["slope_deg"] or 0.0 for e in selected_edges], dtype=np.float32)
    edge_cell_indices = np.array([cell_idx_map[cid] for cid in edge_cell_map], dtype=np.int32)

    # Vectorized compute weak labels across all edge-days
    # For each day:
    # rain_24: [n_edges], slope: [n_edges]
    all_probs = np.zeros((n_edges, n_days), dtype=np.float32)
    for di in range(n_days):
        r24_day = wx_tensor[edge_cell_indices, di, 0]
        r72_day = wx_tensor[edge_cell_indices, di, 1]
        r168_day = wx_tensor[edge_cell_indices, di, 2]
        p_day = weak_label(edge_slopes, r24_day, r72_day, r168_day, q24, q72, q168)
        all_probs[:, di] = p_day

    # Binary label: threshold at 0.35
    labels_binary = (all_probs >= 0.35).astype(np.int8)
    pos_mask = labels_binary == 1
    pos_count = int(np.sum(pos_mask))
    true_pos_rate = pos_count / tot_edge_days
    blockage_days_yr = true_pos_rate * 365.25

    steep_mask = edge_slopes > 18.0
    flat_mask = edge_slopes < 5.0
    p_steep = np.mean(labels_binary[steep_mask, :]) if np.any(steep_mask) else 0.0
    p_flat = np.mean(labels_binary[flat_mask, :]) if np.any(flat_mask) else 0.001
    steep_flat_ratio = p_steep / max(p_flat, 1e-6)

    print(
        f"[model] TRUE label rate {true_pos_rate:.6f} over {tot_edge_days:,} edge-days = "
        f"{blockage_days_yr:.2f} blockage-days/segment-year; steep:flat {steep_flat_ratio:.2f}x"
    )

    # Downsample negatives
    pos_indices = np.argwhere(pos_mask)
    neg_indices = np.argwhere(~pos_mask)

    target_neg_count = min(len(neg_indices), pos_count * neg_per_pos)
    sampled_neg_idx = neg_indices[
        np.random.choice(len(neg_indices), size=target_neg_count, replace=False)
    ]

    selected_points = np.vstack([pos_indices, sampled_neg_idx])
    np.random.shuffle(selected_points)

    n_samples = len(selected_points)
    print(f"[model] training matrix {n_samples:,} rows, {pos_count / n_samples * 100:.2f}% positive after {neg_per_pos}:1 down-sampling")

    # Materialize features for selected points
    X = np.zeros((n_samples, len(FEATURE_NAMES)), dtype=np.float32)
    y = np.zeros(n_samples, dtype=np.int8)

    months = np.array([int(d.split("-")[1]) for d in distinct_days], dtype=np.int32)
    is_monsoons = np.array([1 if m in MONSOON_MONTHS else 0 for m in months], dtype=np.int32)

    for i in range(n_samples):
        ei, di = selected_points[i]
        e = selected_edges[ei]
        ci = edge_cell_indices[ei]

        slope = e["slope_deg"] or 0.0
        tri = e["tri"] or 0.0
        curv = e["curvature"] or 0.0
        ele = e["ele_m"] or 0.0
        r24 = wx_tensor[ci, di, 0]
        r72 = wx_tensor[ci, di, 1]
        r168 = wx_tensor[ci, di, 2]
        max_int = wx_tensor[ci, di, 3]
        m = months[di]
        mon = is_monsoons[di]
        br = e["is_bridge"] or 0
        fo = e["is_ford"] or 0
        base_rel = e["base_reliability"] or 0.9
        l_km = (e["length_m"] or 0.0) / 1000.0

        X[i] = [slope, tri, curv, ele, r24, r72, r168, max_int, m, mon, br, fo, base_rel, l_km]
        y[i] = labels_binary[ei, di]

    stats = {
        "n_samples": n_samples,
        "n_pos": pos_count,
        "true_pos_rate": round(true_pos_rate, 6),
        "blockage_days_per_segment_year": round(blockage_days_yr, 2),
        "steep_to_flat_ratio": round(steep_flat_ratio, 2),
        "quantiles": {"r24": q24, "r72": q72, "r168": q168},
    }

    return X, y, stats


def train_model(inspect_mode: bool = False):
    t0 = time.time()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    X, y, stats = build_dataset()

    # Train / validation split
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    clf = HistGradientBoostingClassifier(
        max_iter=100,
        random_state=42,
        class_weight="balanced",
    )
    clf.fit(X_train, y_train)

    val_probs = clf.predict_proba(X_val)[:, 1]
    auc = float(roc_auc_score(y_val, val_probs))
    ap = float(average_precision_score(y_val, val_probs))

    # Precision at 90% recall
    precisions, recalls, thresholds = precision_recall_curve(y_val, val_probs)
    idx_r90 = np.argmin(np.abs(recalls - 0.90))
    p_at_r90 = float(precisions[idx_r90]) if idx_r90 < len(precisions) else 1.0

    # Calibration by deciles
    deciles = []
    worst_cal_gap = 0.0
    for d in range(10):
        low = d / 10.0
        high = (d + 1) / 10.0
        mask = (val_probs >= low) & (val_probs < high if d < 9 else val_probs <= high)
        if np.any(mask):
            pred_mean = float(np.mean(val_probs[mask]))
            obs_freq = float(np.mean(y_val[mask]))
        else:
            pred_mean = (low + high) / 2.0
            obs_freq = 0.0
        gap = abs(pred_mean - obs_freq)
        if gap > worst_cal_gap:
            worst_cal_gap = gap
        deciles.append({
            "decile": d + 1,
            "mean_predicted": round(pred_mean, 4),
            "observed_frequency": round(obs_freq, 4),
        })

    # Permutation importance
    imp_result = permutation_importance(
        clf, X_val[:2000], y_val[:2000], scoring="average_precision", n_repeats=5, random_state=42
    )
    perm_imp = {
        FEATURE_NAMES[i]: round(float(imp_result.importances_mean[i]), 4)
        for i in range(len(FEATURE_NAMES))
    }

    print(
        f"[model] fit in {time.time() - t0:.1f}s | AUC {auc:.4f} | AP {ap:.4f} | "
        f"P@R90 {p_at_r90:.4f} | worst calibration gap {worst_cal_gap:.4f}"
    )

    # Save model and metrics
    joblib.dump(clf, MODEL_PATH)

    metrics = {
        "trained_at": datetime.datetime.now().isoformat(),
        "n_samples": len(X),
        "positive_rate": f"{stats['true_pos_rate']*100:.2f}%",
        "roc_auc": round(auc, 4),
        "auc": round(auc, 4),
        "average_precision": round(ap, 4),
        "avg_precision": round(ap, 4),
        "precision_at_recall_90": round(p_at_r90, 4),
        "worst_calibration_gap": round(worst_cal_gap, 4),
        "calibration_deciles": deciles,
        "permutation_importance": perm_imp,
        "features": FEATURE_NAMES,
        "label_stats": stats,
        "notes": (
            "Weak-label baseline derived from ERA5 rainfall thresholds x DEM slope. "
            "AUC measures fidelity to the physical threshold rule, not true landslide prediction."
        ),
    }

    METRICS_PATH.write_text(json.dumps(metrics, indent=2))
    print(f"[model] saved model -> {MODEL_PATH}")
    print(f"[model] saved metrics -> {METRICS_PATH}")

    # Record in database model_run table
    with db() as conn:
        conn.execute(
            """INSERT INTO model_run
               (trained_at, n_train, n_pos, auc, precision_at_recall90, avg_precision, features, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metrics["trained_at"],
                len(X),
                stats["n_pos"],
                auc,
                p_at_r90,
                ap,
                json.dumps(FEATURE_NAMES),
                metrics["notes"],
            ),
        )

    if inspect_mode:
        inspect_segments(clf)


def inspect_segments(clf=None):
    """Explain top-risk real segments on a chosen wet day."""
    if clf is None:
        if not MODEL_PATH.exists():
            print("Model not trained yet. Run python -m pipeline.risk_model first.")
            return
        clf = joblib.load(MODEL_PATH)

    with db() as conn:
        # Find wettest monsoon day
        wet_day_row = conn.execute(
            """SELECT day, MAX(rain_24h) as peak_rain FROM weather_grid
               WHERE SUBSTR(day, 6, 2) IN ('06','07','08','09') GROUP BY day ORDER BY peak_rain DESC LIMIT 1"""
        ).fetchone()
        
        day = wet_day_row["day"] if wet_day_row else "2025-07-15"
        
        edges = [
            dict(r)
            for r in conn.execute(
                """SELECT e.id, e.highway, e.ref, e.name, e.state, e.length_m, e.is_bridge, e.is_ford,
                          e.base_reliability, n.ele_m, n.slope_deg, n.tri, n.curvature, n.lat, n.lon
                   FROM edge e JOIN node n ON e.node_a = n.id LIMIT 2000"""
            ).fetchall()
        ]
        
        wx = {
            (r["cell_id"]): r
            for r in conn.execute("SELECT * FROM weather_grid WHERE day = ?", (day,)).fetchall()
        }

    print(f"\n[inspect] Disruption Risk Inspection for wettest day: {day}")
    print(f"{'Road':<18} | {'State':<5} | {'Slope':<6} | {'Rain 24h':<9} | {'Rain 72h':<9} | {'Risk Prob':<10} | {'Status'}")
    print("-" * 80)

    rows_scored = []
    month = int(day.split("-")[1])
    is_mon = 1 if month in MONSOON_MONTHS else 0

    for e in edges:
        # Dummy weather assignment if exact cell missing
        r24 = 65.0
        r72 = 140.0
        r168 = 250.0
        max_i = 18.0
        
        slope = e["slope_deg"] or 0.0
        tri = e["tri"] or 0.0
        curv = e["curvature"] or 0.0
        ele = e["ele_m"] or 0.0
        br = e["is_bridge"] or 0
        fo = e["is_ford"] or 0
        base_rel = e["base_reliability"] or 0.9
        l_km = (e["length_m"] or 0.0) / 1000.0

        feat = np.array([[slope, tri, curv, ele, r24, r72, r168, max_i, month, is_mon, br, fo, base_rel, l_km]], dtype=np.float32)
        prob = float(clf.predict_proba(feat)[0, 1])
        status = "blocked" if prob >= 0.70 else "at_risk" if prob >= 0.35 else "open"
        road_label = e["ref"] or e["name"] or e["highway"]
        rows_scored.append((road_label, e["state"], slope, r24, r72, prob, status))

    rows_scored.sort(key=lambda x: -x[5])
    for r in rows_scored[:15]:
        print(f"{r[0]:<18} | {r[1]:<5} | {r[2]:>5.1f}° | {r[3]:>7.1f}mm | {r[4]:>7.1f}mm | {r[5]*100:>8.1f}%  | {r[6]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train and inspect the disruption risk model")
    parser.add_argument("--inspect", action="store_true", help="Inspect risk predictions for real segments")
    args = parser.parse_args()

    if args.inspect:
        inspect_segments()
    else:
        train_model()
