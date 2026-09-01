/*
 * NER Logistics Platform — Command Centre Dashboard & Live Tracking Engine
 *
 * Real-time GIS road accessibility, interactive corridor selection,
 * and live Zomato-style driver tracking simulation after consignment confirmation.
 */

const API = ""; // same origin
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const COLOUR = { open: "#2ecc71", at_risk: "#f5a623", blocked: "#ff4d4f" };
const SEV = { low: "b-open", moderate: "b-risk", high: "b-blocked", critical: "b-critical" };

let map, edgeLayer, routeLayers = [], originMarker = null, destMarker = null;
let routeMode = false, origin = null, destination = null;
let state = { risk: 0, stateFilter: "" };

// Route & Road Selection State
let activeRoutes = [];
let selectedRouteIndex = 0;
let currentOriginName = "Origin Point";
let currentDestName = "Destination Point";

// Live Zomato Driver Tracking Simulation State
let driverSim = {
  active: false,
  paused: false,
  timerId: null,
  stepIdx: 0,
  densePath: [],
  totalDistanceKm: 0,
  totalMinutes: 0,
  speedMultiplier: 1,
  followDriver: true,
  mission: null,
  driverMarker: null,
  traversedLayer: null,
  remainingLayer: null,
};

/* ---------------------------------------------------------------- map ---- */
function initMap() {
  map = L.map("map", { preferCanvas: true }).setView([25.6, 92.8], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors | risk model: NER-LAIP',
  }).addTo(map);
  edgeLayer = L.layerGroup().addTo(map);
  map.on("click", onMapClick);
}

function onMapClick(e) {
  if (!routeMode) return;
  const pt = [e.latlng.lat, e.latlng.lng];
  if (!origin || (origin && destination)) {
    clearRoute();
    origin = pt;
    currentOriginName = `Coord (${pt[0].toFixed(3)}, ${pt[1].toFixed(3)})`;
    originMarker = L.circleMarker(pt, {
      color: "#4da3ff", fillColor: "#4da3ff", fillOpacity: 1, radius: 8
    }).addTo(map).bindPopup("📍 Origin point set. Now click Destination on the map.").openPopup();
    
    $("#routes").innerHTML = '<div class="hint">📍 <b>Origin set.</b> Now click a destination on the map to calculate best route &amp; alternates.</div>';
  } else if (!destination) {
    destination = pt;
    currentDestName = `Coord (${pt[0].toFixed(3)}, ${pt[1].toFixed(3)})`;
    destMarker = L.circleMarker(pt, {
      color: "#ff4d4f", fillColor: "#ff4d4f", fillOpacity: 1, radius: 8
    }).addTo(map).bindPopup("🏁 Destination point").openPopup();
    planRoute();
  }
}

function clearRoute() {
  routeLayers.forEach((l) => map.removeLayer(l));
  routeLayers = [];
  [originMarker, destMarker].forEach((m) => m && map.removeLayer(m));
  originMarker = destMarker = null;
  origin = destination = null;
  activeRoutes = [];
  selectedRouteIndex = 0;
  $("#routes").innerHTML = '<div class="hint">No route planned yet. Select a Quick Corridor or click 2 points on the map.</div>';
}

