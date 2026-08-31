"""FastAPI application serving dashboard, offline field PWA, driver tracking, and routing API.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, List, Optional
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    BASE_DIR,
    METRICS_PATH,
    MODEL_PATH,
    PROCESSED_DIR,
    RISK_AT_RISK,
    RISK_BLOCKED,
)
from db import db, init_schema
from routing.router import get_router, haversine_km
from services.alerts import translate_alert
from services.risk import get_latest_edge_risks, refresh_risk_scores

FRONTEND_DIR = BASE_DIR / "frontend"
PHOTOS_DIR = PROCESSED_DIR / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="NER Logistics & Accessibility Intelligence Platform API",
    description="Real-time road accessibility, landslide/flood disruption prediction, and risk-aware routing for India's North Eastern Region.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class RouteRequest(BaseModel):
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    alternatives: int = 3
    use_live_forecast: bool = True


class FieldReportItem(BaseModel):
    lat: float
    lon: float
    category: str
    severity: int = 3
    note: Optional[str] = ""
    device_id: Optional[str] = ""
    reported_by: Optional[str] = ""
    captured_at: Optional[str] = None


class ShipmentCreate(BaseModel):
    id: str
    commodity: str
    origin: Optional[str] = ""
    destination: Optional[str] = ""
    dest_lat: Optional[float] = 0.0
    dest_lon: Optional[float] = 0.0
    vehicle_no: Optional[str] = ""
    driver_name: Optional[str] = ""
    driver_phone: Optional[str] = ""
    priority: int = 2


class GpsPingItem(BaseModel):
    shipment_id: str
    lat: float
    lon: float
    speed_kmph: Optional[float] = 0.0
    heading: Optional[float] = 0.0
    battery: Optional[float] = 100.0
    at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Web Pages & Static Mounts
# --------------------------------------------------------------------------- #
@app.get("/", response_class=FileResponse)
def serve_dashboard():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>NER Logistics Platform</h1><p>Dashboard template not found.</p>")


@app.get("/field", response_class=FileResponse)
def serve_field_app():
    field_file = FRONTEND_DIR / "field.html"
    if field_file.exists():
        return FileResponse(field_file)
    return HTMLResponse("<h1>Field App</h1>")


@app.get("/driver", response_class=FileResponse)
def serve_driver_app():
    driver_file = FRONTEND_DIR / "driver.html"
    if driver_file.exists():
        return FileResponse(driver_file)
    return HTMLResponse("<h1>Driver App</h1>")


@app.get("/sw.js", response_class=FileResponse)
@app.get("/static/sw.js", response_class=FileResponse)
def serve_service_worker():
    sw_file = FRONTEND_DIR / "sw.js"
    if sw_file.exists():
        return FileResponse(sw_file, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="Service worker file not found")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# --------------------------------------------------------------------------- #
# API Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def get_health():
    with db() as conn:
        edge_count = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        node_count = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        bridge_count = conn.execute("SELECT COUNT(*) FROM edge WHERE is_bridge = 1").fetchone()[0]
        ford_count = conn.execute("SELECT COUNT(*) FROM edge WHERE is_ford = 1").fetchone()[0]
        net_km = conn.execute("SELECT SUM(length_m)/1000.0 FROM edge").fetchone()[0] or 0.0
        wx_days = conn.execute("SELECT COUNT(DISTINCT day) FROM weather_grid").fetchone()[0]
        report_count = conn.execute("SELECT COUNT(*) FROM field_report").fetchone()[0]

    model_metrics = None
    if METRICS_PATH.exists():
        try:
            model_metrics = json.loads(METRICS_PATH.read_text())
        except Exception:
            pass

    return {
        "status": "ok",
        "network": {
            "network_km": round(net_km, 1),
            "edges": edge_count,
            "nodes": node_count,
            "bridges": bridge_count,
            "fords": ford_count,
            "weather_days": wx_days,
            "field_reports": report_count,
        },
        "model": {
            "auc": model_metrics.get("roc_auc") if model_metrics else None,
            "avg_precision": model_metrics.get("average_precision") if model_metrics else None,
            "notes": model_metrics.get("notes") if model_metrics else "Model not trained yet",
        } if model_metrics else None,
        "hint": "" if edge_count > 0 else "Run make all to build network graph.",
    }


@app.get("/api/network/edges.geojson")
def get_edges_geojson(
    state: Optional[str] = None,
    min_risk: float = Query(0.0, ge=0.0, le=1.0),
    simplify: bool = True,
):
    router = get_router()
    risks = get_latest_edge_risks()

    features = []
    with db() as conn:
        query = "SELECT id, highway, ref, name, state, length_m, is_bridge, is_ford, geom FROM edge"
        params = []
        if state:
            query += " WHERE state = ?"
            params.append(state)

        rows = conn.execute(query, params).fetchall()

    for r in rows:
        eid = r["id"]
        risk = risks.get(eid, 0.0)
        if risk < min_risk:
            continue

        geom_raw = json.loads(r["geom"]) if r["geom"] else []
        if not geom_raw:
            continue

        # GeoJSON coordinates format: [lon, lat]
        coords = [[pt[1], pt[0]] for pt in geom_raw]
        road_label = r["ref"] or r["name"] or r["highway"] or "Road"
        status = "blocked" if risk >= RISK_BLOCKED else "at_risk" if risk >= RISK_AT_RISK else "open"

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "id": eid,
                "road": road_label,
                "highway": r["highway"],
                "km": round((r["length_m"] or 0.0) / 1000.0, 2),
                "state": r["state"],
                "risk": round(risk, 4),
                "status": status,
                "bridge": bool(r["is_bridge"]),
                "ford": bool(r["is_ford"]),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@app.get("/api/risk/districts")
def get_districts_status():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM district_status ORDER BY connectivity_index").fetchall()]
    return {"rows": rows}


@app.get("/api/risk/corridors")
def get_high_risk_corridors(n: int = Query(20, ge=1, le=100)):
    risks = get_latest_edge_risks()
    with db() as conn:
        rows = conn.execute(
            """SELECT e.id, e.highway, e.ref, e.name, e.state, e.length_m, e.is_bridge, e.is_ford, n.lat, n.lon
               FROM edge e JOIN node n ON e.node_a = n.id"""
        ).fetchall()

    scored = []
    for r in rows:
        eid = r["id"]
        p = risks.get(eid, 0.0)
        road_label = r["ref"] or r["name"] or r["highway"] or "Road"
        status = "blocked" if p >= RISK_BLOCKED else "at_risk" if p >= RISK_AT_RISK else "open"
        scored.append({
            "edge_id": eid,
            "road": road_label,
            "state": r["state"],
            "km": round((r["length_m"] or 0.0) / 1000.0, 2),
            "risk": round(p, 4),
            "status": status,
            "bridge": bool(r["is_bridge"]),
            "ford": bool(r["is_ford"]),
            "lat": r["lat"],
            "lon": r["lon"],
        })

    scored.sort(key=lambda x: -x["risk"])
    return {"corridors": scored[:n]}


@app.get("/api/risk/refresh")
def api_refresh_risk():
    res = refresh_risk_scores()
    return res


@app.post("/api/route")
def api_plan_route(req: RouteRequest):
    router = get_router()
    risks = get_latest_edge_risks() if req.use_live_forecast else None
    res = router.plan_route(
        req.lat1,
        req.lon1,
        req.lat2,
        req.lon2,
        alternatives=req.alternatives,
        custom_risks=risks,
    )
    return res


@app.get("/api/alerts")
def get_alerts(limit: int = Query(25, ge=1, le=100)):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM alert ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
    return {"alerts": rows}


@app.get("/api/alerts/translate")
def api_translate_alert(
    kind: str = "high_risk_corridor",
    lang: str = "en",
    road: str = "NH-27",
    state: str = "AS",
):
    return translate_alert(kind=kind, lang=lang, road=road, state=state)


@app.get("/api/field/reports")
def get_field_reports(limit: int = Query(50, ge=1, le=200)):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM field_report ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()]
    return {"reports": rows}


@app.post("/api/field/reports")
def submit_field_reports(reports: List[FieldReportItem]):
    router = get_router()
    now_str = datetime.datetime.now().isoformat()

    inserted = 0
    with db() as conn:
        for item in reports:
            # Snap to nearest node / edge
            try:
                snapped_node, _ = router.snap_node(item.lat, item.lon)
                # Find an incident edge to node
                incident = router.adj.get(snapped_node, [])
                edge_id = incident[0]["edge_id"] if incident else None
            except Exception:
                edge_id = None

            conn.execute(
                """INSERT INTO field_report
                   (lat, lon, category, severity, note, device_id, reported_by, captured_at, received_at, validated, edge_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.lat,
                    item.lon,
                    item.category,
                    item.severity,
                    item.note,
                    item.device_id,
                    item.reported_by,
                    item.captured_at or now_str,
                    now_str,
                    1,
                    edge_id,
                ),
            )
            inserted += 1

    # Rescore risk to immediately reflect field observation
    refresh_risk_scores()
    return {"status": "ok", "inserted": inserted}


