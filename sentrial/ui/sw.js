// Sentrial service worker — handles web push + minimal offline shell cache.

const CACHE = 'sentrial-v1';
const SHELL = ['/ui/', '/icon.svg', '/manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // Only cache-first for the shell; everything else is network.
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (SHELL.includes(url.pathname) || url.pathname === '/ui' || url.pathname.startsWith('/ui/')) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(r => {
        const clone = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone)).catch(() => {});
        return r;
      }).catch(() => cached))
    );
  }
});

self.addEventListener('push', (e) => {
  let data = { title: 'Sentrial', body: '' };
  try { data = e.data ? e.data.json() : data; } catch { data.body = (e.data && e.data.text()) || ''; }
  e.waitUntil(
    self.registration.showNotification(data.title || 'Sentrial', {
      body: data.body || '',
      icon: '/icon.svg',
      badge: '/icon.svg',
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
