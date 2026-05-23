/* ─────────────────────────────────────────────────────────────
   SOHANA Service Worker
   Strategy:
     • Static assets  → Cache-first  (fast, versioned)
     • API routes     → Network-first (always fresh data)
     • Pages          → Network-first with offline fallback
     • Offline page   → Pre-cached on install
   ───────────────────────────────────────────────────────────── */

// INCREMENT THIS on every deploy that changes static assets.
// Railway sets this automatically if you wire it to BUILD_ID — see README.
const CACHE_VERSION = 'sohana-v1';

const STATIC_CACHE  = `${CACHE_VERSION}-static`;
const DYNAMIC_CACHE = `${CACHE_VERSION}-dynamic`;

// Pre-cache these on install — the offline page must always be available
const PRECACHE_URLS = [
  '/offline',
  '/static/css/sohana.css',
  '/static/js/app.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// These URL prefixes always go network-first and are never cached
const NEVER_CACHE = [
  '/api/',
  '/admin/',
  '/auth',
  '/.well-known/',
];

// ── Install: pre-cache shell assets ──────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => {
      return cache.addAll(PRECACHE_URLS).catch(err => {
        // Don't fail install if some assets 404 (e.g. icons not yet added)
        console.warn('[SW] Pre-cache partial failure:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

// ── Activate: remove old cache versions ──────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== STATIC_CACHE && key !== DYNAMIC_CACHE)
          .map(key => {
            console.log('[SW] Deleting old cache:', key);
            return caches.delete(key);
          })
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: routing logic ──────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin requests
  if (url.origin !== location.origin) return;

  // Skip non-GET requests (POST, PUT, etc.) — let them go straight to network
  if (request.method !== 'GET') return;

  // API, admin, and auth routes → always network-first, never cache
  if (NEVER_CACHE.some(prefix => url.pathname.startsWith(prefix))) {
    event.respondWith(fetch(request));
    return;
  }

  // Static assets (/static/) → cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // All other pages → network-first with offline fallback
  event.respondWith(networkFirstWithFallback(request));
});

// ── Strategy: Cache-first ─────────────────────────────────────
async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(STATIC_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Static asset not available and not cached — return empty 404
    return new Response('Asset unavailable offline', { status: 503 });
  }
}

// ── Strategy: Network-first with offline fallback ─────────────
async function networkFirstWithFallback(request) {
  try {
    const response = await fetch(request);

    // Cache successful page responses for fallback
    if (response.ok) {
      const cache = await caches.open(DYNAMIC_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Network failed — try cache, then offline page
    const cached = await caches.match(request);
    if (cached) return cached;

    // Serve the pre-cached offline page for navigation requests
    if (request.mode === 'navigate') {
      const offline = await caches.match('/offline');
      if (offline) return offline;
    }

    return new Response('You are offline and this page has not been cached.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain' },
    });
  }
}
