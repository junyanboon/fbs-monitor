/* FBS Monitor service worker — keep the board readable when the network drops.
 *
 * Junyan reads this from Brazil and on the move; a dead connection used to mean
 * a blank page, which is worse than a board that is honestly an hour old.
 *
 * The whole design turns on one distinction, because getting it backwards is
 * how an offline cache becomes a liar:
 *
 *   THE PAGE          network-first, cache as fallback. Both editions are
 *                     STATIC — the data is spliced into the HTML at build time,
 *                     so a cached page is a cached *edition*, frozen at whatever
 *                     the ~15-min rebuild last published. Serving it cache-first
 *                     would pin the board to an old edition on a perfectly good
 *                     connection.
 *
 *   version.json      NETWORK ONLY. Never cached, never served from cache. It is
 *                     the freshness probe both editions poll every 30 s; a cached
 *                     copy would report the stale edition as current and the page
 *                     would stop reloading onto new ones — silently, because the
 *                     probe swallows errors by design (README rule 11). Offline,
 *                     this fetch must FAIL, so the page keeps rendering the age
 *                     it actually has.
 *
 *   ICONS / MANIFEST  cache-first. They never change within an edition.
 *
 * The page already renders its own data age and marks itself `stale` past 40
 * minutes, so an offline reader sees how old the board is without this worker
 * doing anything extra. Do not add an "offline" banner that contradicts it.
 */

const VERSION = 'fbs-v1';
const SHELL = [
  './',
  './index.html',
  './mobile.html',
  './manifest.webmanifest',
  './manifest-mobile.webmanifest',
  './favicon.svg',
  './icon-192.png',
  './icon-512.png',
  './mobile-icon-192.png',
  './mobile-icon-512.png',
  './apple-touch-icon.png',
  './apple-touch-icon-mobile.png',
];

self.addEventListener('install', event => {
  // addAll rejects the whole batch if any one file 404s, which would leave the
  // worker uninstalled and the board with no offline copy at all. Each file is
  // fetched on its own so a renamed icon costs that icon, not the cache.
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    await Promise.all(SHELL.map(url =>
      cache.add(url).catch(() => {/* missing asset must not fail the install */})));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // never proxy third parties

  // The freshness probe. Must reach the network or fail — see the header note.
  if (url.pathname.endsWith('/version.json')) return;

  // The claim server (server/app.py): identity, claims, sign-in. Live answers
  // or nothing — a cached /api/me would keep a signed-out reader "signed in",
  // and a cached claims list would re-offer a shift someone took. The pages
  // keep their own last-known identity and an outbox in localStorage for the
  // offline case, so the worker has no job here.
  if (url.pathname.includes('/api/') || url.pathname.endsWith('/login')
      || url.pathname.endsWith('/account') || url.pathname.endsWith('/open-shifts.json')) return;

  const isPage = req.mode === 'navigate' || url.pathname.endsWith('.html')
    || url.pathname.endsWith('/');

  if (isPage) {
    // Cache under the bare path, dropping the query. checkEdition() reloads onto
    // `mobile.html?e=<edition stamp>` to defeat GitHub Pages' 10-minute cache
    // (README rule 11), and a Request with a query is a DIFFERENT cache key — so
    // keying on the raw request would store a fresh copy of the page on every
    // rebuild and never hit any of them again offline. One key per page: the
    // newest edition seen is the one you get when the network is gone.
    const key = url.origin + url.pathname;
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        // Only cache a real edition. Caching a 404 or a GitHub Pages error page
        // would hand that back as "the board" the next time the network drops.
        if (fresh && fresh.ok) {
          (await caches.open(VERSION)).put(key, fresh.clone());
        }
        return fresh;
      } catch (e) {
        const cached = await caches.match(key) || await caches.match('./mobile.html')
          || await caches.match('./index.html');
        if (cached) return cached;
        throw e;
      }
    })());
    return;
  }

  // Icons, manifests: cache-first, refreshed in the background.
  event.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    const fresh = await fetch(req);
    if (fresh && fresh.ok) (await caches.open(VERSION)).put(req, fresh.clone());
    return fresh;
  })());
});
