"""SQLite storage layer.

We use plain SQLite for the prototype because it needs zero setup on a student laptop.
The schema is written so it ports 1:1 to PostgreSQL + PostGIS for production: every
coordinate pair becomes a `geometry(Point,4326)` column and every distance query becomes
a spatial index lookup. The migration notes live in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- One row per road-graph vertex (shared nodes between OSM ways are merged).
CREATE TABLE IF NOT EXISTS node (
    id        INTEGER PRIMARY KEY,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    ele_m     REAL,
    slope_deg REAL,
    curvature REAL,
    tri       REAL,               -- terrain ruggedness index (std-dev of neighbour elevations)
    state     TEXT
);

CREATE TABLE IF NOT EXISTS edge (
    id          TEXT PRIMARY KEY, -- "w<osm_way_id>#<n1>-<n2>"
    way_id      INTEGER,
    node_a      INTEGER NOT NULL,
    node_b      INTEGER NOT NULL,
    highway     TEXT,
    ref         TEXT,             -- NH-27, SH-14, ...
    name        TEXT,
    surface     TEXT,
    is_bridge   INTEGER DEFAULT 0,
    is_tunnel   INTEGER DEFAULT 0,
    is_ford     INTEGER DEFAULT 0,
    length_m    REAL NOT NULL,
    speed_kmph  REAL NOT NULL,
    base_minutes REAL NOT NULL,   -- free-flow travel time
    base_reliability REAL,
    state       TEXT,
    geom        TEXT,             -- JSON [[lat,lon], ...] shape points, for map rendering
    FOREIGN KEY(node_a) REFERENCES node(id),
    FOREIGN KEY(node_b) REFERENCES node(id)
);

-- Adjacency index: this is what makes Dijkstra fast without a graph library.
CREATE INDEX IF NOT EXISTS idx_edge_a ON edge(node_a);
CREATE INDEX IF NOT EXISTS idx_edge_b ON edge(node_b);
CREATE INDEX IF NOT EXISTS idx_edge_state ON edge(state);

-- Rainfall grid: one compact row per day per grid cell (numpy float32 blob per cell).
CREATE TABLE IF NOT EXISTS weather_grid (
    cell_id     INTEGER,
    lat         REAL,
    lon         REAL,
    day         TEXT,             -- YYYY-MM-DD
    rain_24h    REAL,
    rain_72h    REAL,
    rain_168h   REAL,
    max_intensity REAL,
    PRIMARY KEY (cell_id, day)
);
CREATE INDEX IF NOT EXISTS idx_wx_day ON weather_grid(day);

-- Field reports from the mobile app (also used as ground-truth labels once validated).
CREATE TABLE IF NOT EXISTS field_report (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    category     TEXT NOT NULL,   -- landslide | flood | road_damage | blocked | congestion | clear
    severity     INTEGER DEFAULT 2,
    note         TEXT,
    photo_path   TEXT,
    device_id    TEXT,
    reported_by  TEXT,
    captured_at  TEXT,            -- when the field officer took the observation (may be offline)
    received_at  TEXT NOT NULL,
    validated    INTEGER DEFAULT 0,
    edge_id      TEXT,            -- snapped segment, filled by the server
    FOREIGN KEY(edge_id) REFERENCES edge(id)
);
CREATE INDEX IF NOT EXISTS idx_report_time ON field_report(received_at);

-- Shipment / essential-commodity consignment tracking.
CREATE TABLE IF NOT EXISTS shipment (
    id           TEXT PRIMARY KEY,
    commodity    TEXT NOT NULL,   -- medicine | food | construction | agri_produce | fuel
    origin       TEXT,
    destination  TEXT,
    dest_lat     REAL,
    dest_lon     REAL,
    vehicle_no   TEXT,
    driver_name  TEXT,
    driver_phone TEXT,
    status       TEXT DEFAULT 'in_transit',  -- in_transit|delayed|blocked|delivered|rerouted
    priority     INTEGER DEFAULT 2,          -- 1 = emergency (medicines, relief)
    created_at   TEXT NOT NULL,
    eta_minutes  REAL,
    eta_confidence REAL
);

CREATE TABLE IF NOT EXISTS gps_ping (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    speed_kmph  REAL,
    heading     REAL,
    battery     REAL,
    pinged_at   TEXT NOT NULL,
    FOREIGN KEY(shipment_id) REFERENCES shipment(id)
);
CREATE INDEX IF NOT EXISTS idx_ping_shipment ON gps_ping(shipment_id, pinged_at);

CREATE TABLE IF NOT EXISTS alert (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,    -- road_blocked | delay | high_risk_corridor | delivery_late | geofence
    severity    TEXT NOT NULL,    -- low | moderate | high | critical
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    edge_id     TEXT,
    district    TEXT,
    state       TEXT,
    risk        REAL,
    channel     TEXT,             -- sms | push | email | dashboard
    language    TEXT DEFAULT 'en',
    created_at  TEXT NOT NULL,
    acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_time ON alert(created_at);

-- District connectivity rollup, refreshed by the analytics job and served to the dashboard.
CREATE TABLE IF NOT EXISTS district_status (
    district        TEXT PRIMARY KEY,
    state           TEXT,
    segments_total  INTEGER,
    segments_open   INTEGER,
    segments_at_risk INTEGER,
    segments_blocked INTEGER,
    mean_risk       REAL,
    bridges_at_risk INTEGER,
    network_km      REAL,
    connectivity_index REAL,      -- 0..1, share of network usable
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS model_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trained_at  TEXT NOT NULL,
    n_train     INTEGER,
    n_pos       INTEGER,
    auc         REAL,
    precision_at_recall90 REAL,
    avg_precision REAL,
    features    TEXT,
    notes       TEXT
);
"""


def ensure_db_ready(db_path: str | Path | None = None) -> Path:
    target = Path(db_path or DB_PATH)
    if not target.exists() or target.stat().st_size == 0:
        gz_path = target.with_name(target.name + ".gz")
        if gz_path.exists():
            import gzip
            import shutil
            print(f"Extracting pre-trained database from {gz_path} -> {target}...")
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(gz_path, "rb") as f_in, open(target, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            print(f"Database ready ({round(target.stat().st_size / 1024 / 1024, 1)} MB).")
        else:
            init_schema(target)
    return target


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    # timeout matters: the elevation, weather and scoring jobs can all be writing at once,
    # and SQLite's default is to raise "database is locked" almost immediately. 60 s of
    # patience turns that from a crash into a short wait.
    target = ensure_db_ready(db_path)
    conn = sqlite3.connect(str(target), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


@contextmanager
def db(db_path: str | Path | None = None):
    """`with db() as conn:` - commits on success, rolls back on exception."""
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: str | Path | None = None) -> None:
    target = Path(db_path or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=60.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    ensure_db_ready()
    print(f"schema ready -> {DB_PATH}")
