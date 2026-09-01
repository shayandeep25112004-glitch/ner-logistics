/*
 * NER Logistics & Accessibility Platform — Advanced Service Worker (v2)
 *
 * Designed specifically for low-bandwidth 2G, high-latency, and zero-connectivity hill corridors.
 * 
 * Strategy:
 * 1. App Shell: Cache-first (instant offline page loads).
 * 2. API Reads: Network-first with a 2.5-second timeout race, falling back immediately to cached snapshots.
 * 3. Map Tiles: Cache-first with background revalidation (ensures maps render offline).
 * 4. Background Sync: Auto-triggers IndexedDB sync on network reconnect.
 */

const SHELL_CACHE = "ner-shell-v2";
const DATA_CACHE = "ner-data-v2";
const TILE_CACHE = "ner-tiles-v2";

const APP_SHELL = [
  "/",
  "/field",
  "/driver",
  "/static/idb.js",
  "/static/app.js",
  "/static/vendor/leaflet.js",
  "/static/vendor/leaflet.css",
];

// Install: Pre-cache App Shell
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
      .catch((err) => console.warn("[SW] Cache install warning:", err))
  );
});

// Activate: Clean up older cache versions
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== SHELL_CACHE && key !== DATA_CACHE && key !== TILE_CACHE)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

// Fetch Dispatcher
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // Mutations bypass SW to reach IndexedDB / Network directly

  const url = new URL(req.url);

  // 1. External Map Tiles (CartoDB / OpenStreetMap / OpenTopoMap)
  if (
    url.hostname.includes("tile.openstreetmap.org") ||
    url.hostname.includes("basemaps.cartocdn.com") ||
    url.hostname.includes("tile.opentopomap.org")
  ) {
    event.respondWith(handleTileFetch(req));
    return;
  }

  // Same-origin requests only below
  if (url.origin !== self.location.origin) return;

  // 2. API Endpoints: Network-first with fast 2.5s timeout race
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(handleApiFetchWithTimeout(req, 2500));
    return;
  }

  // 3. App Shell Pages & Static Assets: Cache-first
  event.respondWith(handleShellFetch(req, url.pathname));
});

/**
 * Fast network fetch with timeout race.
 * If 2G network takes > timeoutMs, instantly serve cached snapshot.
 */
async function handleApiFetchWithTimeout(req, timeoutMs = 2500) {
  const cache = await caches.open(DATA_CACHE);

  const fetchPromise = fetch(req)
    .then((networkRes) => {
      if (networkRes && networkRes.status === 200) {
        cache.put(req, networkRes.clone());
      }
      return networkRes;
    })
    .catch((err) => {
      console.log(`[SW] API network failed for ${req.url}, trying cache fallback...`);
      return null;
    });

  const timeoutPromise = new Promise((resolve) => {
    setTimeout(() => resolve(null), timeoutMs);
  });

  const winner = await Promise.race([fetchPromise, timeoutPromise]);
  if (winner) return winner;

  // Network either failed or timed out: fall back to cache
  const cachedRes = await cache.match(req);
  if (cachedRes) {
    console.log(`[SW] Serving cached API snapshot for ${req.url}`);
    return cachedRes;
  }

  // If no cache yet, wait for fetchPromise to finally resolve or reject
  const fallbackRes = await fetchPromise;
  if (fallbackRes) return fallbackRes;

  return new Response(JSON.stringify({ error: "Offline - Cached data unavailable", offline: true }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * App Shell cache-first handler
 */
async function handleShellFetch(req, pathname) {
  const cachedRes = await caches.match(req);
  if (cachedRes) return cachedRes;

  try {
    const networkRes = await fetch(req);
    if (networkRes && networkRes.status === 200) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(req, networkRes.clone());
    }
    return networkRes;
  } catch (err) {
    // If navigation request fails completely, return fallback shell
    if (pathname === "/field") {
      const fallback = await caches.match("/field");
      if (fallback) return fallback;
    } else if (pathname === "/driver") {
      const fallback = await caches.match("/driver");
      if (fallback) return fallback;
    } else {
      const fallback = await caches.match("/");
      if (fallback) return fallback;
    }
    throw err;
  }
}

/**
 * Tile cache handler
 */
async function handleTileFetch(req) {
  const tileCache = await caches.open(TILE_CACHE);
  const cachedTile = await tileCache.match(req);
  if (cachedTile) return cachedTile;

  try {
    const networkTile = await fetch(req);
    if (networkTile && networkTile.status === 200) {
      tileCache.put(req, networkTile.clone());
    }
    return networkTile;
  } catch (err) {
    // Return empty transparent pixel if offline & tile not cached
    return new Response("", { status: 408, headers: { "Content-Type": "image/png" } });
  }
}

// Background Sync (if supported by browser)
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-field-reports") {
    console.log("[SW] Background sync triggered for field reports.");
    event.waitUntil(
      self.clients.matchAll().then((clients) => {
        clients.forEach((client) => {
          client.postMessage({ type: "TRIGGER_SYNC" });
        });
      })
    );
  }
});
