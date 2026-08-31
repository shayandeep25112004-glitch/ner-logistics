# NER Logistics & Accessibility Intelligence Platform

AI/GIS logistics intelligence for India's North Eastern Region: real-time road
accessibility, landslide & flood disruption prediction, risk-aware routing with genuine
alternates, essential-commodity tracking, offline field reporting and multilingual alerts.

**This is a working build, not a mock-up.** The road network is real OpenStreetMap
geometry for all eight NER states, the terrain comes from the 90 m Copernicus DEM, the
rainfall is real ERA5 reanalysis, and the risk model is actually trained and evaluated.

---

## Quick start

### Option 1 — One-Click Scripts
- **Windows**: Double-click `run_app.bat` or run `.\run_app.ps1`
- **Linux / macOS**: Run `./run_app.sh`
- **Docker Compose**: Run `docker compose up -d`

### Option 2 — Standard Setup
```bash
pip install -r requirements.txt
make all          # build network -> terrain -> rainfall -> train model
make serve        # then open http://localhost:8000
```

Or step by step:

```bash
cd backend
python -m db                          # create the schema
python -m pipeline.build_network      # 1. real OSM graph
python -m pipeline.elevation          # 2. DEM slope/TRI/curvature
python -m pipeline.weather            # 3. 2 years of ERA5 rainfall
python -m pipeline.risk_model         # 4. train + evaluate
python -m pipeline.risk_model --inspect   # explain the model on real segments
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Then:

| URL | What it is |
|---|---|
| `http://localhost:8000/` | Command-centre dashboard |
| `http://localhost:8000/field` | Offline-first field reporting app (open on a phone) |
| `http://localhost:8000/driver` | Driver / consignment tracking app |
| `http://localhost:8000/docs` | Interactive API docs (FastAPI) |

---

## What is in the box

```
backend/
  config.py                  every tunable in one place
  db.py                      SQLite schema (ports 1:1 to PostGIS)
  pipeline/
    build_network.py         OSM -> junction-collapsed routable graph
    elevation.py             DEM -> slope, terrain ruggedness, curvature
    weather.py               ERA5 -> 24/72/168 h rainfall grid
    risk_model.py            training, evaluation, explainability
  routing/router.py          risk-weighted A* + Yen's K-alternate routes
  services/
    risk.py                  live per-segment scoring + district rollup
    alerts.py                alerting, cooldown, 6-language templates
    imd_client.py            IMD adapter (IP-whitelisted; falls back to Open-Meteo)
  api/main.py                FastAPI
frontend/
  index.html + app.js        command centre
  field.html                 offline-first field reporting (IndexedDB + service worker)
  driver.html                consignment tracking with divert advice
  sw.js                      app-shell caching
tools/make_deck.py           generates the pitch deck from live database numbers
docs/                        PLAN, ARCHITECTURE, DATA_SOURCES, RESULTS
```

---

## The four things worth understanding before you read the code

**1. The graph is collapsed at junctions, and that decision is load-bearing.**
One edge per OSM shape-point pair gives **1,968,231** edges — Mizoram alone averages 362
shape points per way because village roads are densely GPS-traced. Collapsing at junctions
keeps every road routable and all geometry for the map, and brings the graph down to
**159,730 edges / 156,289 vertices** — a 12× reduction. Shape-point density varies ~10×
between states, so without this the same road class costs 10× more to process in Mizoram
than in Assam.

**2. The model's labels are weak, and that is stated everywhere it matters.**
A supervised model needs ground truth. India has it — GSI's National Landslide Susceptibility
Mapping programme holds 91,000 historical landslides (33,904 field-validated), free from NGDR
and Bhukosh. Those datasets are not bundled here, so out of the box the model trains on
**weak labels derived from published rainfall-triggering thresholds × real DEM slope**.
The reported AUC therefore measures how well gradient boosting reproduces a
physically-motivated threshold rule over real terrain and real rainfall. **It is not
landslide prediction accuracy.** Swap `build_labels()` to read the GSI inventory and every
number becomes real; nothing else changes.

**3. Cost is not distance.**
`cost = free-flow minutes × (1 + 12 × risk)`. A short road with a 0.6 slide probability must
lose to a long valley detour — that is the entire point of the platform.

**4. The field app writes to IndexedDB before it touches the network.**
Losing a field officer's observation is worse than delaying it. Reports queue offline,
photos are downscaled client-side to ~800 KB, and a service worker caches the app shell so
the page opens at zero signal.

---

## Verified constraints (so you do not rediscover them)

* Open-Meteo elevation accepts **at most 100 coordinate pairs per POST** — 500 returns
  HTTP 400. It backs the 90 m Copernicus GLO-90 DEM and needs no API key.
* IMD's `api.imd.gov.in` endpoints are **IP-whitelisted**. Email their nodal officer with
  your static public IP. Until then `services/imd_client.py` falls back to Open-Meteo.
* Geofabrik was 302-redirecting during this build; `download.openstreetmap.fr` works and
  uses **underscores** (`arunachal_pradesh.osm.pbf`, not hyphens).
* `pyosmium`'s `TagList` has no `.items()` — use `dict(tags)`. NodeRefs on a way carry no
  tags, so ford tags need a separate node pass.
* An `.osm.pbf` stores nodes **before** ways, so the usual "remember which node ids I need,
  capture them in `node()`" handler silently captures nothing.

---

## Documentation

* [`docs/PLAN.md`](docs/PLAN.md) — the part-wise 90-day build plan (7 parts, exit criteria)
* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design decisions and the PostGIS migration path
* [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) — every dataset, with access status
* [`docs/RESULTS.md`](docs/RESULTS.md) — what was actually measured in this workspace
* `docs/NER_LAIP_deck.pptx` — pitch deck, generated from live numbers (`python tools/make_deck.py`)

---

## Honest limitations

* Weak labels until the GSI/Bhuvan inventory is joined.
* ERA5's ~31 km grid versus a 20 m road cut — a grid cell can be wet while the road is dry.
* OSM completeness varies sharply across remote districts.
* No live traffic feed, so congestion is inferred, not observed.
* Single-node SQLite and an in-memory graph — fine for the prototype, not for a national
  deployment.

Data © OpenStreetMap contributors (ODbL). Elevation © Copernicus GLO-90. Rainfall © ERA5 /
Copernicus Climate Change Service.
