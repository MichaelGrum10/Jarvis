/* Service worker: makes Jarvis installable and keeps the shell available offline.
 * API calls are never cached — stale calendar data is worse than an error. */

const CACHE = 'jarvis-shell-v21';
// Versioned to match the URLs index.html and app.js actually request. An
// unversioned entry here would warm the cache with a URL nothing asks for.
const V = '22';
const SHELL = [
  '/', `/static/app.js?v=${V}`, `/static/voice.js?v=${V}`, `/static/hud.js?v=${V}`, `/static/galaxy.js?v=${V}`, `/static/reactor.js?v=${V}`, `/static/panels.js?v=${V}`, `/static/speech.js?v=${V}`, `/static/bargein.js?v=${V}`,
  `/static/style.css?v=${V}`, `/static/hud.css?v=${V}`, '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/') || event.request.method !== 'GET') return;
  // /reset exists to escape a bad cache. Serving it from cache would be a joke
  // at the user's expense.
  if (url.pathname === '/reset') return;

  // Scripts are revalidated on every load. They import each other, so a stale
  // one paired with a fresh one breaks the whole app with an import error —
  // and the cached copy is worth far less than the app working.
  const isScript = url.pathname.endsWith('.js');

  event.respondWith(
    fetch(event.request, isScript ? { cache: 'no-cache' } : undefined)
      .then((res) => {
        if (res.ok && url.origin === self.location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match('/')))
  );
});
