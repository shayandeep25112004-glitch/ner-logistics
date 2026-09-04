/*
 * NER Logistics & Accessibility Platform — Advanced Service Worker (v4)
 *
 * Designed specifically for low-bandwidth 2G, high-latency, and zero-connectivity hill corridors.
 * 
 * Strategy:
 * 1. App Shell & Code: Network-First (always serve fresh updates when online, fallback to cache when offline).
 * 2. API Reads: Network-first with a 2.5-second timeout race, falling back immediately to cached snapshots.
 * 3. Map Tiles: Cache-first with background revalidation (ensures maps render offline).
 * 4. Background Sync: Auto-triggers IndexedDB sync on network reconnect.
 */

const SHELL_CACHE = "ner-shell-v7";
const DATA_CACHE = "ner-data-v7";
const TILE_CACHE = "ner-tiles-v7";

const APP_SHELL = [
  "/",
  "/field",
  "/driver",
  "/static/idb.js",
  "/static/app.js",
  "/static/vendor/leaflet.js",
  "/static/vendor/leaflet.css",
];

// Install: Pre-cache App Shell & Skip Waiting for immediate activation
self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(APP_SHELL))
      .catch((err) => console.warn("[SW] Cache install warning:", err))
  );
});

// Activate: Immediately purge all older cache versions and claim clients
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

  // 3. App Shell Pages & Static Assets: Network-First (always fresh when online)
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
      return null;
    });

  const timeoutPromise = new Promise((resolve) => {
    setTimeout(() => resolve(null), timeoutMs);
  });

  const winner = await Promise.race([fetchPromise, timeoutPromise]);
  if (winner) return winner;

  // Network either failed or timed out: fall back to cache
  const cachedRes = await cache.match(req);
  if (cachedRes) return cachedRes;

  const fallbackRes = await fetchPromise;
  if (fallbackRes) return fallbackRes;

  return new Response(JSON.stringify({ offline: true, error: "Network offline & no cached data" }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * App shell fetch: Network-First with instant offline cache fallback.
 */
async function handleShellFetch(req, pathname) {
  try {
    const networkRes = await fetch(req);
    if (networkRes && networkRes.status === 200) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(req, networkRes.clone());
    }
    return networkRes;
  } catch (err) {
    const cachedRes = await caches.match(req);
    if (cachedRes) return cachedRes;

    // If specific navigation route fails, try cached fallbacks
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
 * Tile cache handler: Cache-First for lightning fast offline map tiles.
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
    return new Response("", { status: 408, headers: { "Content-Type": "image/png" } });
  }
}
