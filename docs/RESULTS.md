# Results

Everything below was measured in this workspace by running the pipeline and the test suite.
Reproduce with `make all && cd backend && python tests/test_platform.py`.

Test suite: **91/91 checks passed**.

> **Note on provenance.** These numbers come from a clean rebuild. An earlier run of this
> project produced slightly different figures (159,730 edges, AUC 0.994); the differences
> are explained below where they matter. Where a number changed, the reason is stated
> rather than papered over.

---

## 1. Road network

| Metric | Value |
|---|---|
| Road segments (edges) | **163,432** |
| Graph vertices | **159,456** |
| Network length | **54,776 km** |
| Bridges | **5,462** |
| River fords | **95** |
| States | 8 (AR, AS, ML, MN, MZ, NL, SK, TR) |
| Raw OSM shape-point segments | 1,969,016 |
| Candidate edges before de-duplication | 299,558 |
| Border-overlap duplicates removed | **136,126** |
| Self-loops dropped | 7 |

Segments by state: NL 60,861 · ML 33,992 · AS 17,038 · AR 16,282 · MN 12,709 · MZ 11,593 ·
TR 9,971 · SK 986.

### The duplicate problem, found by measurement

Junction collapsing alone gave **299,558** edges covering only **163,432** distinct junction
pairs — 45% were duplicates. I sampled 200 of them and checked the geometry: **all 200 were
the same physical road**, matching to within 3 m with the same `ref` and the same length,
differing only in OSM way id. The cause is that the OSM.fr state extracts overlap at borders.

Keeping them is not harmless. It inflates network length (59,321 km vs 54,776 km), makes the
risk model score one landslide twice, and hands the router parallel edges between the same
junctions — which is precisely what made Yen's K-shortest return three "alternates" that all
drove through the same blocked corridor. De-duplicating by junction pair, preferring the
richer record, is what produces the 1.02 edges-per-node graph above.

**State attribution** uses the *smallest* extract that contains a road. Without that rule,
Assam's 59 MB extract claimed 100,150 edges across half the region, because the large
extracts reach well outside their own state while the small ones do not.

## 2. Terrain

| Metric | Value |
|---|---|
| Vertices with elevation | **159,456 / 159,456 (100%)** |
| Mean slope | 5.17° |
| Max slope | 48.36° |
| Mean terrain ruggedness index | 5.03 m |
| DEM tiles | 728 at z10, **44.9 MB**, fetched in **17.3 s** |
| Time to sample all 159k vertices | 8.6 s, **zero network calls** |

Validation against Open-Meteo's 90 m Copernicus GLO-90 DEM:

| Place | Terrain tiles | GLO-90 |
|---|---|---|
| Guwahati | 51.0 m | 50 m |
| Shillong | 1,433.9 m | 1,436 m |
| Cherrapunji | 1,234.6 m | 1,313 m |
| Aizawl | 1,074.8 m | 1,132 m |
| Gangtok | 1,537.5 m | 1,650 m |

Cherrapunji and Gangtok sit on cliff edges where one pixel (~137 m) spans tens of metres
vertically, so a bilinear sample will not match a point lookup exactly. That is DEM
resolution, not a decoder error — the two plains/plateau sites agree to within 2.5 m.

**Fords needed a second look.** Reading the tag off *ways* found only 15: there are 1,278
nodes tagged `ford=yes` in the region but only 16 routable ways carry a ford tag, and nearly
all of those are tracks and paths. The meaningful measure is a ford *node* sitting on a road
we route on, which gives **95**.

## 3. Rainfall

| Metric | Value |
|---|---|
| Rows in `weather_grid` | **248,200** |
| Grid cells | 340 (0.5° ≈ 55 km) |
| Days | 730 (2024-08-25 → 2026-08-24) |
| Cells with < 700 days | **0** |
| Mean daily rainfall, Jun–Sep | 11.0 mm |
| Mean daily rainfall, Dec–Feb | 0.6 mm (**19×**) |
| Peak 24 h rainfall | **282.1 mm** |
| 99th percentile, 24 h / 72 h / 168 h | 49.9 / 124.4 / 239.1 mm |
| Fetch time (17 chunks, with 429 backoff) | 162 s |

