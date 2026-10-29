/* ---------------------------------------------------------------------------
   Web cache: a service worker that keeps the last good copy of the front end.

   Every request for the app's own files (index.html, the scripts, styles.css)
   still goes to the network first, exactly as it would without this file. The
   server sends them with Cache-Control: no-cache (RevalidatedStaticFiles in
   safety/api/main.py), so the browser checks the ETag on every load and gets a
   cheap 304 when nothing changed. The copy that comes back is also stored here.

   When the network fails -- a dropped mobile connection, or the few seconds of
   502 while a deploy restarts the API -- the stored copy is served instead, so
   the page still opens and shows its own "could not load" message for the data
   rather than the browser's "site can't be reached" page.

   Network first, never cache first, on purpose: a cached app.js answering ahead
   of the network is exactly the old-script-against-new-page skew that no-cache
   was added to prevent. The cache here is only ever a fallback.

   Deliberately not touched:
   - /api/ -- the data stays live. The server has its own cache with its own
     invalidation (cached_json in safety/api/main.py); a second copy here would
     be a second rule to keep in sync and an easy way to show stale incidents.
   - unpkg (MapLibre, h3-js) and every other origin. A service worker's own
     fetches are held to the CSP it was served with (safety/api/security.py),
     and connect-src does not list unpkg, so caching those here would fail.
     They pass straight through to the browser, which already keeps them in its
     HTTP cache: they are exact pinned versions, checked with SRI.
   - FastAPI's /docs, /redoc and /openapi.json.

   Adding a front-end file needs no change here: anything same-origin is stored
   the first time it loads. SHELL only decides what is stored up front, before
   the first full page load, so list new scripts there too when convenient.

   Removing this later: deleting sw.js is not enough, because a browser that
   already installed it keeps it when the update check 404s. Ship a sw.js whose
   activate handler calls self.registration.unregister() and drop the
   sw-register.js tag from index.html. Until then it is harmless either way --
   with the network up, every response is the network's.
   --------------------------------------------------------------------------- */

/* Only used to find and delete this worker's caches. Network first refreshes
   the stored copy on every online load, so a deploy never needs a bump here. */
const CACHE_NAME = "safety-app-shell-v2";
// "shell-", not just "safety-app-": the dev-only copies under /classic/ and
// /testing/ keep their own caches (safety-app-classic-*, safety-app-testing-*)
// on the same origin, and this worker must not sweep them away.
const CACHE_PREFIX = "safety-app-shell-";

const SHELL = [
  "./",
  "./index.html",
  "./styles.css",
  "./html.js",
  "./windows.js",
  "./hours.js",
  "./app.js",
  "./sw-register.js",
];

/* Gateway errors: nginx answering because the API behind it is restarting (a
   deploy) or down. Worth falling back for. Any other status is the server's
   real answer and is passed through untouched. */
const GATEWAY_ERRORS = new Set([502, 503, 504]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // One add per file, not addAll: addAll is all-or-nothing, so a single
      // renamed file would leave the fallback empty. A missing entry here is
      // filled in by the fetch handler the first time that file loads.
      Promise.allSettled(
        SHELL.map((url) => cache.add(new Request(url, { cache: "no-cache" })))
      )
    )
  );
  // Nothing here can serve a stale page while the network is up, so there is
  // no reason to wait for every open tab to close before taking over.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

function handledHere(request) {
  if (request.method !== "GET") return false;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;
  const path = url.pathname;
  if (path.startsWith("/api/")) return false;
  if (path === "/docs" || path.startsWith("/docs/") || path === "/redoc") return false;
  if (path === "/openapi.json") return false;
  return true;
}

/* The page keeps its state in the query string (?city=...&window=..., see
   history.replaceState in app.js), so a page load matches the stored page
   whatever the query, and falls back to "/" itself. Scripts and styles are
   matched exactly. */
async function storedCopy(cache, request) {
  if (request.mode === "navigate") {
    return (
      (await cache.match(request, { ignoreSearch: true })) ||
      (await cache.match("./"))
    );
  }
  return cache.match(request);
}

async function networkFirst(event) {
  const { request } = event;
  const cache = await caches.open(CACHE_NAME);

  let response;
  try {
    response = await fetch(request);
  } catch (err) {
    // Offline, DNS failure, connection refused: the network gave no answer.
    const stored = await storedCopy(cache, request);
    if (stored) return stored;
    throw err;
  }

  if (GATEWAY_ERRORS.has(response.status)) {
    const stored = await storedCopy(cache, request);
    return stored || response;
  }

  // Only complete, same-origin, non-redirected answers are worth keeping. A
  // stored redirect is refused by the browser when replayed for a page load.
  if (response.status === 200 && response.type === "basic" && !response.redirected) {
    event.waitUntil(cache.put(request, response.clone()));
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  if (!handledHere(event.request)) return; // the browser handles it as if this file did not exist
  event.respondWith(networkFirst(event));
});
