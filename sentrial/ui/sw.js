// Sentrial service worker — web push + thin offline shell.
// Network-first for HTML so UI changes propagate immediately.
// Bump CACHE when invalidating all caches.

const CACHE = 'sentrial-v3';
const STATIC = ['/icon.svg', '/manifest.json', '/ui/logo.png', '/ui/logo-192.png', '/ui/logo-64.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Never cache API or bootstrap responses
  if (url.pathname.startsWith('/api/') || url.pathname === '/ui/bootstrap') return;

  // Static assets — cache-first
  if (STATIC.includes(url.pathname)) {
    e.respondWith(
      caches.match(req).then(c => c || fetch(req).then(r => {
        const clone = r.clone();
        caches.open(CACHE).then(cache => cache.put(req, clone)).catch(() => {});
        return r;
      }))
    );
    return;
  }

  // HTML and JS/CSS — network-first so updates land on reload
  if (url.pathname === '/ui/' || url.pathname === '/ui' || url.pathname.startsWith('/ui/')) {
    e.respondWith(
      fetch(req).then(r => {
        const clone = r.clone();
        caches.open(CACHE).then(cache => cache.put(req, clone)).catch(() => {});
        return r;
      }).catch(() => caches.match(req))
    );
  }
});

self.addEventListener('push', (e) => {
  let data = { title: 'Sentrial', body: '' };
  try { data = e.data ? e.data.json() : data; } catch { data.body = (e.data && e.data.text()) || ''; }
  e.waitUntil(
    self.registration.showNotification(data.title || 'Sentrial', {
      body: data.body || '',
      icon: '/ui/logo-192.png',
      badge: '/ui/logo-64.png',
      tag: data.tag || 'sentrial-default',
      data: data,
      requireInteraction: false,
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/ui/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) { if (c.url.includes('/ui') && 'focus' in c) return c.focus(); }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});
