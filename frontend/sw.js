/*
 * Service worker for the field app.
 *
 * Strategy: cache-first for the app shell (so the page opens with zero connectivity, which
 * is the common case on a hillside in Arunachal), network-first for API reads (risk data
 * must be fresh when a signal exists), and never cache writes - a report must always reach
 * the server, or sit in IndexedDB until it can.
 */
const CACHE = "ner-field-v1";
const SHELL = ["/field", "/static/vendor/leaflet.js", "/static/vendor/leaflet.css"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;              // writes always go to the network

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API reads: try the network, fall back to whatever we last saw.
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // App shell: cache first so it works offline.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => caches.match("/field")))
  );
});
