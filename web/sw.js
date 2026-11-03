// Service worker: installability and an offline shell, nothing more.
//
// Network-first everywhere, deliberately. This app cannot answer anything
// without the network, so a cache that wins races would buy no capability and
// cost correctness: assets here are unhashed (`blocks.js` is `blocks.js` in
// every build), which is the same reason the server sends `no-cache,
// must-revalidate` from RevalidatingFiles in app/main.py. A cache-first worker
// would reintroduce exactly the stale-module bug that class exists to prevent.
//
// So the cache is a fallback and never a shortcut: online, every response comes
// from the server; offline, the shell still paints and says so itself.

const VERSION = 'v1';
const CACHE = `simplixity-shell-${VERSION}`;

// Just enough to paint without a network. Everything else joins the cache the
// first time it is fetched successfully, which keeps this list from drifting
// out of sync with a module graph it does not own.
const PRECACHE = [
  '/',
  '/static/styles.css',
  // Parser-blocking, so offline it is the difference between painting the shell
  // and waiting on a request that will not answer.
  '/static/js/app/boot.js',
  '/static/js/app/app.js',
  '/favicon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      // Individually, so one 404 cannot fail the whole install the way
      // cache.addAll would.
      .then((cache) => Promise.allSettled(PRECACHE.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // The API is never intercepted. `/api/runs/{id}/events` is an SSE stream, and
  // passing a stream through respondWith is a dependable way to buffer it into
  // uselessness, which is the failure this app just spent a fix on. The rest of
  // /api carries per-chat content that has no business outliving a session in a
  // cache anyway.
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(networkFirst(request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);

  try {
    const response = await fetch(request);
    // Only plain 200s are stored. Partials and redirects replay badly, and an
    // error page cached as the shell would outlive the outage that caused it.
    if (response.status === 200 && response.type === 'basic') {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;

    // A navigation with nothing cached for it still gets the shell: the app
    // renders its own empty state and its own error copy, which is a better
    // offline story than the browser's.
    if (request.mode === 'navigate') {
      const shell = await cache.match('/');
      if (shell) return shell;
    }
    return Response.error();
  }
}
