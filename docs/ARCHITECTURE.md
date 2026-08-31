# Architecture & design decisions

Every significant choice, why it was made, and what changes for production.

---

## 1. Storage: SQLite now, PostGIS later

The prototype uses a single SQLite file with two plain indexes on `edge(node_a)` and
`edge(node_b)`. That is deliberate: a student team should be able to `git clone`, `pip
install`, `make all` and have a working system with no database server.

The schema is written so the migration is mechanical:

| Prototype | Production |
|---|---|
| `node.lat`, `node.lon` REAL | `geometry(Point, 4326)` + GiST index |
| `edge.geom` TEXT (JSON polyline) | `geometry(LineString, 4326)` |
| `router._grid` (Python spatial hash) | `ST_DWithin` on a GiST index |
| `field_report` snapped in Python | `UPDATE … SET edge_id = (SELECT … ORDER BY geom <-> point LIMIT 1)` |
| In-process `score_all()` cache | Redis, shared across API workers |
| In-memory adjacency | Keep it — a routing process should own its graph |

The in-memory adjacency stays in production. Loading ~30–160 k edges is tens of MB and
rebuilding it per request would dominate the latency budget; a routing worker should own
its graph and reload it on a signal.

---

## 2. The graph: why junction collapsing is not an optimisation but a requirement

| Approach | Edges | Consequence |
|---|---|---|
| One edge per shape-point pair | 1,968,231 | ~1.5 M elevation lookups, A\* expands far more vertices, GeoJSON payload in the hundreds of MB |
| Collapsed at junctions | **159,730** | Same routability, full geometry preserved for the map, elevation only at the 156,289 vertices |

**The vertex rule.** A node must stay a vertex if it is:
1. shared between two or more ways (a junction), **or**
2. a way terminus (a dead end — otherwise A\* routes into a chain and finds no way out),
   **or**
3. repeated within a single way (a roundabout or hairpin).

Node-occurrence counts alone are **not** sufficient: an interior shape point occurs exactly
once, and so does a dead-end terminus. Only the position inside the way distinguishes them.
Getting this wrong in one direction gives you a graph where roads do not connect; in the
other it gives you 1.97 M edges. Both failure modes were hit during this build.

**Self-loops are dropped.** A roundabout whose start and end vertex are the same cannot be
represented as an edge between two distinct vertices. It is a small, correct simplification:
a roundabout is a node in a routing graph, not a road to traverse.

---

## 3. Elevation: cached tiles, verified coverage, resumable

Bulk elevation comes from **AWS Terrain Tiles** (`pipeline/dem.py`): a public S3 bucket, no
API key and no hourly quota. The whole NER at zoom 10 is 728 tiles / 45 MB, fetched in ~17 s
and cached, after which sampling all 156,289 vertices takes ~10 s with no network at all.

This replaced the Open-Meteo elevation endpoint, which accepts at most 100 coordinate pairs
per POST (500 returns HTTP 400) — so 156 k vertices means ~1,563 requests, which exhausted
the free tier's hourly limit and returned HTTP 429 partway through. Open-Meteo is kept as a
fallback for points the tiles cannot answer. Validated against it at three known places:
Guwahati 51.0 m vs 50 m, Shillong 1,433.9 m vs 1,436 m, Cherrapunji 1,301.5 m vs 1,313 m.

`fetch_elevations()` only fills vertices where `ele_m IS NULL`, so an interrupted run
resumes rather than restarting. It also **verifies coverage against the database** and exits
non-zero below 98%, because an earlier version reported "resolved 156289 vertices" while
having written NULLs to 151,289 of them.

---

## 4. Rainfall: reduce on arrival

Two years of hourly data for 340 grid cells is ~6 M numbers (~24 MB of JSON). The landslide
physics uses **accumulations**, not individual hours, so the fetcher reduces each cell's
hourly series to four daily numbers as it arrives: `rain_24h`, `rain_72h`, `rain_168h`,
`max_intensity`. The table stays at ~250 k rows and the model loses nothing it uses.

**Grid resolution.** 0.5° (~55 km) is finer than ERA5's own ~31 km native grid, so no
resolution is invented. It is also the honest limit: a 55 km cell cannot resolve which side
of a ridge is raining. This is the single biggest accuracy limitation and is stated on the
deck rather than hidden.

**Live scoring splices forecast onto archive.** The model was trained on antecedent
rainfall that is *observed*. At request time the future 72 h comes from the forecast API and
the preceding 168 h comes from the stored archive, joined at the current hour. Feeding a
forecast-only vector to a model trained on observed antecedents is a distribution shift
that silently degrades it.

---

## 5. The model

**Algorithm.** `HistGradientBoostingClassifier`. Gradient boosting handles the mix of
continuous terrain, rainfall and categorical asset features without a one-hot explosion,
trains in seconds on a laptop, and — critically for a government audience — supports
permutation importance so you can explain any prediction.

**Class imbalance.** Blockages are rare. Negatives are down-sampled to ~25:1 and the
**positive rate is printed**, because a 96% accuracy on a 4%-positive problem is worthless
and looks impressive if you are not careful.

**Metrics chosen for the operational question, not the leaderboard.**
* *ROC-AUC* — overall discrimination.
* *Average precision* — the right summary under heavy imbalance.
* **Precision at 90% recall** — the metric that actually drives cost: if we catch 9 of 10
  failures, what fraction of the corridors we flag really fail? That is how many crews you
  dispatch for nothing.
