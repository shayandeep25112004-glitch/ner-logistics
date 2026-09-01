/**
 * NER Logistics & Accessibility Platform — Offline-First IndexedDB Layer
 * Zero-dependency robust storage engine for zero-connectivity & low-bandwidth hill operations.
 */

class NEROfflineDB {
  constructor() {
    this.dbName = "ner_offline_db";
    this.version = 1;
    this.db = null;
    this._initPromise = null;
    this.syncListeners = [];
  }

  async init() {
    if (this.db) return this.db;
    if (this._initPromise) return this._initPromise;

    this._initPromise = new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        console.warn("[NER IDB] IndexedDB not supported on this browser.");
        return resolve(null);
      }

      const request = indexedDB.open(this.dbName, this.version);

      request.onupgradeneeded = (event) => {
        const db = event.target.result;

        // 1. Field reports queue
        if (!db.objectStoreNames.contains("reports_queue")) {
          const reportStore = db.createObjectStore("reports_queue", {
            keyPath: "client_uuid",
          });
          reportStore.createIndex("status", "status", { unique: false });
          reportStore.createIndex("captured_at", "captured_at", { unique: false });
        }

        // 2. Cached risk snapshots & district status
        if (!db.objectStoreNames.contains("cached_risk")) {
          db.createObjectStore("cached_risk", { keyPath: "key" });
        }

        // 3. Cached vehicle routes & alternates
        if (!db.objectStoreNames.contains("cached_routes")) {
          db.createObjectStore("cached_routes", { keyPath: "route_id" });
        }

        // 4. Offline Map Tiles / metadata
        if (!db.objectStoreNames.contains("tile_cache")) {
          db.createObjectStore("tile_cache", { keyPath: "tile_url" });
        }
      };

      request.onsuccess = (event) => {
        this.db = event.target.result;
        console.log("[NER IDB] Database initialized successfully.");
        resolve(this.db);
      };

