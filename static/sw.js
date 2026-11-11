/* Service worker: the app shell works offline; API calls always go to the network. */
const CACHE = 'mapo-shell-v2';
const SHELL = ['/', '/static/app.css', '/static/core.js', '/static/pages.js', '/static/admin.js',
               '/static/icon-192.png', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;   // never cache data
  if (url.origin !== location.origin) return;                                   // fonts: let the browser handle it
  // network first so deploys show up immediately; fall back to the cached shell when offline
  e.respondWith(fetch(e.request).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match(e.request).then(r => r || caches.match('/'))));
});
