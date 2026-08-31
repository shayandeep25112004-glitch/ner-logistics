# Data sources

Every dataset the platform uses or should use, with access status verified while building
this repo.

---

## Used in this build

### 1. Road network — OpenStreetMap

| | |
|---|---|
| Source | `https://download.openstreetmap.fr/extracts/asia/india/<state>.osm.pbf` |
| Access | Free, no key, ODbL licence |
| Verified | ✅ all 8 NER states downloaded (~130 MB total) |
| Used in | `pipeline/build_network.py` |

**Notes**
* Filenames use **underscores**: `arunachal_pradesh.osm.pbf`. The hyphenated form 404s on
  this mirror.
* Geofabrik (`download.geofabrik.de`) was returning 302 redirects to its homepage from this
  network; the openstreetmap.fr mirror served identical data.
* The extracts are **non-standard PBF layout** (the producer puts the blob length in
  `BlobHeader` field 3 as a varint rather than inlining the blob). `pyosmium` reads them
  fine; a hand-rolled protobuf decoder will not.
* Only `motorway … tertiary` are kept as routable. `track`, `path` and `service` exist in
  large numbers but are not viable for freight.

### 2. Terrain — Copernicus GLO-90 DEM (90 m)

| | |
|---|---|
| Source | `https://api.open-meteo.com/v1/elevation` |
| Access | Free, no key |
| Verified | ✅ batch POST; **max 100 coordinate pairs per request** (500 → HTTP 400) |
| Used in | `pipeline/elevation.py` |

### 3. Historical rainfall — ERA5 reanalysis

| | |
|---|---|
| Source | `https://archive-api.open-meteo.com/v1/archive` |
| Access | Free, no key |
| Verified | ✅ hourly `precipitation`, multi-location, multi-year |
| Used in | `pipeline/weather.py` |

ERA5 has roughly a 5-day ingest lag, so the window ends five days before today.

### 4. Live forecast

| | |
|---|---|
| Source | `https://api.open-meteo.com/v1/forecast` |
| Access | Free, no key |
| Used in | `services/risk.py` |

---

## Available and should be joined for production

### 5. IMD — the authoritative warning feed  ⚠️ IP-whitelisted

| | |
|---|---|
| Source | `https://api.imd.gov.in/api/v1/...` |
| Access | **Requires your static public IP to be whitelisted by IMD** |
| Docs | `https://api.imd.gov.in/public/api_reference.html` |
| Adapter | `services/imd_client.py` (written; needs `IMD_API_KEY`) |

Endpoints that matter:

| Endpoint | Gives you |
|---|---|
| `/api/v1/districtnowcast` | District-wise nowcast, 19 warning categories, colour-coded |
| `/api/v1/districtrainfall?id=<n>` | District rainfall + warning code + colour |
| `/api/v1/state_district_rainfall_forecast` | 5-day district rainfall forecast |
| `/api/v1/stationnowcast?id=<station>` | Station-level nowcast |

IMD warning codes: `1` no warning · `2` heavy rain · `4` thunderstorm & lightning ·
`15` fog · `16` very heavy rain · `17` extremely heavy rain.
Colour codes: `1` red · `2` orange · `3` yellow · `4` green.

To get access: write to IMD's nodal officer with your static public IP. IMD's own MAUSAM
journal article documents this process and lists state governments (Uttar Pradesh,
Telangana, Kerala) already consuming these APIs to drive their disaster-alert sites.

### 6. GSI National Landslide Susceptibility Mapping (NLSM) — **this is your label source**

| | |
|---|---|
| Coverage | 1:50,000 susceptibility mapping for the Himalayan region, the **Tertiary Belt of north-eastern India** and the Western Ghats — 19 states/UTs, ~4.3 lakh km² |
| Inventory | **91,000 historical landslides, of which 33,904 are field-validated** |
| Access | Free download from GSI's National Geoscience Data Repository (NGDR) and the **Bhukosh** portal; also viewable in the **Bhooskhalan** mobile app |
| Status | GSI is upscaling to 1:10,000/1:5,000 for 200 critical sectors by 2028 |

This is the dataset that turns the model from a threshold-rule approximation into a real
landslide predictor. Joining it is a change to `build_labels()` and nothing else.

### 7. ISRO Bhuvan — event-based landslide inventory

| | |
|---|---|
| Source | `https://bhuvan-app1.nrsc.gov.in/disaster/` |
| Access | Free; published as WMS + webservice URLs, plus technical documents and PDFs |
| Also | Landslide Atlas of India (65 MB PDF), event impact maps |
| Other Bhuvan layers | Cartosat-1 10 m DEM, road data, land use/cover |

Useful for near-real-time event validation — Bhuvan publishes post-event impact maps
(e.g. the Chooralmala/Wayanad slides).

### 8. NDEM — National Database for Emergency Management

`https://ndem.nrsc.gov.in` — ISRO's emergency-management geospatial portal. Flood
inundation, hazard layers, critical infrastructure. Free.

### 9. Central Water Commission — flood forecasts

River-level flood forecasts for the Brahmaputra and Barak basins, which is what you need for
the flood (as opposed to landslide) side of the model. Free.

### 10. NHIDCL / state PWD — closure ground truth

NHIDCL publishes project status documents (PDF/XLSX) covering the NER national highways,
including ongoing works and completion dates. State PWDs publish closure notices. Neither
has an API — this is a scraping job, but it is the closest thing to a real "road closed"
label available today, and it is what the model should ultimately be validated against.

---

## Sources considered and rejected

| Source | Why not |
|---|---|
| Google Maps / Directions API | Cost at national scale, and licence terms restrict caching the derived network. Fine for a demo, wrong for a government platform. |
| Mapbox / HERE | Same licensing problem. Mapbox's free tier is fine for the *basemap* only. |
| Open-Meteo Flood API | Hourly `river_discharge` is a useful extra feature but adds a dependency for marginal gain at this stage. |
| Commercial telematics GPS | The driver PWA already provides GPS; buying it duplicates a capability you are building. |

---

## Reproducing the fetch

```bash
cd backend
python -m pipeline.build_network --refresh-extracts   # force re-download
python -m pipeline.elevation                          # only fills NULL elevations
python -m pipeline.weather                            # 2-year window ending 5 days ago
```

All three are idempotent and resumable.

---

## Licence note

Road data is © OpenStreetMap contributors under ODbL, which requires attribution on any
published map and share-alike on derived databases. Elevation is © Copernicus GLO-90.
Rainfall is © ERA5 / Copernicus Climate Change Service. The attribution strings are already
in `frontend/app.js`; keep them.