      request.onerror = (event) => {
        console.error("[NER IDB] Error opening database:", event.target.error);
        reject(event.target.error);
      };
    });

    return this._initPromise;
  }

  // -------------------------------------------------------------------------
  // Field Reports Queue Operations
  // -------------------------------------------------------------------------

  /**
   * Save a field report into IndexedDB immediately with 'pending' status.
   */
  async saveReport(report) {
    await this.init();
    if (!this.db) return null;

    const entry = {
      client_uuid: report.client_uuid || "rep-" + Date.now() + "-" + Math.random().toString(36).slice(2, 9),
      lat: Number(report.lat),
      lon: Number(report.lon),
      location_name: report.location_name || "",
      category: report.category || "landslide",
      severity: Number(report.severity) || 3,
      note: report.note || "",
      reported_by: report.reported_by || "Field Officer",
      device_id: report.device_id || "web-client",
      photo_data: report.photo_data || null,
      ai_verified: report.ai_verified || null,
      status: "pending", // 'pending' | 'syncing' | 'synced' | 'failed'
      captured_at: report.captured_at || new Date().toISOString(),
      synced_at: null,
      retry_count: 0,
      last_error: null,
    };

    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["reports_queue"], "readwrite");
      const store = tx.objectStore("reports_queue");
      const req = store.put(entry);

      req.onsuccess = () => {
        this._notifySyncListeners("saved", entry);
        resolve(entry);
      };
      req.onerror = (e) => reject(e.target.error);
    });
  }

  /**
   * Retrieve all reports from local database (newest first).
   */
  async getAllReports() {
    await this.init();
    if (!this.db) return [];

    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["reports_queue"], "readonly");
      const store = tx.objectStore("reports_queue");
      const req = store.getAll();

      req.onsuccess = () => {
        const items = req.result || [];
        items.sort((a, b) => new Date(b.captured_at) - new Date(a.captured_at));
        resolve(items);
      };
      req.onerror = (e) => reject(e.target.error);
    });
  }

  /**
   * Get count of unsynced (pending/failed) reports.
   */
  async getUnsyncedCount() {
    await this.init();
    if (!this.db) return 0;

    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["reports_queue"], "readonly");
      const store = tx.objectStore("reports_queue");
      const req = store.getAll();

      req.onsuccess = () => {
        const items = req.result || [];
        const unsynced = items.filter((i) => i.status === "pending" || i.status === "failed");
        resolve(unsynced.length);
      };
      req.onerror = () => resolve(0);
    });
  }

  /**
   * Mark report status (e.g., 'syncing', 'synced', 'failed')
   */
  async updateReportStatus(client_uuid, status, extra = {}) {
    await this.init();
    if (!this.db) return;

    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["reports_queue"], "readwrite");
      const store = tx.objectStore("reports_queue");
      const getReq = store.get(client_uuid);

      getReq.onsuccess = () => {
        const item = getReq.result;
        if (!item) return resolve(null);

        item.status = status;
        if (status === "synced") {
          item.synced_at = new Date().toISOString();
        }
        if (extra.last_error) item.last_error = extra.last_error;
        if (typeof extra.retry_count === "number") item.retry_count = extra.retry_count;

        const putReq = store.put(item);
        putReq.onsuccess = () => {
          this._notifySyncListeners(status, item);
          resolve(item);
        };
        putReq.onerror = (e) => reject(e.target.error);
      };
      getReq.onerror = (e) => reject(e.target.error);
    });
  }

  // -------------------------------------------------------------------------
  // Batch Synchronizer Engine
  // -------------------------------------------------------------------------

  /**
   * Attempt to push all pending reports to backend API.
   */
  async syncPendingReports() {
    if (!navigator.onLine) {
      console.log("[NER IDB] Device is currently offline. Skipping sync.");
      return { success: false, synced: 0, reason: "offline" };
    }

    await this.init();
    if (!this.db) return { success: false, synced: 0 };

    const reports = await this.getAllReports();
    const pending = reports.filter((r) => r.status === "pending" || r.status === "failed");

    if (pending.length === 0) {
      return { success: true, synced: 0, message: "Queue is empty." };
    }

    console.log(`[NER IDB] Starting sync for ${pending.length} pending report(s)...`);
    let syncedCount = 0;

    for (const item of pending) {
      await this.updateReportStatus(item.client_uuid, "syncing");
      try {
        const payload = {
          lat: item.lat,
          lon: item.lon,
          category: item.category,
          severity: item.severity,
          note: item.note,
          device_id: item.device_id,
          reported_by: item.reported_by,
          captured_at: item.captured_at,
          photo_data: item.photo_data,
          client_uuid: item.client_uuid,
        };

        const res = await fetch("/api/field-report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (res.ok) {
          await this.updateReportStatus(item.client_uuid, "synced");
          syncedCount++;
        } else {
          const errText = await res.text();
          await this.updateReportStatus(item.client_uuid, "failed", {
            last_error: `HTTP ${res.status}: ${errText.slice(0, 100)}`,
            retry_count: (item.retry_count || 0) + 1,
          });
        }
      } catch (err) {
        console.warn(`[NER IDB] Network error syncing report ${item.client_uuid}:`, err);
        await this.updateReportStatus(item.client_uuid, "failed", {
          last_error: err.message || "Network request failed",
          retry_count: (item.retry_count || 0) + 1,
        });
      }
    }

    this._notifySyncListeners("sync_complete", { synced: syncedCount, total: pending.length });
    return { success: true, synced: syncedCount, total: pending.length };
  }

  // -------------------------------------------------------------------------
  // Snapshot Caching (Risk, District Matrix, Routes)
  // -------------------------------------------------------------------------

  async setCache(key, data) {
    await this.init();
    if (!this.db) return;
    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["cached_risk"], "readwrite");
      const store = tx.objectStore("cached_risk");
      const req = store.put({ key, data, timestamp: Date.now() });
      req.onsuccess = () => resolve(true);
      req.onerror = (e) => reject(e.target.error);
    });
  }

  async getCache(key) {
    await this.init();
    if (!this.db) return null;
    return new Promise((resolve) => {
      const tx = this.db.transaction(["cached_risk"], "readonly");
      const store = tx.objectStore("cached_risk");
      const req = store.get(key);
      req.onsuccess = () => resolve(req.result ? req.result.data : null);
      req.onerror = () => resolve(null);
    });
  }

  async saveRoute(routeId, routeData) {
    await this.init();
    if (!this.db) return;
    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(["cached_routes"], "readwrite");
      const store = tx.objectStore("cached_routes");
      const req = store.put({ route_id: routeId, data: routeData, saved_at: Date.now() });
      req.onsuccess = () => resolve(true);
      req.onerror = (e) => reject(e.target.error);
    });
  }

  async getRoute(routeId) {
    await this.init();
    if (!this.db) return null;
    return new Promise((resolve) => {
      const tx = this.db.transaction(["cached_routes"], "readonly");
      const store = tx.objectStore("cached_routes");
      const req = store.get(routeId);
      req.onsuccess = () => resolve(req.result ? req.result.data : null);
      req.onerror = () => resolve(null);
    });
  }

  // -------------------------------------------------------------------------
  // Event & Listener Subscriptions
  // -------------------------------------------------------------------------

  onSyncEvent(callback) {
    this.syncListeners.push(callback);
  }

  _notifySyncListeners(event, data) {
    this.syncListeners.forEach((fn) => {
      try {
        fn(event, data);
      } catch (e) {
        console.error("[NER IDB] Listener error:", e);
      }
    });
  }
}

// Global Singleton Instance
window.nerDB = new NEROfflineDB();
window.addEventListener("DOMContentLoaded", () => {
  window.nerDB.init();

  // Auto-sync when internet comes back online
  window.addEventListener("online", () => {
    console.log("[NER IDB] Network connection restored! Initiating background queue sync...");
    window.nerDB.syncPendingReports();
  });
});