* *Calibration by decile* — a probability that is not calibrated cannot be thresholded.
  If the predicted and observed bars track each other, a 0.7 cut-off means something.

**Leakage check.** `month` is a legitimate feature (monsoon is real), but if permutation
importance puts `month` far above slope and rainfall, the model has learned "it is July"
instead of "this slope is saturated". Inspect the ranking before quoting any number.

---

## 6. Routing

**Cost.** `cost = free-flow minutes × (1 + 12 × risk)`. The weight is a policy knob: raise
it and the router detours aggressively around anything risky, lower it and it prioritises
distance. Expose it to the control room rather than hard-coding a value in a config file —
the right setting for a medicine convoy is not the right setting for aggregate.

**A\* over Dijkstra.** The heuristic is straight-line distance divided by the fastest road
class in the network. It is admissible because the risk multiplier is ≥ 1, so
`length / MAX_SPEED_KMPH` never overstates the true cost and optimality is preserved. On
long inter-district journeys this expands far fewer vertices.

**Blocked ≠ deleted.** A blocked segment gets a huge-but-finite penalty. When an entire
valley is cut, returning "no route" is useless in a control room; returning a route flagged
`passes_blocked_segment: true` tells them exactly what they need to know.

**Alternates via Yen's K-shortest loopless paths.** Asking for "the second shortest path"
usually returns something sharing 95% of the primary — no use when the primary is buried.
Yen re-runs the search from successive spur points with the used edge forbidden, forcing
each alternative to diverge somewhere different.

**Spur points are capped at 24.** Yen's cost is O(K × spur points × one shortest-path
search). Uncapped on a 160 k-edge graph it becomes the entire latency budget, and routes
past the first few spur points are rarely sensible corridors anyway.

**Candidates must diverge by ≥15%.** Without this filter, Yen returned three "alternates"
for Guwahati → Shillong sharing 96–100% of the primary — micro-reroutes around parallel OSM
edges (dual carriageways, service roads beside a highway) that all drove through the same
landslide. Returning fewer than K routes is then the correct answer, and the router says so
in a `diagnostics` field instead of padding the list.

---

## 7. Offline-first field reporting

The requirement is explicit in the problem statement and it is the part most teams fake.
The design rule is: **the network is never on the critical path for capturing an
observation.**

1. Write to IndexedDB *before* any fetch. A crash, a dead battery or airplane mode loses
   nothing.
2. Flush as one **batched** POST — `/api/field/reports` accepts an array for exactly this.
3. Downscale photos **client-side** to ~800 KB. An 8 MB phone photo over a 2G uplink is a
   failed upload, and a failed upload is a lost observation.
4. Send photos *after* the text reports, so a large file cannot block the report.
5. Service worker caches the app shell (cache-first) and API reads (network-first with a
   cache fallback), so the page opens at zero bars and still shows the last-known risk
   around the officer.
6. Sync on `online`, on `visibilitychange`, and on a timer — the officer never has to
   think about it.

**Reports feed back into the model.** The server snaps each report to the nearest segment
and pins that segment's risk. A confirmed obstruction is stronger evidence than any
forecast, so it must override it immediately rather than waiting for a retrain.

---

## 8. Alerting

Two responsibilities kept separate: `evaluate()` decides *what* is worth telling someone
(with a 30-minute per-segment cooldown, so a segment oscillating around the threshold does
not page a control room on every refresh, and a per-run cap so a bad forecast cannot flood
the feed), and `notify()` decides *how* it reaches them.

Translations are **static templates**, not a translation API, for two reasons: alerts must
still go out when the network is down, and a fixed template means the wording has been
reviewed once by a speaker instead of varying per call.

---

## 9. What changes for production

| Area | Prototype | Production |
|---|---|---|
| Database | SQLite | PostgreSQL + PostGIS, partitioned `gps_ping` |
| Map serving | GeoJSON over the API | Vector tiles, pre-rendered per zoom level |
| Routing speed | A\* + capped Yen | Contraction hierarchies; precomputed district-to-district matrix |
| Model labels | Weak (rainfall thresholds) | GSI NLSM inventory + Bhuvan events + validated field reports |
| Weather | Open-Meteo | IMD whitelisted feed + NCMRWF, with Open-Meteo as fallback |
| Jobs | Called inline from the API | Celery/RQ workers on a schedule, with a dead-letter queue |
| Auth | none | RBAC: field officer / district control room / state admin / read-only, plus an audit log per alert |
| Notifications | console | MSG91 or CDAC UMS for SMS, FCM for push, email for the daily digest |
| Observability | print statements | Prometheus metrics + structured logs; alert on scoring-job failure, not just on roads |

---

## 10. Things that will bite you

* **Do not cache the risk map in a global dict across processes.** It works with one uvicorn
  worker and silently serves stale data with four. Use Redis.
* **Undirected graph.** OSM `oneway` tagging on NER hill roads is sparse and unreliable, so
  the graph is treated as undirected. That is right for accessibility planning and wrong for
  turn-by-turn navigation — say which one you are building.
* **OSM completeness varies.** A district with poor OSM coverage looks *more* connected than
  it is, because missing roads cannot fail. Check coverage per district before publishing a
  connectivity index.
* **The `ref` tag is how you find the highways.** `NH-27`, `NH-02`, `NH-29` — filter on it to
  get the corridors that actually matter, rather than drowning in village roads.
