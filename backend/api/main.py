"""FastAPI application serving dashboard, offline field PWA, driver tracking, and routing API.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, List, Optional
from contextlib import asynccontextmanager
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
from db import db, ensure_db_ready, init_schema
from services.alerts import translate_alert
from services.vision import verify_base64_photo, analyze_image_bytes
from services.news import (
    get_live_disaster_news,
    verify_corridor_condition,
    add_disaster_news,
    init_news_schema,
)

try:
    from routing.router import get_router, haversine_km
    from services.risk import get_latest_edge_risks, get_latest_edge_reasons, get_edge_reason, refresh_risk_scores
    _ROUTER_AVAILABLE = True
except Exception as _e:
    import warnings
    warnings.warn(f"Router/risk unavailable (empty DB?): {_e}")
    _ROUTER_AVAILABLE = False
    def get_router(): return None  # type: ignore
    def haversine_km(*a, **kw): return 0.0  # type: ignore
    def get_latest_edge_risks(): return {}  # type: ignore
    def get_latest_edge_reasons(): return {}  # type: ignore
    def get_edge_reason(eid): return ""  # type: ignore
    def refresh_risk_scores(): return {"status": "no data"}  # type: ignore

FRONTEND_DIR = BASE_DIR / "frontend"
PHOTOS_DIR = PROCESSED_DIR / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure the SQLite database is unpacked and routing graph is preloaded into RAM on startup."""
    ensure_db_ready()
    import logging
    logger = logging.getLogger("uvicorn")
    logger.info("DB is ready. Pre-loading road network routing graph into memory...")
    try:
        router = get_router()
        if router and not router.loaded:
            router.load_graph()
        refresh_risk_scores()
        logger.info(f"Road network graph pre-loaded ({len(router.edges) if router else 0:,} edges). Server ready.")
    except Exception as exc:
        logger.warning(f"Router preloading notice: {exc}")
    yield


app = FastAPI(
    title="NER Logistics & Accessibility Intelligence Platform API",
    description="Real-time road accessibility, landslide/flood disruption prediction, and risk-aware routing for India's North Eastern Region.",
    version="1.0.0",
    lifespan=lifespan,
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
    photo_data: Optional[str] = None
    client_uuid: Optional[str] = None


class ImageVerifyRequest(BaseModel):
    image_base64: str
    category: str


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
    status: Optional[str] = "in_transit"
    eta_minutes: Optional[float] = 120.0


class ShipmentStatusUpdate(BaseModel):
    status: str
    eta_minutes: Optional[float] = None


class CorridorVerifyRequest(BaseModel):
    road: str
    state: Optional[str] = "NER"


class DisasterNewsItem(BaseModel):
    source: str
    headline: str
    summary: str
    road_ref: Optional[str] = "NH-6"
    state: Optional[str] = "NER"
    severity: Optional[str] = "warning"
    is_blocked: Optional[bool] = False
    divert_info: Optional[str] = ""
    speech_en: Optional[str] = None
    speech_hi: Optional[str] = None
    speech_as: Optional[str] = None


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
    reasons = get_latest_edge_reasons()

    features = []
    with db() as conn:
        if state:
            query = """SELECT id, highway, ref, name, state, length_m, is_bridge, is_ford, geom 
                       FROM edge 
                       WHERE state = ? 
                         AND (highway IN ('trunk', 'primary', 'secondary', 'tertiary', 'trunk_link', 'primary_link') 
                              OR is_bridge = 1 
                              OR is_ford = 1)
                       LIMIT 1500"""
            rows = conn.execute(query, [state]).fetchall()
        else:
            # For all-state overview, return top major arteries and monitored assets (fast ~600KB payload)
            query = """SELECT id, highway, ref, name, state, length_m, is_bridge, is_ford, geom 
                       FROM edge 
                       WHERE highway IN ('trunk', 'primary', 'secondary') 
                          OR is_bridge = 1 
                          OR is_ford = 1
                       LIMIT 1000"""
            rows = conn.execute(query).fetchall()

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
        reason_text = reasons.get(eid, "Terrain & Weather Disruption Risk")

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
                "reason": reason_text,
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
    reasons = get_latest_edge_reasons()
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
            "reason": reasons.get(eid, "Terrain & Weather Disruption Risk"),
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
    try:
        router = get_router()
        if not router or not getattr(router, "nodes", None):
            if router:
                router.load_graph()
            else:
                return {
                    "routes": [],
                    "computed_in_ms": 0,
                    "diagnostics": "Road network graph is warming up. Please try again in 3 seconds.",
                    "model_note": "A* with Yen K-alternates",
                    "snap_distance_m": [0.0, 0.0],
                }

        risks = get_latest_edge_risks() if req.use_live_forecast else None
        res = router.plan_route(
            req.lat1,
            req.lon1,
            req.lat2,
            req.lon2,
            alternatives=req.alternatives,
            custom_risks=risks,
        )

        reasons = get_latest_edge_reasons()
        for rt in res.get("routes", []):
            block_reasons = []
            for s in rt.get("segments", []):
                eid = s.get("edge_id")
                s_reason = reasons.get(eid, "")
                s["reason"] = s_reason
                if s.get("risk", 0) >= RISK_AT_RISK and s_reason and s_reason not in block_reasons:
                    block_reasons.append(s_reason)
            if block_reasons:
                rt["blockage_reason"] = "; ".join(block_reasons[:2])
            elif rt.get("passes_blocked_segment"):
                rt["blockage_reason"] = "Severe landslide and road obstruction on active corridor"
            else:
                rt["blockage_reason"] = "Clear corridor: stable terrain and normal transit flow"

        return res
    except Exception as exc:
        return {
            "routes": [],
            "computed_in_ms": 0,
            "diagnostics": f"Route calculation error: {str(exc)}",
            "model_note": "A* with Yen K-alternates",
            "snap_distance_m": [0.0, 0.0],
        }


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
                edge_id = incident[0][1] if incident else None
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


@app.post("/api/field-report/verify")
def api_verify_hazard_photo(req: ImageVerifyRequest):
    """
    AI Computer Vision verification: analyzes image data against reported hazard category.
    """
    res = verify_base64_photo(req.image_base64, req.category)
    return res


@app.post("/api/field-report")
def submit_single_field_report(item: FieldReportItem):
    router = get_router()
    now_str = datetime.datetime.now().isoformat()

    # Optional AI Vision verification
    ai_res = None
    if item.photo_data:
        ai_res = verify_base64_photo(item.photo_data, item.category)

    try:
        snapped_node, _ = router.snap_node(item.lat, item.lon)
        incident = router.adj.get(snapped_node, [])
        if incident:
            first = incident[0]
            if isinstance(first, (list, tuple)) and len(first) > 1:
                edge_id = first[1]
            elif isinstance(first, dict):
                edge_id = first.get("edge_id")
            else:
                edge_id = None
        else:
            edge_id = None
    except Exception:
        edge_id = None

    with db() as conn:
        # Idempotency check: avoid double-inserting if retried across flaky 2G uplink
        existing = conn.execute(
            """SELECT id FROM field_report 
               WHERE lat = ? AND lon = ? AND category = ? AND captured_at = ?
               LIMIT 1""",
            (item.lat, item.lon, item.category, item.captured_at or now_str),
        ).fetchone()

        if not existing:
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
                    1 if (ai_res is None or ai_res.get("verified")) else 0,
                    edge_id,
                ),
            )

    # Rescore risk
    refresh_risk_scores()
    return {
        "status": "ok",
        "ai_verification": ai_res,
        "edge_id": edge_id,
        "timestamp": now_str,
    }


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

    ai_res = analyze_image_bytes(content, category)

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
            (lat, lon, category, 3, note, str(dst), device_id, now_str, 1 if ai_res.get("verified") else 0, edge_id),
        )

    return {"status": "ok", "photo_path": str(dst), "ai_verification": ai_res}


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
                s.status or "in_transit",
                s.priority,
                now_str,
                s.eta_minutes or 120.0,
            ),
        )
    return {"status": "ok", "id": s.id}