@app.post("/api/field/reports/photo")
async def submit_field_photo(
    file: UploadFile = File(...),
    lat: float = Form(...),
    lon: float = Form(...),
    category: str = Form(...),
    note: Optional[str] = Form(""),
    device_id: Optional[str] = Form(""),
):
    fname = f"photo_{int(time.time()*1000)}_{file.filename}"
    dst = PHOTOS_DIR / fname
    content = await file.read()
    dst.write_bytes(content)

    router = get_router()
    try:
        snapped_node, _ = router.snap_node(lat, lon)
        incident = router.adj.get(snapped_node, [])
        edge_id = incident[0]["edge_id"] if incident else None
    except Exception:
        edge_id = None

    now_str = datetime.datetime.now().isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO field_report
               (lat, lon, category, severity, note, photo_path, device_id, received_at, validated, edge_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lat, lon, category, 3, note, str(dst), device_id, now_str, 1, edge_id),
        )

    return {"status": "ok", "photo_path": str(dst)}


@app.get("/api/shipments")
def list_shipments():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM shipment ORDER BY created_at DESC").fetchall()]
    return {"count": len(rows), "shipments": rows}


@app.post("/api/shipments")
def create_shipment(s: ShipmentCreate):
    now_str = datetime.datetime.now().isoformat()
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO shipment
               (id, commodity, origin, destination, dest_lat, dest_lon, vehicle_no,
                driver_name, driver_phone, status, priority, created_at, eta_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                s.id,
                s.commodity,
                s.origin,
                s.destination,
                s.dest_lat,
                s.dest_lon,
                s.vehicle_no,
                s.driver_name,
                s.driver_phone,
                "in_transit",
                s.priority,
                now_str,
                120.0,
            ),
        )
    return {"status": "ok", "id": s.id}