## 4. Disruption model

| Metric | Value |
|---|---|
| Edge-days labelled | 8,760,000 |
| **True** positive rate | 0.014346 |
| Blockage-days per segment-year | **5.24** |
| P(fail \| slope > 18°) | 0.1265 |
| P(fail \| slope < 5°) | 0.0121 |
| Steep : flat ratio | **10.44×** |
| Training matrix | 879,718 rows, 14.29% positive after 6:1 down-sampling |
| ROC-AUC | **1.0000** |
| Average precision | **1.0000** |
| Precision at 90% recall | **1.0000** |
| Calibration, worst decile gap | **0.0002** |
| Fit time | 23.3 s |

Permutation importance (mean AP drop):

| Feature | Importance |
|---|---|
| rain_24h | 0.4084 |
| rain_72h | 0.0435 |
| slope_deg | 0.0244 |
| rain_168h | 0.0098 |
| ele_m, tri, curvature, max_intensity, base_reliability, length_km, month, is_monsoon, is_bridge, is_ford | **0.0000** |

### An AUC of 1.0 is a warning, not a result

**These metrics carry no information about landslide prediction.** The labels are weak: a
rainfall-triggering rule crossed with real DEM slope. The model was asked to reproduce a
deterministic function of the features it was handed, and it did — perfectly.

Three things in this table prove the point:

1. **AUC is exactly 1.0000.** Not 0.99, not 0.999 — 1.0. A model only achieves that on a
   noiseless, deterministic target.
2. **Ten of fourteen features have exactly zero importance.** The label is a function of
   `rain_24h`, `rain_72h`, `rain_168h` and `slope_deg` only, so the model correctly found
   that everything else — elevation, ruggedness, curvature, bridges, month — contributes
   nothing. Real landslides depend on geology, soil depth, land use and distance to
   lineaments; a label that makes all of those irrelevant is too simple, and the model just
   told us so.
3. **The calibration table is degenerate.** Deciles 1–8 are all 0.000 predicted and 0.000
   observed; decile 9 is 0.4288/0.4286; decile 10 is 1.000/1.000. The label function
   saturates, so the "probabilities" are essentially binary. They track perfectly because
   there is almost nothing between 0 and 1.

I also removed a genuine leak on the way: the feature set originally included `saturation`,
which is *literally the label's own load/need ratio* — the answer, handed to the model. It
also had `i24`, `i72` and `i168`, which are just the rainfall columns divided by constants
(gradient boosting is scale-invariant per feature, so they added nothing). Removing them did
not lower the AUC, which is itself the proof that the label, not the features, is doing the
work.

**The single highest-value change available** is to replace `failure_probability()` with a
join against GSI's National Landslide Susceptibility Mapping inventory (91,000 historical
landslides, 33,904 field-validated, free from NGDR and the Bhukosh portal) plus ISRO
Bhuvan's event-based inventory. Nothing else in the file changes, and then every metric
becomes a real one. Until then, **do not quote the AUC as accuracy.**

### Two calibration defects this design avoids

**Literature thresholds do not transfer.** The widely cited "150 mm in 24 h triggers slides"
sits far above this region's 99th percentile, which is 49.9 mm. Thresholds are derived from
the observed grid, so "i24 = 1.0" means *a 1-in-100 wet day here*.

**A hard slope gate makes the model ignore rainfall.** An earlier label read
`if slope_deg < 18: p = 0.004`, and permutation importance came back slope 0.519 vs
rain_72h 0.005 — a threshold rule wearing gradient-boosting clothing. Slope now *modulates*
how much water is needed. The result is the 10.44× steep-to-flat separation above, with flat
ground still failing at 1.2% — because Assam's plains do close, from flooding and
subsidence.

## 5. Live risk scoring

| Metric | Value |
|---|---|
| Segments scored | 163,432 |
| Wall time | **7.7 s** including the live forecast fetch |
| `predict_proba` alone | 2.04 s |
| Mean risk | 0.0092 |
| At risk (≥ 0.35) | 1,721 |
| Blocked (≥ 0.70) | 1,533 |
| Distinct risk values | 25,177 |
| Rainfall source | Open-Meteo 7-day forecast |

