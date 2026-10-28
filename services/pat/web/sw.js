/* Pat service worker.
 * Goals: make Pat installable + launch instantly, work as an app shell, and
 * NEVER interfere with live data (chat streaming, image gen, voice, approvals).
 *
 * Strategy:
 *   - Only GET requests are ever handled here. Anything else (POST/PUT, SSE,
 *     streaming) falls straight through to the network untouched.
 *   - /api/* is ALWAYS network — never cached, never intercepted beyond pass-through.
 *   - Navigations (opening the app) are network-first: you always get the freshest
 *     UI when online, and the cached shell only when the server is unreachable.
 *   - Static icons/manifest are cache-first (they rarely change; bump CACHE to refresh).
 */
const CACHE = "pat-shell-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-512-maskable.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
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
  if (req.method !== "GET") return;                    // POST/stream -> network
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;     // cross-origin -> network
  if (url.pathname.startsWith("/api/")) return;        // live data -> never cache

  // App shell / navigations: network-first, fall back to cached shell offline.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/").then((r) => r || caches.match(req)))
    );
    return;
  }

  // Static assets (icons, manifest): cache-first, refresh in background.
  e.respondWith(
    caches.match(req).then((cached) => {
      const net = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => cached);
      return cached || net;
    })
  );
});