@app.post("/api/shipments/ping")
def ping_shipment(p: GpsPingItem):
    now_str = datetime.datetime.now().isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO gps_ping (shipment_id, lat, lon, speed_kmph, heading, battery, pinged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (p.shipment_id, p.lat, p.lon, p.speed_kmph, p.heading, p.battery, p.at or now_str),
        )
        shipment = conn.execute("SELECT * FROM shipment WHERE id = ?", (p.shipment_id,)).fetchone()

    if not shipment or not shipment["dest_lat"]:
        return {
            "shipment_id": p.shipment_id,
            "eta_minutes": 60.0,
            "distance_remaining_km": 45.0,
            "risk_ahead": 0.1,
            "advice": None,
        }

    d_lat = shipment["dest_lat"]
    d_lon = shipment["dest_lon"]

    router = get_router()
    risks = get_latest_edge_risks()
    routing_res = router.plan_route(p.lat, p.lon, d_lat, d_lon, alternatives=2, custom_risks=risks)

    routes = routing_res.get("routes", [])
    if not routes:
        return {
            "shipment_id": p.shipment_id,
            "eta_minutes": None,
            "distance_remaining_km": round(haversine_km(p.lat, p.lon, d_lat, d_lon), 1),
            "risk_ahead": 0.8,
            "advice": {"reroute": False, "reason": "No viable route forward."},
        }

    primary = routes[0]
    eta = primary["risk_adjusted_minutes"]
    dist_km = primary["distance_km"]
    max_risk = primary["max_segment_risk"]

    advice = None
    if len(routes) > 1 and primary["passes_blocked_segment"]:
        alt = routes[1]
        saved = max(0.0, primary["risk_adjusted_minutes"] - alt["risk_adjusted_minutes"])
        advice = {
            "reroute": True,
            "reason": "Primary route blocked by landslide/flood.",
            "saved_minutes": round(saved, 1),
        }
    elif max_risk >= RISK_BLOCKED:
        advice = {
            "reroute": False,
            "reason": f"Corridor ahead is assessed as blocked (risk {max_risk*100:.0f}%).",
        }

    return {
        "shipment_id": p.shipment_id,
        "eta_minutes": round(eta, 1),
        "distance_remaining_km": round(dist_km, 1),
        "risk_ahead": round(max_risk, 4),
        "advice": advice,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