/* -------------------------------------------------------------- data ---- */
async function loadHealth() {
  try {
    const res = await fetch(`${API}/api/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const h = await res.json();
    if (h && window.nerDB) window.nerDB.setCache("health", h);
    renderHealth(h);
  } catch (e) {
    if (window.nerDB) {
      const cached = await window.nerDB.getCache("health");
      if (cached) {
        renderHealth(cached);
        setStatus("Offline Mode: Serving cached platform metrics.");
        return;
      }
    }
    setStatus("Cannot reach the API: " + e.message);
  }
}

function renderHealth(h) {
  if (h.status !== "ok") { setStatus("Network not built yet: " + (h.hint || "")); return; }
  $("#pillNet").textContent =
    `${h.network.network_km.toLocaleString()} km · ${h.network.edges.toLocaleString()} segments · ${h.network.bridges.toLocaleString()} bridges`;
  $("#pillModel").textContent = h.model
    ? `model AUC ${h.model.auc} · AP ${h.model.avg_precision}`
    : "model not trained";
  $("#pillModel").title = h.model ? h.model.notes : "";
  $("#pillTime").textContent = new Date().toLocaleTimeString();

  const k = h.network;
  $("#kpis").innerHTML = [
    ["Network", `${k.network_km.toLocaleString()} km`],
    ["Road segments", k.edges.toLocaleString()],
    ["Bridges monitored", k.bridges.toLocaleString()],
    ["River fords", k.fords.toLocaleString()],
    ["Rainfall days", k.weather_days.toLocaleString()],
    ["Field reports", k.field_reports.toLocaleString()],
  ].map(([l, v]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
}

async function loadEdges() {
  const cacheKey = `edges_${state.stateFilter || "all"}_${state.risk}`;
  try {
    const params = new URLSearchParams({ simplify: "true" });
    if (state.stateFilter) params.set("state", state.stateFilter);
    if (state.risk > 0) params.set("min_risk", state.risk);
    const res = await fetch(`${API}/api/network/edges.geojson?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const gj = await res.json();
    if (!gj || !gj.features) return;
    if (window.nerDB) window.nerDB.setCache(cacheKey, gj);
    renderEdges(gj);
  } catch (err) {
    if (window.nerDB) {
      const cached = await window.nerDB.getCache(cacheKey);
      if (cached) {
        renderEdges(cached);
        setStatus("Offline Mode: Serving cached road corridor map.");
        return;
      }
    }
    console.warn("Edge overlay loading bypassed:", err);
  }
}

function renderEdges(gj) {
  edgeLayer.clearLayers();
  L.geoJSON(gj, {
    style: (f) => ({
      color: COLOUR[f.properties.status] || COLOUR.open,
      weight: f.properties.status === "open" ? 1.4 : 3.5,
      opacity: f.properties.status === "open" ? 0.6 : 0.95,
    }),
    onEachFeature: (f, layer) => {
      const p = f.properties;
      const reasonHtml = p.reason ? `<div style="margin-top:5px;padding:4px 7px;background:rgba(245,158,11,0.15);border-radius:6px;font-size:11px;color:#fde68a;"><b>⚠️ Hazard Factor:</b> ${p.reason}</div>` : "";
      
      layer.bindPopup(
        `<b>${p.road}</b><br>class: ${p.highway} · ${p.km} km<br>` +
        `state: ${p.state}<br>risk: <b>${(p.risk * 100).toFixed(1)}%</b> (${p.status})` +
        reasonHtml +
        (p.bridge ? "<br>🌉 bridge monitored" : "") + (p.ford ? "<br>🌊 river ford" : "") +
        `<div style="margin-top:8px;"><button type="button" class="primary" style="padding:3px 8px;font-size:11px;" onclick="inspectRoadFromMap('${p.id}', '${p.road}', '${p.state}', '${p.highway}', ${p.km}, ${p.risk}, '${p.status}', '${(p.reason || '').replace(/'/g, "\\'")}', ${p.bridge}, ${p.ford})">🔍 View Selection Info</button></div>`
      );

      layer.on("click", () => {
        inspectRoad(p);
      });
    },
  }).addTo(edgeLayer);
  setStatus(`Loaded ${gj.features.length} road segments`);
}

async function loadDistricts() {
  try {
    const res = await fetch(`${API}/api/risk/districts`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (d && window.nerDB) window.nerDB.setCache("districts", d);
    renderDistricts(d);
  } catch (err) {
    if (window.nerDB) {
      const cached = await window.nerDB.getCache("districts");
      if (cached) { renderDistricts(cached); return; }
    }
    console.warn("District status loading bypassed:", err);
  }
}

function renderDistricts(d) {
  const tb = $("#tblDistricts tbody");
  if (!tb || !d.rows) return;
  tb.innerHTML = d.rows.map((r) => {
    const pct = (r.connectivity_index * 100).toFixed(0);
    const c = r.connectivity_index > 0.85 ? COLOUR.open
      : r.connectivity_index > 0.6 ? COLOUR.at_risk : COLOUR.blocked;
    return `<tr><td>${r.state}</td><td>${r.segments_open}</td>
      <td>${r.segments_at_risk}</td><td>${r.segments_blocked}</td>
      <td><div style="display:flex;gap:6px;align-items:center">
        <div class="bar" style="width:70px"><i style="width:${pct}%;background:${c}"></i></div>
        <span class="mono">${pct}%</span></div></td></tr>`;
  }).join("");

  const sel = $("#selState");
  if (sel && sel.options.length <= 1) {
    d.rows.forEach((r) => {
      const o = document.createElement("option");
      o.value = r.state; o.textContent = r.state;
      sel.appendChild(o);
    });
  }
}

async function loadCorridors() {
  try {
    const res = await fetch(`${API}/api/risk/corridors?n=20`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (d && window.nerDB) window.nerDB.setCache("corridors", d);
    renderCorridors(d);
  } catch (err) {
    if (window.nerDB) {
      const cached = await window.nerDB.getCache("corridors");
      if (cached) { renderCorridors(cached); return; }
    }
    console.warn("Corridors loading bypassed:", err);
  }
}

function renderCorridors(d) {
  const tb = $("#tblCorridors tbody");
  if (!tb || !d.corridors) return;
  tb.innerHTML = d.corridors.map((c) =>
    `<tr style="cursor:pointer;" onclick="inspectRoadFromMap('${c.edge_id}', '${c.road}', '${c.state}', 'trunk', ${c.km}, ${c.risk}, '${c.status}', '${(c.reason || '').replace(/'/g, "\\'")}', ${c.bridge ? 1 : 0}, ${c.ford ? 1 : 0})">
      <td title="${c.edge_id}"><b>${c.road}</b>${c.bridge ? " 🌉" : ""}${c.ford ? " 🌊" : ""}</td>
      <td>${c.state}</td>
      <td class="mono">${c.km}</td>
      <td class="mono">${(c.risk * 100).toFixed(0)}%</td>
      <td style="font-size:11px;line-height:1.35;color:${c.risk >= 0.7 ? '#fca5a5' : c.risk >= 0.35 ? '#fde68a' : 'var(--muted)'}">${c.reason || "Terrain & Weather Disruption Risk"}</td>
      <td><span class="badge b-${c.status === "open" ? "open" : c.status === "at_risk" ? "risk" : "blocked"}">${c.status}</span></td>
    </tr>`
  ).join("");
}

async function loadAlerts() {
  try {
    const res = await fetch(`${API}/api/alerts?limit=25`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (d && window.nerDB) window.nerDB.setCache("alerts", d);
    renderAlerts(d);
  } catch (err) {
    if (window.nerDB) {
      const cached = await window.nerDB.getCache("alerts");
      if (cached) { renderAlerts(cached); return; }
    }
    console.warn("Alerts loading bypassed:", err);
  }
}

function renderAlerts(d) {
  const el = $("#alerts");
  if (!el) return;
  if (!d.alerts || !d.alerts.length) { el.innerHTML = '<div class="hint">No active alerts at this time.</div>'; return; }
  el.innerHTML = d.alerts.map((a) =>
    `<div class="alert">
       <div class="t">${a.title} <span class="badge ${SEV[a.severity] || "b-risk"}">${a.severity}</span></div>
       <div class="m">${a.body}</div>
       <div class="ts">${a.created_at.replace("T", " ")}</div>
     </div>`).join("");
}

async function loadShipments() {
  try {
    const res = await fetch(`${API}/api/shipments`);
    if (!res.ok) return;
    const d = await res.json();
    const tb = $("#tblShips tbody");
    if (!tb) return;
    if (!d.count || !d.shipments || !d.shipments.length) {
      tb.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">No consignments registered.</td></tr>';
      return;
    }
    tb.innerHTML = d.shipments.map((s) =>
      `<tr>
        <td><b>${s.id}</b></td>
        <td>${getCommodityLabel(s.commodity)}</td>
        <td>${s.destination || "—"}</td>
        <td class="mono">${s.eta_minutes ? Math.round(s.eta_minutes) + " min" : "—"}</td>
        <td><span class="badge b-${s.status === "blocked" ? "blocked" : s.status === "delivered" ? "open" : "risk"}">${s.status}</span></td>
        <td><button type="button" class="primary" style="padding:2px 8px;font-size:11px;" onclick="trackShipmentFromTable('${s.id}', '${s.commodity}', '${s.origin || ''}', '${s.destination || ''}', '${s.vehicle_no || ''}', '${s.driver_name || ''}')">📍 Track</button></td>
      </tr>`
    ).join("");
  } catch (err) {
    console.warn("Shipments loading bypassed:", err);
  }
}

function getCommodityLabel(c) {
  if (c === "medicine") return "💊 Medicines";
  if (c === "food") return "🌾 Food Grain";
  if (c === "fuel") return "⛽ Fuel/LPG";
  if (c === "relief") return "📦 Relief Kits";
  return "🚚 " + (c || "Cargo");
}

/* ------------------------------------------------------------- road inspector ---- */
function inspectRoad(p) {
  const card = $("#cardRoadInspector");
  if (!card) return;
  card.style.display = "block";

  const roadName = p.road || p.ref || p.name || "Road Segment";
  const stateCode = p.state || "NER";
  const highway = p.highway || "trunk";
  const km = p.km || 0;
  const risk = p.risk || 0;
  const status = p.status || (risk >= 0.7 ? "blocked" : risk >= 0.35 ? "at_risk" : "open");
  const reason = p.reason || "Stable corridor with standard terrain conditions";
  const bridge = p.bridge ? "🌉 Bridge Monitored" : p.ford ? "🌊 River Ford / Flood Zone" : "Standard Highway";

  $("#inspRoadName").textContent = roadName;
  $("#inspSub").textContent = `State: ${stateCode} · Type: ${highway.toUpperCase()} Corridor`;
  $("#inspLength").textContent = `${km} km`;
  $("#inspTime").textContent = `${Math.max(1, Math.round(km * 1.3))} min`;
  $("#inspAssets").textContent = bridge;
  $("#inspHazard").textContent = reason;

  const rBadge = $("#inspRiskBadge");
  rBadge.className = `badge b-${status === "open" ? "open" : status === "at_risk" ? "risk" : "blocked"}`;
  rBadge.textContent = `${(risk * 100).toFixed(1)}% Risk · ${status.toUpperCase()}`;

  // Action buttons
  $("#btnSetAsOrigin").onclick = () => {
    if (p.lat && p.lon) {
      clearRoute();
      routeMode = true;
      origin = [p.lat, p.lon];
      currentOriginName = roadName;
      originMarker = L.circleMarker(origin, { color: "#4da3ff", fillColor: "#4da3ff", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Origin: " + roadName).openPopup();
      map.flyTo(origin, 9);
      $("#routes").innerHTML = `<div class="hint">📍 Origin set to <b>${roadName}</b>. Now click destination on map.</div>`;
    } else {
      setStatus(`Selected ${roadName} as corridor reference.`);
    }
  };

  $("#btnSetAsDest").onclick = () => {
    if (p.lat && p.lon) {
      if (!origin) {
        setStatus("Please set Origin point first.");
        return;
      }
      destination = [p.lat, p.lon];
      currentDestName = roadName;
      destMarker = L.circleMarker(destination, { color: "#ff4d4f", fillColor: "#ff4d4f", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Destination: " + roadName).openPopup();
      planRoute();
    } else {
      setStatus(`Selected ${roadName} as destination reference.`);
    }
  };
}

window.inspectRoadFromMap = function(id, road, state, highway, km, risk, status, reason, bridge, ford) {
  inspectRoad({
    id, road, state, highway, km: +km, risk: +risk, status, reason,
    bridge: Boolean(bridge), ford: Boolean(ford)
  });
};

window.closeRoadInspector = function() {
  const card = $("#cardRoadInspector");
  if (card) card.style.display = "none";
};

/* ------------------------------------------------------------- route planning ---- */
async function planRoute(isRetry = false) {
  if (!origin || !destination) return;
  $("#routes").innerHTML = `
    <div class="hint" style="display:flex;align-items:center;gap:10px;color:var(--accent);">
      <span class="pill-dot" style="background:var(--accent);animation:pulse 1s infinite;"></span>
      <span>Calculating AI risk-adjusted route &amp; alternates&hellip;</span>
    </div>`;

  try {
    const body = {
      lat1: origin[0], lon1: origin[1], lat2: destination[0], lon2: destination[1],
      alternatives: 3, use_live_forecast: true,
    };
    
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 20000);

    const r = await fetch(`${API}/api/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!r.ok) {
      if (!isRetry) {
        $("#routes").innerHTML = `
          <div class="hint" style="color:#f5a623;">
            ⏳ Server is warming up the road network. Retrying calculation in 2s&hellip;
          </div>`;
        setTimeout(() => planRoute(true), 2000);
        return;
      }

      let errDetail = "";
      try {
        const errJson = await r.json();
        errDetail = errJson.error || errJson.detail || errJson.diagnostics || JSON.stringify(errJson);
      } catch (e) {
        errDetail = await r.text();
      }
      
      $("#routes").innerHTML = `
        <div class="hint" style="color:#ff4d4f;line-height:1.6;">
          <b>⚠️ Route Calculation Notice:</b><br>
          <span style="font-size:12px;color:var(--text);">${errDetail || "Server was temporarily unable to find a connected path."}</span>
          <div style="margin-top:10px;">
            <button type="button" class="primary" style="padding:4px 12px;font-size:11px;" onclick="planRoute(true)">🔄 Retry Route</button>
          </div>
        </div>`;
      return;
    }

    const d = await r.json();
    activeRoutes = d.routes || [];
    selectedRouteIndex = 0;

    if (!activeRoutes.length) {
      $("#routes").innerHTML = `
        <div class="hint" style="color:#f5a623;line-height:1.6;">
          <b>⚠️ No connected road corridor found</b> between selected points.<br>
          <span style="font-size:11px;color:var(--muted);">${d.diagnostics || "Try tapping closer to major state highways (NH-27, NH-6, NH-10)."}</span>
          <div style="margin-top:10px;">
            <button type="button" style="padding:4px 12px;font-size:11px;" onclick="planRoute(true)">🔄 Re-try Calculation</button>
          </div>
        </div>`;
      return;
    }

    renderRoutesOnMap();
    renderRoutesList(d);
  } catch (err) {
    if (!isRetry) {
      $("#routes").innerHTML = `<div class="hint" style="color:#f5a623">Server is calculating, retrying in 2s&hellip;</div>`;
      setTimeout(() => planRoute(true), 2000);
      return;
    }
    $("#routes").innerHTML = `
      <div class="hint" style="color:#ff4d4f;line-height:1.6;">
        <b>Connection Error:</b> ${err.message}<br>
        <button type="button" class="primary" style="margin-top:8px;padding:4px 12px;font-size:11px;" onclick="planRoute(true)">🔄 Retry Route</button>
      </div>`;
  }
}

function renderRoutesOnMap() {
  routeLayers.forEach((l) => map.removeLayer(l));
  routeLayers = [];

  activeRoutes.forEach((rt, i) => {
    const isSelected = i === selectedRouteIndex;
    const colour = i === 0 ? "#38bdf8" : i === 1 ? "#facc15" : "#c084fc";
    
    const line = L.polyline(rt.polyline, {
      color: colour,
      weight: isSelected ? 6 : 3.5,
      opacity: isSelected ? 1.0 : 0.55,
      dashArray: isSelected ? null : "6 6",
      lineCap: "round",
    }).addTo(map);

    line.on("click", () => {
      selectRoute(i);
    });

    line.bindPopup(
      `<b>Route ${rt.rank} (${i === 0 ? 'Primary' : 'Alternate'})</b><br>` +
      `${rt.distance_km} km · <b>${Math.round(rt.risk_adjusted_minutes)} min</b> risk-adjusted<br>` +
      `Max segment risk: ${(rt.max_segment_risk * 100).toFixed(0)}% (${rt.risk_level})<br>` +
      `<div style="margin-top:8px;"><button type="button" class="primary" style="padding:4px 10px;font-size:11px;" onclick="selectRoute(${i})">✓ Select This Route</button></div>`
    );
    routeLayers.push(line);
  });

  if (activeRoutes.length && activeRoutes[0].polyline && activeRoutes[0].polyline.length) {
    const b = L.latLngBounds(activeRoutes.flatMap((r) => r.polyline));
    map.fitBounds(b, { padding: [40, 40] });
  }
}

function renderRoutesList(d) {
  const routesHtml = activeRoutes.map((rt, i) => {
    const isSelected = i === selectedRouteIndex;
    return `
      <div class="route-card ${isSelected ? 'selected' : ''}" onclick="selectRoute(${i})">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
          <div style="font-weight:700; font-size:13px; display:flex; align-items:center; gap:6px;">
            <span>Route ${rt.rank} ${i === 0 ? '(Primary Recommended)' : '(Alternate Bypass)'}</span>
            ${isSelected ? '<span class="route-badge-sel">✓ Selected</span>' : ''}
          </div>
          <span class="badge b-${rt.risk_level === "clear" ? "open" : rt.risk_level === "at_risk" ? "risk" : "blocked"}">${rt.risk_level}</span>
        </div>
        <div class="mono" style="font-size:12px; color:#fff; margin-bottom:2px;">
          <b>${rt.distance_km} km</b> &middot; Free-flow ${Math.round(rt.free_flow_minutes)} min &middot; Risk-Adjusted <b>${Math.round(rt.risk_adjusted_minutes)} min</b>
        </div>
        <div style="color:var(--muted); font-size:11px; margin-bottom:4px;">
          Max corridor risk: ${(rt.max_segment_risk * 100).toFixed(0)}%
          ${rt.passes_blocked_segment ? " &middot; <b style='color:#ff4d4f'>⚠️ Blocked segment ahead</b>" : ""}
        </div>
        <div style="color:#94a3b8; font-size:11px; line-height:1.35;">
          🛣️ <b>Segments:</b> ${rt.segments.slice(0, 5).map((s) => s.road).join(" → ")}${rt.segments.length > 5 ? " …" : ""}
        </div>
      </div>
    `;
  }).join("");

  const selRoute = activeRoutes[selectedRouteIndex] || activeRoutes[0];
  const dispatchButtonHtml = `
    <div style="padding:14px; background:rgba(22,32,58,0.5); border-top:1px solid var(--line);">
      <button type="button" class="primary" style="width:100%; padding:12px 14px; font-size:14px; font-weight:700; display:flex; align-items:center; justify-content:center; gap:8px;" onclick="openDispatchModal()">
        🚀 Confirm &amp; Dispatch Consignment
      </button>
      <div style="font-size:11px; color:var(--muted); text-align:center; margin-top:6px;">
        Selected: Route ${selRoute ? selRoute.rank : 1} (${selRoute ? selRoute.distance_km : 0} km &middot; ${selRoute ? Math.round(selRoute.risk_adjusted_minutes) : 0} min)
      </div>
    </div>
  `;

  $("#routes").innerHTML = routesHtml + dispatchButtonHtml +
    `<div class="hint" style="font-size:11px;">Computed in ${d.computed_in_ms} ms &middot; ${d.model_note}</div>`;
}

window.selectRoute = function(idx) {
  if (idx < 0 || idx >= activeRoutes.length) return;
  selectedRouteIndex = idx;
  renderRoutesOnMap();
  renderRoutesList({ computed_in_ms: 12, model_note: "A* with Yen K-alternates" });
  
  const selRoute = activeRoutes[idx];
  if (selRoute && selRoute.segments && selRoute.segments.length) {
    inspectRoad({
      road: `Route ${selRoute.rank} Corridor (${selRoute.segments[0].road} → ${selRoute.segments[selRoute.segments.length - 1].road})`,
      state: "NER",
      highway: selRoute.segments[0].highway || "primary",
      km: selRoute.distance_km,
      risk: selRoute.max_segment_risk,
      status: selRoute.risk_level === "clear" ? "open" : selRoute.risk_level,
      reason: selRoute.blockage_reason || "Corridor evaluated by AI Risk Engine",
      bridge: selRoute.segments.some(s => s.is_bridge),
      ford: selRoute.segments.some(s => s.is_ford),
    });
  }
};

// Quick Route Presets
window.setQuickRoute = function(lat1, lon1, lat2, lon2, name) {
  clearRoute();
  routeMode = true;
  $("#btnLayer").textContent = "📍 Route Mode: Active";
  $("#btnLayer").classList.add("primary");
  origin = [lat1, lon1];
  destination = [lat2, lon2];
  
  const parts = name.split("→");
  currentOriginName = parts[0] ? parts[0].trim() : "Origin";
  currentDestName = parts[1] ? parts[1].trim() : "Destination";

  originMarker = L.circleMarker(origin, { color: "#4da3ff", fillColor: "#4da3ff", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Origin: " + currentOriginName).openPopup();
  destMarker = L.circleMarker(destination, { color: "#ff4d4f", fillColor: "#ff4d4f", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Destination: " + currentDestName);
  planRoute();
};

/* ---------------------------------------------------- dispatch confirmation modal ---- */
window.openDispatchModal = function() {
  const modal = $("#modalDispatch");
  if (!modal) return;
  const selRoute = activeRoutes[selectedRouteIndex] || activeRoutes[0];
  
  const summary = selRoute
    ? `${currentOriginName} → ${currentDestName} &middot; ${selRoute.distance_km} km &middot; ${Math.round(selRoute.risk_adjusted_minutes)} min (${selRoute.risk_level.toUpperCase()} SAFETY)`
    : `${currentOriginName} → ${currentDestName} &middot; 84.0 km &middot; 95 min`;

  $("#modalRouteSummary").innerHTML = summary;
  $("#mCid").value = `NER-${Date.now().toString().slice(-6)}`;
  $("#mOrigin").value = currentOriginName;
  $("#mDest").value = currentDestName;

  modal.classList.add("active");
};

window.openDispatchModalDirect = function() {
  if (!origin || !destination || !activeRoutes.length) {
    setQuickRoute(26.1820, 91.7480, 25.5788, 91.8933, "Guwahati → Shillong");
    setTimeout(() => openDispatchModal(), 500);
  } else {
    openDispatchModal();
  }
};

window.closeDispatchModal = function() {
  const modal = $("#modalDispatch");
  if (modal) modal.classList.remove("active");
};

window.confirmAndStartTracking = async function() {
  const cid = $("#mCid").value || `NER-${Date.now().toString().slice(-4)}`;
  const cargo = $("#mCargo").value || "medicine";
  const orig = $("#mOrigin").value || currentOriginName;
  const dest = $("#mDest").value || currentDestName;
  const driver = $("#mDriver").value || "Rajesh Bora";
  const phone = $("#mPhone").value || "+91 98640 12345";
  const veh = $("#mVeh").value || "AS-01-AX-9921";

  const selRoute = activeRoutes[selectedRouteIndex] || activeRoutes[0];
  const eta = selRoute ? selRoute.risk_adjusted_minutes : 90.0;
  const dLat = destination ? destination[0] : 25.5788;
  const dLon = destination ? destination[1] : 91.8933;

  // 1. Post to backend
  try {
    await fetch(`${API}/api/shipments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: cid,
        commodity: cargo,
        origin: orig,
        destination: dest,
        dest_lat: dLat,
        dest_lon: dLon,
        vehicle_no: veh,
        driver_name: driver,
        driver_phone: phone,
        status: "in_transit",
        priority: 1,
        eta_minutes: eta,
      }),
    });
    loadShipments();
  } catch (err) {
    console.warn("Could not sync shipment to server:", err);
  }

  // 2. Close modal
  closeDispatchModal();

  // 3. Launch live Zomato driver simulation
  startLiveDriverTracking({
    id: cid,
    commodity: cargo,
    origin: orig,
    destination: dest,
    driver: driver,
    phone: phone,
    vehicle: veh,
    route: selRoute || {
      polyline: [origin || [26.1820, 91.7480], destination || [25.5788, 91.8933]],
      distance_km: 98.4,
      risk_adjusted_minutes: 110,
      segments: [{ road: "NH-6 Meghalaya Gateway", risk: 0.12 }]
    }
  });
};

/* ---------------------------------------------------- live zomato driver tracking engine ---- */
function interpolatePolyline(rawCoords, density = 4) {
  if (!rawCoords || rawCoords.length < 2) return rawCoords || [];
  const dense = [];
  for (let i = 0; i < rawCoords.length - 1; i++) {
    const p1 = rawCoords[i];
    const p2 = rawCoords[i + 1];
    dense.push(p1);
    for (let step = 1; step < density; step++) {
      const frac = step / density;
      const lat = p1[0] + (p2[0] - p1[0]) * frac;
      const lon = p1[1] + (p2[1] - p1[1]) * frac;
      dense.push([lat, lon]);
    }
  }
  dense.push(rawCoords[rawCoords.length - 1]);
  return dense;
}

function calculateBearing(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * (Math.PI / 180);
  const y = Math.sin(dLon) * Math.cos(lat2 * (Math.PI / 180));
  const x = Math.cos(lat1 * (Math.PI / 180)) * Math.sin(lat2 * (Math.PI / 180)) -
            Math.sin(lat1 * (Math.PI / 180)) * Math.cos(lat2 * (Math.PI / 180)) * Math.cos(dLon);
  const brng = Math.atan2(y, x) * (180 / Math.PI);
  return (brng + 360) % 360;
}

function startLiveDriverTracking(mission) {
  // Clear any existing simulation
  stopDriverSimulation();

  driverSim.active = true;
  driverSim.paused = false;
  driverSim.stepIdx = 0;
  driverSim.mission = mission;
  driverSim.speedMultiplier = 1;
  driverSim.followDriver = true;
  driverSim.totalDistanceKm = mission.route.distance_km || 80.0;
  driverSim.totalMinutes = mission.route.risk_adjusted_minutes || 90.0;

  // Build dense coordinate path for smooth animation
  driverSim.densePath = interpolatePolyline(mission.route.polyline, 6);
  if (driverSim.densePath.length < 2) {
    driverSim.densePath = [
      [26.1820, 91.7480], [26.0421, 91.8105], [25.8820, 91.8540],
      [25.7140, 91.8820], [25.5788, 91.8933]
    ];
  }

  // Setup dual polyline layers (Traversed = Neon glow; Remaining = Dashed sky blue)
  driverSim.traversedLayer = L.polyline([], {
    color: "#10b981", weight: 5, opacity: 0.95, lineCap: "round"
  }).addTo(map);

  driverSim.remainingLayer = L.polyline(driverSim.densePath, {
    color: "#38bdf8", weight: 4, opacity: 0.85, dashArray: "6 6"
  }).addTo(map);

  // Setup Custom Leaflet HTML Marker with rotating icon and radar halo
  const truckIcon = L.divIcon({
    className: "custom-driver-vehicle-marker",
    html: `
      <div class="vehicle-marker-container">
        <div class="vehicle-pulse-halo"></div>
        <div class="vehicle-icon-card" id="vehicleTruckIcon">🚚</div>
      </div>
    `,
    iconSize: [44, 44],
    iconAnchor: [22, 22],
  });

  const startPt = driverSim.densePath[0];
  driverSim.driverMarker = L.marker(startPt, { icon: truckIcon, zIndexOffset: 1000 }).addTo(map);

  // Show & Update Floating HUD
  const hud = $("#driverLiveHud");
  if (hud) {
    hud.classList.remove("hidden");
    $("#hudDriverName").textContent = mission.driver || "Rajesh Bora";
    $("#hudVehCargo").textContent = `${mission.vehicle || 'AS-01-AX-9921'} · ${getCommodityLabel(mission.commodity)}`;
    $("#hudOriginDest").textContent = `${mission.origin || 'Guwahati'} → ${mission.destination || 'Shillong'}`;
    $("#hudStatusBadge").className = "hud-status-badge";
    $("#hudStatusText").textContent = "LIVE EN ROUTE";
    $("#hudAlertBanner").style.display = "none";
    $("#btnPauseResume").textContent = "⏸️ Pause";
    $("#btnSpeedMultiplier").textContent = "⚡ Speed: 1x";
    $("#btnFollowDriver").classList.add("active");
  }

  map.flyTo(startPt, 11, { duration: 1 });

  // Run simulation frame tick
  runDriverSimTick();
}

function runDriverSimTick() {
  if (!driverSim.active) return;
  if (driverSim.timerId) clearInterval(driverSim.timerId);

  const intervalMs = Math.max(30, Math.round(180 / driverSim.speedMultiplier));
  driverSim.timerId = setInterval(() => {
    if (driverSim.paused) return;

    const totalSteps = driverSim.densePath.length;
    if (driverSim.stepIdx >= totalSteps - 1) {
      completeConsignmentDelivery();
      return;
    }

    driverSim.stepIdx++;
    const currPt = driverSim.densePath[driverSim.stepIdx];
    const prevPt = driverSim.densePath[Math.max(0, driverSim.stepIdx - 1)];
    const progressFrac = driverSim.stepIdx / (totalSteps - 1);

    // 1. Move Marker
    if (driverSim.driverMarker) {
      driverSim.driverMarker.setLatLng(currPt);
    }

    // 2. Rotate Vehicle Icon to Direction
    const bearing = calculateBearing(prevPt[0], prevPt[1], currPt[0], currPt[1]);
    const iconEl = $("#vehicleTruckIcon");
    if (iconEl) {
      iconEl.style.transform = `rotate(${Math.round(bearing)}deg)`;
    }

    // 3. Update Polyline Trails
    if (driverSim.traversedLayer && driverSim.remainingLayer) {
      driverSim.traversedLayer.setLatLngs(driverSim.densePath.slice(0, driverSim.stepIdx + 1));
      driverSim.remainingLayer.setLatLngs(driverSim.densePath.slice(driverSim.stepIdx));
    }

    // 4. Update HUD Telemetry
    updateDriverHudTelemetry(progressFrac, currPt);

    // 5. Follow Driver Camera
    if (driverSim.followDriver && driverSim.stepIdx % 3 === 0) {
      map.panTo(currPt, { animate: true, duration: 0.2 });
    }

    // 6. Periodic GPS Ping to Server
    if (driverSim.stepIdx % 20 === 0 && driverSim.mission) {
      pingShipmentGps(driverSim.mission.id, currPt);
    }
  }, intervalMs);
}

function updateDriverHudTelemetry(progressFrac, currPt) {
  const pct = Math.min(100, Math.round(progressFrac * 100));
  $("#hudProgressPct").textContent = `${pct}% Completed`;
  $("#hudProgressFill").style.width = `${pct}%`;

  const remainingDist = ((1 - progressFrac) * driverSim.totalDistanceKm).toFixed(1);
  const remainingEta = Math.max(0, Math.round((1 - progressFrac) * driverSim.totalMinutes));
  const simSpeed = Math.round(48 + Math.sin(driverSim.stepIdx * 0.15) * 8);

  $("#hudDistVal").textContent = `${remainingDist} km`;
  $("#hudEtaVal").textContent = `${remainingEta} min`;
  $("#hudSpeedVal").textContent = `${simSpeed} km/h`;

  // Dynamic checkpoint name
  const segments = (driverSim.mission && driverSim.mission.route && driverSim.mission.route.segments) || [];
  if (segments.length) {
    const segIdx = Math.min(segments.length - 1, Math.floor(progressFrac * segments.length));
    const curSeg = segments[segIdx];
    $("#hudCheckpointText").innerHTML = `Current: <b>${curSeg.road || 'National Corridor'}</b> (${curSeg.km || 5} km · ${(curSeg.risk * 100 || 12).toFixed(0)}% Risk)`;
  }
}

async function pingShipmentGps(shipmentId, pt) {
  try {
    await fetch(`${API}/api/shipments/ping`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        shipment_id: shipmentId,
        lat: pt[0],
        lon: pt[1],
        speed_kmph: 52,
        heading: 180,
        battery: 96,
        at: new Date().toISOString()
      }),
    });
  } catch (e) {}
}

window.toggleFollowDriver = function() {
  driverSim.followDriver = !driverSim.followDriver;
  const btn = $("#btnFollowDriver");
  if (btn) btn.classList.toggle("active", driverSim.followDriver);
};

window.toggleSimPause = function() {
  driverSim.paused = !driverSim.paused;
  const btn = $("#btnPauseResume");
  if (btn) btn.textContent = driverSim.paused ? "▶️ Resume" : "⏸️ Pause";
};

window.cycleSpeedMultiplier = function() {
  const speeds = [1, 2, 4, 8];
  const nextIdx = (speeds.indexOf(driverSim.speedMultiplier) + 1) % speeds.length;
  driverSim.speedMultiplier = speeds[nextIdx];
  const btn = $("#btnSpeedMultiplier");
  if (btn) btn.textContent = `⚡ Speed: ${driverSim.speedMultiplier}x`;
  runDriverSimTick();
};

window.triggerSimHazardAlert = function() {
  const alertBanner = $("#hudAlertBanner");
  const badge = $("#hudStatusBadge");
  const badgeText = $("#hudStatusText");

  if (alertBanner) {
    alertBanner.style.display = "flex";
    alertBanner.innerHTML = `
      <span>🚨</span>
      <span><b>ZOMATO LIVE REROUTE ADVICE:</b> Active landslide at km 48 on NH-6. Switching dynamically to <b>SH-14 Mawlyngkhung Bypass</b>. Saves <b>48 min</b> road blockage!</span>
    `;
  }

  if (badge && badgeText) {
    badge.className = "hud-status-badge diverted";
    badgeText.textContent = "⚠️ DIVERTTED VIA BYPASS";
  }

  // Play hazard tone
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    osc.frequency.setValueAtTime(740, ctx.currentTime);
    osc.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.2);
  } catch (e) {}
};

