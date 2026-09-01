/*
 * Command-centre dashboard logic.
 *
 * Everything talks to the same FastAPI origin (relative URLs), so the app works behind a
 * reverse proxy, on a laptop, or in a container without a single hard-coded host.
 */

const API = "";                                   // same origin
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const COLOUR = { open: "#2ecc71", at_risk: "#f5a623", blocked: "#ff4d4f" };
const SEV = { low: "b-open", moderate: "b-risk", high: "b-blocked", critical: "b-critical" };

let map, edgeLayer, routeLayers = [], originMarker = null, destMarker = null;
let routeMode = false, origin = null, destination = null;
let state = { risk: 0, stateFilter: "" };

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
    originMarker = L.circleMarker(pt, { color: "#4da3ff", fillColor: "#4da3ff",
      fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("📍 Origin point set. Now click Destination.").openPopup();
    $("#routes").innerHTML = '<div class="hint">📍 <b>Origin set.</b> Now click a destination on the map to calculate best route &amp; alternates.</div>';
  } else if (!destination) {
    destination = pt;
    destMarker = L.circleMarker(pt, { color: "#ff4d4f", fillColor: "#ff4d4f",
      fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("🏁 Destination point").openPopup();
    planRoute();
  }
}

function clearRoute() {
  routeLayers.forEach((l) => map.removeLayer(l));
  routeLayers = [];
  [originMarker, destMarker].forEach((m) => m && map.removeLayer(m));
  originMarker = destMarker = null;
  origin = destination = null;
  $("#routes").innerHTML = '<div class="hint">No route planned yet.</div>';
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
      weight: f.properties.status === "open" ? 1.1 : 3,
      opacity: f.properties.status === "open" ? 0.5 : 0.95,
    }),
    onEachFeature: (f, layer) => {
      const p = f.properties;
      layer.bindPopup(
        `<b>${p.road}</b><br>class: ${p.highway} · ${p.km} km<br>` +
        `state: ${p.state}<br>risk: <b>${(p.risk * 100).toFixed(1)}%</b> (${p.status})` +
        (p.bridge ? "<br>⚠ bridge" : "") + (p.ford ? "<br>⚠ river ford" : ""));
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
    `<tr><td title="${c.edge_id}">${c.road}${c.bridge ? " ⚠" : ""}</td><td>${c.state}</td>
     <td class="mono">${c.km}</td><td class="mono">${(c.risk * 100).toFixed(0)}%</td>
     <td><span class="badge b-${c.status === "open" ? "open" : c.status === "at_risk" ? "risk" : "blocked"}">${c.status}</span></td></tr>`
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
    if (!d.count || !d.shipments) {
      tb.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">No consignments registered.</td></tr>';
      return;
    }
    tb.innerHTML = d.shipments.map((s) =>
      `<tr><td>${s.id}</td><td>${s.commodity}</td><td>${s.destination || "—"}</td>
       <td class="mono">${s.eta_minutes ? Math.round(s.eta_minutes) + " min" : "—"}</td>
       <td><span class="badge b-${s.status === "blocked" ? "blocked" : s.status === "delivered" ? "open" : "risk"}">${s.status}</span></td></tr>`
    ).join("");
  } catch (err) {
    console.warn("Shipments loading bypassed:", err);
  }
}

/* ------------------------------------------------------------- route ---- */
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
        // Auto-retry once in case the server was waking up or reloading
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

    routeLayers.forEach((l) => map.removeLayer(l));
    routeLayers = [];

    if (!d.routes || !d.routes.length) {
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

    d.routes.forEach((rt, i) => {
      const colour = i === 0 ? "#7ad7ff" : i === 1 ? "#ffd166" : "#b39ddb";
      const line = L.polyline(rt.polyline, {
        color: colour,
        weight: i === 0 ? 5 : 3.5,
        opacity: 0.95,
        dashArray: i === 0 ? null : "6 6"
      }).addTo(map);

      line.bindPopup(`<b>Route ${rt.rank}</b><br>${rt.distance_km} km · ` +
        `${Math.round(rt.risk_adjusted_minutes)} min risk-adjusted<br>` +
        `max segment risk ${(rt.max_segment_risk * 100).toFixed(0)}% (${rt.risk_level})` +
        (rt.passes_blocked_segment ? "<br><b style='color:#ff4d4f'>passes a blocked segment</b>" : ""));
      routeLayers.push(line);
    });

    if (d.routes.length && d.routes[0].polyline && d.routes[0].polyline.length) {
      const b = L.latLngBounds(d.routes.flatMap((r) => r.polyline));
      map.fitBounds(b, { padding: [40, 40] });
    }

    $("#routes").innerHTML = d.routes.map((rt, i) => `
      <div class="route" style="background:${i === 0 ? 'rgba(77,163,255,0.06)' : 'transparent'}">
        <div class="hd"><b>Route ${rt.rank} ${i === 0 ? '(Recommended)' : '(Alternate)'}</b>
          <span class="badge b-${rt.risk_level === "clear" ? "open" : rt.risk_level === "at_risk" ? "risk" : "blocked"}">${rt.risk_level}</span></div>
        <div class="mono">${rt.distance_km} km &middot; free flow ${Math.round(rt.free_flow_minutes)} min
          &middot; risk-adjusted <b>${Math.round(rt.risk_adjusted_minutes)} min</b></div>
        <div style="color:var(--muted);font-size:12px">max segment risk ${(rt.max_segment_risk * 100).toFixed(0)}%
          ${rt.passes_blocked_segment ? " &middot; <b style='color:#ff4d4f'>blocked segment ahead</b>" : ""}</div>
        <div style="color:var(--muted);font-size:12px">${rt.segments.slice(0, 6).map((s) => s.road).join(" → ")}${rt.segments.length > 6 ? " …" : ""}</div>
      </div>`).join("") +
      `<div class="hint">Computed in ${d.computed_in_ms} ms &middot; ${d.model_note}</div>`;
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

// Preset route helper
window.setQuickRoute = function(lat1, lon1, lat2, lon2, name) {
  clearRoute();
  routeMode = true;
  $("#btnLayer").textContent = "📍 Route Mode: Active";
  $("#btnLayer").classList.add("primary");
  origin = [lat1, lon1];
  destination = [lat2, lon2];
  originMarker = L.circleMarker(origin, { color: "#4da3ff", fillColor: "#4da3ff", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Origin: " + name.split("→")[0]).openPopup();
  destMarker = L.circleMarker(destination, { color: "#ff4d4f", fillColor: "#ff4d4f", fillOpacity: 1, radius: 8 }).addTo(map).bindPopup("Destination: " + name.split("→")[1]);
  planRoute();
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
  b.disabled = false; b.textContent = "Refresh risk";
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

function bindUi() {
  routeMode = true;
  $("#btnRefresh").onclick = refreshAll;
  $("#btnClearRoute").onclick = clearRoute;
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
