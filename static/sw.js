const CACHE_VERSION = 'countdepot-mobile-v3';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const STATIC_ASSETS = [
  '/static/offline.html',
  '/static/icons/icon.svg',
  '/static/icons/maskable.svg',
  '/static/vendor/zxing-library-0.21.3.min.js'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => !key.startsWith(CACHE_VERSION)).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/_platform') || url.pathname === '/_stripe/webhook') return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(response => {
        const copy = response.clone();
        caches.open(STATIC_CACHE).then(cache => cache.put(request, copy));
        return response;
      }))
    );
    return;
  }

  event.respondWith(
    fetch(request).catch(() => caches.match('/static/offline.html'))
  );
});