Two performance bugs were found by timing the components rather than guessing:

* `cell_for()` scanned all 340 grid cells for every segment — 55 million distance
  computations. The grid is regular, so the nearest cell is index arithmetic. Verified
  against the linear scan on 3,000 random points: **0 mismatches**.
* The 17 forecast requests ran sequentially, ~10 s of pure waiting, dominating a pass whose
  model inference takes 2 s. They now run 8-wide.

Together: **25.4 s → 7.7 s**.

## 6. Routing

| Journey | Distance | Free-flow | Result |
|---|---|---|---|
| Guwahati → Shillong | 98.72 km | 104.3 min | 971 edges, 1 route |
| Guwahati → Shillong, primary blocked | 177.12 km | — | detour, **0% edge overlap** |
| Guwahati → Gangtok | — | — | 0 routes **+ diagnostic** |
| `POST /api/route` latency | — | — | 1,027 ms |
| Snap distance | 396.5 m / 5.8 m | — | origin / destination |

### The finding worth putting on a slide

**The NER has almost no alternate routes.** Guwahati → Shillong returns a single route with
this diagnostic:

> *"only one route returned: no alternative diverges from the primary by the 15% minimum,
> which is the expected answer for a single-corridor district"*

That is the problem statement, demonstrated by the data: these districts hang off one
highway, so when it fails there is no plan B. The platform's value is therefore in
**warning before the corridor fails**, not in finding detours.

An earlier version returned three "alternates" sharing 96–100% of the primary — micro-reroutes
around the duplicate parallel edges described in §1. The divergence filter removed them, and
de-duplicating the graph removed the underlying cause.

## 7. Connectivity analysis

| Metric | Value |
|---|---|
| Connected components | 284 |
| Dominant component | **155,286 nodes** across AR, AS, ML, MN, MZ, NL, TR |
| Sikkim | **840 nodes, its own component** |
| Single-state components > 50 nodes | 9 |

Sikkim is unreachable from the rest of the graph because its link to India runs through West
Bengal, which is not an NER state and so is not in the extracts. Rather than an empty route
list that reads "no route found", the API returns a diagnostic:

> *"origin and destination are in different connected components of the road graph — no route
> exists in the current dataset (state extracts are clipped at borders, so corridors leaving
> the NER are severed)"*

Fix for production: include the bordering districts of West Bengal, or stitch extracts on
shared node IDs.

## 8. Alerts

One alert per corridor per run, aggregated with a segment count:

> **Road blocked: NH29** — NH29 in NL is assessed as blocked (risk 100%). Use the suggested
> alternate route. **116 segments on this corridor are affected.**

Per-segment alerting produced 40+ near-identical messages for a single highway crossing one
wet grid cell — the alert fatigue that gets warning systems muted. A 30-minute per-segment
cooldown and a 60-alert-per-run cap sit underneath. Latest run: **52 alerts** across the
region.

Verified rendering in all six configured languages, e.g. `পথ বন্ধ: NH-27` (Assamese),
`সড়ক অবরুদ্ধ: NH-27` (Bengali), `সडक अवरुद्ध: NH-27` (Nepali), `লম্বী থিংল্লে: NH-27` (Meitei).

---

## Bugs found by verification, not by reading

| Bug | How it was caught |
|---|---|
| 136,126 duplicate edges from border-overlapping extracts | distinct junction-pair count vs edge count |
| Assam claiming 100k edges across the region | per-state edge totals vs extract sizes |
| Fords undercounted 95 → 15 | counting node tags, not way tags |
| `saturation` feature leaked the label's own answer | AUC came back exactly 1.0000 |
| `build_dataset` materialised 8.76M×18 rows | **OOM-killed at 1.55 GB RSS on a 2 GB box** |
| `searchsorted` on a shuffled array | `IndexError` on the reverse-index write |
| `cell_for` linear scan made scoring 25 s | timing the components separately |
| 17 sequential forecast requests | same |
| `wx.get(cell)` missing the day → empty matrix crash | running `--inspect` |
| One highway → 40 duplicate alerts | reading real alert output |
| Empty route list for unroutable pairs | Gangtok test |
| Deck silently kept a stale file after failing | schema drift between metrics and `make_deck.py` |
