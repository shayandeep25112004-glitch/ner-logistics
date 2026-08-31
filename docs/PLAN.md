# Part-wise build plan — NER Logistics & Accessibility Intelligence Platform

A 90-day, seven-part plan for a final-year / capstone build. Each part ends in something
you can demo, so you are never three weeks from "nothing works".

The numbers in the **Exit criteria** lines are the ones to defend in a viva. They come from
actually running this repo — see `docs/RESULTS.md`.

---

## The one-sentence architecture

> Real OSM road graph + real DEM terrain + real ERA5 rainfall → a trained
> gradient-boosting model that scores every road segment for disruption probability →
> a risk-aware A\* router that turns those probabilities into routes and alternates →
> a dashboard, an offline field app and a driver app on one FastAPI origin.

```
                 ┌────────────────────────────── DATA ─────────────────────────────┐
 OpenStreetMap   │  Copernicus GLO-90 DEM   ERA5 rainfall   IMD warnings (prod)     │
 (8 NER states)  │  (Open-Meteo elevation)  (Open-Meteo)    GSI/Bhuvan landslide DB │
                 └───────┬──────────────────────┬───────────────────┬──────────────┘
                         ▼                      ▼                   ▼
                 ┌────────────────┐   ┌──────────────────┐  ┌────────────────┐
                 │ build_network  │   │ elevation        │  │ weather        │
                 │ graph at       │   │ slope / TRI /    │  │ 24·72·168 h    │
                 │ junctions      │   │ curvature        │  │ rainfall grid  │
                 └───────┬────────┘   └────────┬─────────┘  └───────┬────────┘
                         └──────────┬──────────┴───────────────────┘
                                    ▼
                          ┌───────────────────┐
                          │ risk_model        │  HistGradientBoosting, calibrated,
                          │ (per-segment p)   │  permutation importance, PR curve
                          └─────────┬─────────┘
                    ┌───────────────┼───────────────────┐
                    ▼               ▼                   ▼
            ┌──────────────┐ ┌────────────┐    ┌──────────────────┐
            │ router (A*   │ │ alerts +   │    │ district rollup  │
            │ + Yen K-alt) │ │ i18n       │    │ connectivity idx │
            └──────┬───────┘ └─────┬──────┘    └────────┬─────────┘
                   └───────────────┴────────────────────┘
                                   ▼
              FastAPI  ──►  dashboard · field PWA · driver PWA
```

---

## Part 0 — Scope, data sources and the honest baseline  *(Days 1–5)*

Before writing code, pin down three things. Teams that skip this part burn two weeks later.

**1. Which districts?** Pick a corridor, not "the whole NER". Suggested primary corridor:
**Guwahati → Shillong → (NH-27) → Silchar**, plus **Dimapur → Kohima → Imphal (NH-29/NH-02)**
— the latter is the one that fails every monsoon and gives you a real story.

**2. Which data sources?** All of these were verified working while building this repo:

| Need | Source | Status |
|---|---|---|
| Road network | OpenStreetMap state extracts (`download.openstreetmap.fr`) | ✅ free, no key |
| Terrain / elevation | Open-Meteo elevation API (90 m Copernicus GLO-90) | ✅ free, no key, ≤100 pts/POST |
| Historical rainfall | Open-Meteo archive API (ERA5) | ✅ free, no key |
| Live forecast | Open-Meteo forecast API | ✅ free, no key |
| Authoritative warnings | IMD `api.imd.gov.in` (`/api/v1/districtnowcast`, `/api/v1/districtrainfall`) | ⚠️ **IP-whitelisted** — email IMD's nodal officer with your static IP |
| Landslide inventory / susceptibility | GSI **National Landslide Susceptibility Mapping** — 91,000 landslides, 33,904 field-validated, via NGDR + Bhukosh | ✅ free download |
| Event-based landslide inventory | ISRO Bhuvan `bhuvan-app1.nrsc.gov.in/disaster` (WMS + webservice) | ✅ free |
| Flood inundation | Bhuvan / NDEM, CWC flood forecasts | ✅ free |
| NH project & closure status | NHIDCL project status PDFs, state PWD notices | ⚠️ scrape |

**3. What is your baseline?** Write it down now: today a district administration learns a
road is blocked from a phone call. Your baseline is *"phone call, 2–6 hours, no map, no
alternate"*. Everything you measure later is against that.

**Exit criteria:** a one-page data-source sheet with a working `curl` for each API, and a
named primary corridor.

---

## Part 1 — The road graph  *(Days 6–14)*  → `pipeline/build_network.py`

Build the routable network from the 8 state extracts.

**Steps**
1. Download the 8 `.osm.pbf` extracts (~130 MB total).
2. Parse with `pyosmium` (`pip install osmium`, prebuilt wheels — no compiler needed).
3. Keep only routable classes: `motorway … tertiary`.
4. **Collapse the graph at junctions.** This is the step that separates a working build
   from a broken one. One edge per OSM shape-point pair gives **1,968,231** edges; one edge
   per junction-to-junction chain gives **156,289 vertices / 159,730 edges** — a 12×
   reduction with identical routability and full geometry preserved for the map.