window.completeConsignmentDelivery = function() {
  if (driverSim.timerId) clearInterval(driverSim.timerId);
  driverSim.paused = true;

  const badge = $("#hudStatusBadge");
  const badgeText = $("#hudStatusText");
  if (badge && badgeText) {
    badge.className = "hud-status-badge delivered";
    badgeText.textContent = "🎉 DELIVERED AT DESTINATION";
  }

  $("#hudProgressPct").textContent = "100% Delivered";
  $("#hudProgressFill").style.width = "100%";
  $("#hudDistVal").textContent = "0 km";
  $("#hudEtaVal").textContent = "0 min";
  $("#hudSpeedVal").textContent = "0 km/h";
  $("#hudCheckpointText").innerHTML = "🎉 <b>Mission Accomplished:</b> Consignment safely handed over at destination depot.";

  if (driverSim.mission) {
    fetch(`${API}/api/shipments/${driverSim.mission.id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "delivered", eta_minutes: 0 }),
    }).then(() => loadShipments()).catch(() => {});
  }
};

window.closeLiveTracker = function() {
  stopDriverSimulation();
  const hud = $("#driverLiveHud");
  if (hud) hud.classList.add("hidden");
};

function stopDriverSimulation() {
  if (driverSim.timerId) clearInterval(driverSim.timerId);
  driverSim.active = false;
  if (driverSim.driverMarker) { map.removeLayer(driverSim.driverMarker); driverSim.driverMarker = null; }
  if (driverSim.traversedLayer) { map.removeLayer(driverSim.traversedLayer); driverSim.traversedLayer = null; }
  if (driverSim.remainingLayer) { map.removeLayer(driverSim.remainingLayer); driverSim.remainingLayer = null; }
}

window.trackShipmentFromTable = function(id, commodity, originStr, destStr, veh, driver) {
  // Preset demo corridor coordinates
  setQuickRoute(26.1820, 91.7480, 25.5788, 91.8933, `${originStr || 'Guwahati'} → ${destStr || 'Shillong'}`);
  setTimeout(() => {
    startLiveDriverTracking({
      id: id,
      commodity: commodity,
      origin: originStr || "Guwahati Central Depot",
      destination: destStr || "Shillong Civil Hospital",
      driver: driver || "Rajesh Bora",
      phone: "+91 98640 12345",
      vehicle: veh || "AS-01-AX-9921",
      route: activeRoutes[0] || {
        polyline: [[26.1820, 91.7480], [26.0421, 91.8105], [25.8820, 91.8540], [25.7140, 91.8820], [25.5788, 91.8933]],
        distance_km: 98.4,
        risk_adjusted_minutes: 110,
        segments: [{ road: "NH-6 Shillong Highway", risk: 0.12, km: 98.4 }]
      }
    });
  }, 400);
};

/* ------------------------------------------------------- translation ---- */
async function translateCorridor() {
  const lang = $("#selLang").value;
  const rows = $$("#tblCorridors tbody tr");
  if (!rows.length) return;
  const road = rows[0].cells[0].textContent.trim();
  const st = rows[0].cells[1].textContent.trim();
  const d = await (await fetch(
    `${API}/api/alerts/translate?kind=high_risk_corridor&lang=${lang}&road=${encodeURIComponent(road)}&state=${st}`
  )).json();
  setStatus(`${d.language.toUpperCase()}: ${d.title} — ${d.body}`);
}

/* -------------------------------------------------------------- ui ---- */
function setStatus(t) { $("#status").textContent = t; }

async function refreshAll() {
  const b = $("#btnRefresh");
  b.disabled = true; b.textContent = "Re-scoring…";
  try {
    await (await fetch(`${API}/api/risk/refresh`)).json();
  } catch (e) { setStatus("Refresh failed: " + e.message); }
  await Promise.all([loadHealth(), loadEdges(), loadDistricts(), loadCorridors(),
                     loadAlerts(), loadShipments()]);
  b.disabled = false; b.textContent = "⚡ Refresh Live Risk";
}

const STATE_CENTERS = {
  "AS": [26.2006, 92.9376, 7],
  "ML": [25.5788, 91.8933, 8],
  "AR": [27.0844, 93.6053, 7],
  "MN": [24.8170, 93.9368, 8],
  "MZ": [23.7271, 92.7176, 8],
  "NL": [25.6701, 94.1077, 8],
  "SK": [27.3389, 88.6065, 8],
  "TR": [23.8315, 91.2868, 8]
};

const HIGHWAY_PRESETS = {
  "NH-6": [26.1820, 91.7480, 25.5788, 91.8933, "Guwahati → Shillong (NH-6)"],
  "NH-306": [24.8333, 92.7789, 23.7271, 92.7176, "Silchar → Aizawl (NH-306)"],
  "NH-29": [25.9064, 93.7273, 25.6701, 94.1077, "Dimapur → Kohima (NH-29)"],
  "NH-27": [26.1820, 91.7480, 26.7509, 94.2037, "Guwahati → Jorhat (NH-27)"],
  "NH-10": [27.3389, 88.6065, 27.5089, 88.5289, "Gangtok → Mangan (NH-10)"],
  "NH-715": [26.7509, 94.2037, 27.0844, 93.6053, "Jorhat → Itanagar (NH-715)"],
  "NH-208": [24.1700, 92.0300, 23.8315, 91.2868, "Kumarghat → Agartala (NH-208)"],
};

function bindUi() {
  routeMode = true;
  $("#btnRefresh").onclick = refreshAll;
  $("#btnClearRoute").onclick = () => { clearRoute(); closeLiveTracker(); };
  
  $("#selState").onchange = (e) => {
    state.stateFilter = e.target.value;
    if (STATE_CENTERS[state.stateFilter]) {
      const [lat, lon, zoom] = STATE_CENTERS[state.stateFilter];
      map.flyTo([lat, lon], zoom, { duration: 1.2 });
    } else {
      map.flyTo([26.0, 92.5], 7, { duration: 1.2 });
    }
    loadEdges();
  };

  $("#selQuickRoad").onchange = (e) => {
    const val = e.target.value;
    if (val && HIGHWAY_PRESETS[val]) {
      const [lat1, lon1, lat2, lon2, name] = HIGHWAY_PRESETS[val];
      setQuickRoute(lat1, lon1, lat2, lon2, name);
    }
  };

  $("#rngRisk").oninput = (e) => { state.risk = +e.target.value; };
  $("#rngRisk").onchange = loadEdges;
  $("#selLang").onchange = translateCorridor;
  
  $("#btnLayer").onclick = (e) => {
    routeMode = !routeMode;
    e.target.textContent = "📍 Route Mode: " + (routeMode ? "Active" : "Off");
    e.target.classList.toggle("primary", routeMode);
    $("#mapHint").innerHTML = routeMode
      ? "💡 <b>Route mode active</b> — Click origin on the map, then destination to calculate safest route & alternates."
      : "Turn on <b>Route mode</b>, then click two points on the map.";
  };
}

(async function main() {
  initMap();
  bindUi();
  await loadHealth();
  await Promise.all([loadEdges(), loadDistricts(), loadCorridors(), loadAlerts(), loadShipments()]);
  setInterval(() => { loadAlerts(); loadShipments(); }, 30000);
})();