@app.post("/api/shipments/{shipment_id}/status")
def update_shipment_status(shipment_id: str, payload: ShipmentStatusUpdate):
    with db() as conn:
        if payload.eta_minutes is not None:
            conn.execute(
                "UPDATE shipment SET status = ?, eta_minutes = ? WHERE id = ?",
                (payload.status, payload.eta_minutes, shipment_id),
            )
        else:
            conn.execute(
                "UPDATE shipment SET status = ? WHERE id = ?",
                (payload.status, shipment_id),
            )
    return {"status": "ok", "id": shipment_id, "new_status": payload.status}


@app.post("/api/shipments/ping")
def ping_shipment(p: GpsPingItem):
    now_str = datetime.datetime.now().isoformat()
    with db() as conn:
        shipment = conn.execute("SELECT * FROM shipment WHERE id = ?", (p.shipment_id,)).fetchone()
        if not shipment:
            conn.execute(
                """INSERT OR IGNORE INTO shipment (id, commodity, origin, destination, dest_lat, dest_lon, status, created_at)
                   VALUES (?, 'Essential Supplies', 'Origin Depot', 'Destination Hub', ?, ?, 'in_transit', ?)""",
                (p.shipment_id, p.lat, p.lon, now_str),
            )
            shipment = conn.execute("SELECT * FROM shipment WHERE id = ?", (p.shipment_id,)).fetchone()

        conn.execute(
            """INSERT INTO gps_ping (shipment_id, lat, lon, speed_kmph, heading, battery, pinged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (p.shipment_id, p.lat, p.lon, p.speed_kmph, p.heading, p.battery, p.at or now_str),
        )

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


# --------------------------------------------------------------------------- #
# Real-Time Disaster News & Hazard Verification Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/news/live")
def api_get_live_news(
    limit: int = Query(20, ge=1, le=100),
    state: Optional[str] = None,
    road: Optional[str] = None,
):
    news = get_live_disaster_news(limit=limit, state_filter=state, road_filter=road)
    return {"count": len(news), "news": news}


@app.get("/api/news/ticker")
def api_get_news_ticker():
    news = get_live_disaster_news(limit=10)
    ticker = [
        f"🚨 {n['source']}: {n['headline']} ({n['published_at'].split('T')[1][:5] if 'T' in n['published_at'] else ''})"
        for n in news
    ]
    return {"ticker": ticker, "items": news}


@app.post("/api/news/verify-corridor")
def api_verify_corridor(req: CorridorVerifyRequest):
    return verify_corridor_condition(req.road, req.state or "NER")


@app.post("/api/news/report")
def api_submit_disaster_news(item: DisasterNewsItem):
    res = add_disaster_news(item.model_dump())
    refresh_risk_scores()
    return res


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