5. Attach bridges, tunnels, river **fords**, surface quality, and the NH/SH `ref` code.

**Traps this repo already hit (so you don't):**
- OSM PBF stores nodes *before* ways. The classic "collect needed node ids in `way()`,
  capture them in `node()`" handler silently captures **nothing**.
- A vertex must be: shared between ways, **or** a way terminus, **or** repeated inside one
  way (roundabout/hairpin). Counting node occurrences alone cannot tell a dead end from an
  interior shape point — both occur exactly once.
- `pyosmium` `TagList` has no `.items()`; use `dict(tags)`.
- NodeRefs on a way carry **no tags** — ford tags need a separate node pass.
- Geofabrik was 302-redirecting during this build; `download.openstreetmap.fr` worked and
  uses underscores (`arunachal_pradesh.osm.pbf`, not hyphens).

**Exit criteria:** `SELECT COUNT(*) FROM edge` in the low hundred-thousands, not millions,
and a route exists between Guwahati and Shillong. Verified here: 159,730 edges,
Guwahati → Shillong = 98.73 km / 105.3 min free-flow, computed in ~1 s.

---

## Part 2 — Terrain features  *(Days 15–21)*  → `pipeline/elevation.py`

Landslides are a slope-and-water phenomenon, so slope has to be a real feature.

**Steps**
1. Get elevation for every vertex. **Do not use a rate-limited point API for this** — 156k
   vertices at 100/request is ~1,563 calls, which exhausts the free tier's hourly quota.
   Download AWS Terrain Tiles (public S3, no key, no quota) for the NER bbox — 728 tiles /
   45 MB at zoom 10 — and sample locally. Open-Meteo's endpoint stays as a fallback.
2. **Verify coverage against the database, not against a counter.** An earlier version
   counted failed batches as resolved and printed "resolved 156289 vertices" while writing
   NULLs to 151,289 of them.
2. `slope_deg` — steepest gradient of any incident edge.
3. `tri` — terrain ruggedness index: std-dev of elevation across the vertex + neighbours.
4. `curvature` — mean absolute turning angle; switchbacks concentrate runoff and slide.

**Design note:** if you have 150 k+ vertices, don't call the API per vertex. Build a coarse
elevation lattice and interpolate — or, as done here, collapse the graph first so you only
ever need elevations at junctions.

**Exit criteria:** 100% elevation coverage confirmed by `SELECT COUNT(*) WHERE ele_m IS
NULL`, and mean/max slope sanity-checked against known terrain. Verified here: 156,289/156,289
covered, mean slope 6.26°, max 87.74°, mean TRI 5.06 m.

---

## Part 3 — Rainfall  *(Days 22–28)*  → `pipeline/weather.py`

**Steps**
1. Lay a 0.5° (~55 km) grid over the NER bbox — ~176 cells. Finer than ERA5's own ~31 km
   grid, so you are not inventing resolution.
2. Pull 2 years of hourly precipitation per cell.
3. **Reduce to daily aggregates on arrival**: `rain_24h`, `rain_72h`, `rain_168h`,
   `max_intensity`. Storing 3 M hourly numbers buys you nothing; these four are what the
   landslide physics uses.
4. Write an `imd_client.py` adapter with the same return shape, so swapping in IMD's
   official district nowcast is a config change, not a rewrite.

**Exit criteria:** `weather_grid` has ~130 k rows; you can name the wettest day in the
window and it is in June–September.

---

## Part 4 — The disruption model  *(Days 29–50)*  → `pipeline/risk_model.py`

This is the part judges will interrogate. Structure it as three honest layers.

**4a. Features** (16): terrain (`slope_deg`, `tri`, `curvature`, `ele_m`), rainfall
(`rain_24h`, `rain_72h`, `rain_168h`, `max_intensity`), season (`month`, `is_monsoon`),
asset (`is_bridge`, `is_ford`, `is_unsealed`, `highway_rank`), geometry (`log_length_m`,
`base_reliability`).

**4b. Labels — be explicit.** A supervised model needs ground truth. India *has* it:
- GSI NLSM: 91,000 historical landslides, 33,904 field-validated (NGDR / Bhukosh).
- ISRO Bhuvan event-based landslide inventory (WMS).
- Your own validated field reports.

Those are not bundled in this repo, so out of the box the model trains on **weak labels
from published rainfall-triggering thresholds × DEM slope**. That is a legitimate
cold-start strategy, and it means:

> The AUC measures how well gradient boosting reproduces a physically-motivated threshold
> rule over real terrain and real rainfall. **It is not landslide prediction accuracy.**
> Never present it as such. Swap `build_labels()` to read the GSI inventory and every
> number becomes real — nothing else in the file changes.

**4c. Train and evaluate.**
- Model: `HistGradientBoostingClassifier` (handles the imbalance and the mixed types
  without one-hot explosion; trains in seconds).
- Class imbalance: down-sample negatives to ~25:1, and report the **positive rate** so
  nobody mistakes a 96% accuracy for skill.
- Metrics that matter operationally: **ROC-AUC**, **average precision**, and above all
  **precision at 90% recall** — "if we catch 9 of 10 failures, how many of the corridors
  we flag actually fail?" That number is what decides how many crews you send for nothing.
- **Calibration by decile.** An uncalibrated probability is useless for a threshold.
- **Permutation importance**, so you can say *why* the model flagged a segment.

**4d. The explainability demo.** `python -m pipeline.risk_model --inspect` prints the
highest-risk real segments on a chosen day with their slope and rainfall next to the
probability. This is the single most convincing slide in the deck.

**Exit criteria:** metrics JSON on disk, calibration table monotonic, importance ranking
led by slope and rainfall features (if `month` dominates, you have leakage — investigate).

---

## Part 5 — Risk-aware routing  *(Days 51–64)*  → `routing/router.py`

**Steps**
1. Cost function: `cost = free_flow_minutes × (1 + W × risk)`. A short road with a 0.6
   slide probability must lose to a long valley detour — that is the entire point.
2. **A\*, not plain Dijkstra.** The heuristic (straight-line minutes at the fastest road
   class) is admissible because the risk multiplier is ≥ 1, so optimality is preserved and
   long inter-district journeys expand far fewer vertices.
3. **Blocked ≠ deleted.** Give a blocked segment a huge-but-finite penalty. When a whole
   valley is cut you still want to return *a* route, flagged as passing a blocked segment.
4. **Alternates via Yen's K-shortest loopless paths.** "Second shortest" usually shares 95%
   of the primary — useless when the primary is buried. Yen forces each alternative to
   diverge at a different spur point.
5. Cap the spur points (24 here). Yen re-runs the search per spur point; uncapped, it is
   your entire latency budget.

**Exit criteria:** for Guwahati→Tawang in a simulated monsoon, the primary route changes
versus dry-season routing, and route 2 shares < 50% of route 1's edges.

---

## Part 6 — Apps  *(Days 65–82)*  → `api/main.py`, `frontend/`

**6a. API (FastAPI).** One origin serves everything: `/api/network/edges.geojson`,
`/api/route`, `/api/risk/districts`, `/api/risk/corridors`, `/api/field/reports`,
`/api/shipments` + `/ping`, `/api/alerts`. Relative URLs mean no CORS and no hard-coded
hosts — it just works behind a proxy.

**6b. Dashboard** (`frontend/index.html`): risk-coloured Leaflet map, district
connectivity index, high-risk corridors, live alerts, click-to-plan routing, consignment
table.

**6c. Field app** (`frontend/field.html`) — the offline story is a graded requirement, so
build it properly:
- every report is written to **IndexedDB before any network attempt**;
- one **batched** POST flushes the queue (the API takes an array for exactly this reason);
- photos are **downscaled client-side to ~800 KB** — an 8 MB phone photo over a 2G uplink
  is a failed upload;
- sync fires on `online`, on `visibilitychange`, and on a timer;
- a **service worker** caches the app shell so the page opens at zero bars;
- the server **snaps each report to the nearest segment** and pins that segment's risk, so
  a confirmed obstruction overrides the forecast on the next route.

**6d. Driver app** (`frontend/driver.html`): registers a consignment, streams GPS pings,
buffers them offline, and renders the server's divert advice with minutes saved.

**6e. Multilingual alerts** (`services/alerts.py`): static templates in English, Hindi,
Assamese, Bengali, Nepali and Meitei. Static rather than a translation API because alerts
must still go out when the network is down, and a fixed template means the wording has
been reviewed once by a speaker.

**Exit criteria:** kill your Wi-Fi mid-report in the field app, reconnect, and watch it
arrive on the dashboard.

---

## Part 7 — Evidence, packaging and the viva  *(Days 83–90)*

1. **Measure against the baseline.** Time-to-detect, alternate-route savings, alerts per
   true event.
2. **Threats to validity** — write them yourself before a judge does: weak labels, ERA5
   grid resolution vs a 20 m road cut, OSM completeness in remote districts, no live
   traffic feed.
3. **What production changes:** PostGIS, a tile server, contraction hierarchies, IMD
   whitelist, GSI inventory, SMS gateway, auth (RBAC for field officers vs control room),
   and an audit log for every alert.
4. Build the deck (`python tools/make_deck.py`), record a 3-minute walkthrough.

---

## Suggested team split (4 people)

| Person | Owns | Parts |
|---|---|---|
| A | Data & GIS pipeline | 1, 2, 3 |
| B | ML model & evaluation | 4 |
| C | Routing & backend API | 5, 6a |
| D | Frontend, offline sync, i18n | 6b–6e, 7 |

## What to cut first if you run out of time
1. The driver app (Part 6d) — the ping endpoint still demonstrates the idea.
2. Multilingual beyond Hindi + Assamese.
3. District-level (vs state-level) administrative boundaries.
4. **Never cut:** the junction collapse (Part 1), the calibration table (Part 4c), or the
   offline field queue (Part 6c). Those three are what make it credible.
