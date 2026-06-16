// SportsBrain Service Worker — Web Push Handler
// Wird vom PWA-Frontend registriert (siehe index.html → registerPush()).
//
// Verarbeitet zwei Events:
//   - "push":              eingehende Push-Notification anzeigen
//   - "notificationclick": Notification angeklickt → PWA öffnen / fokussieren
//
// Wir machen hier KEIN Caching der App-Shell (PWA bleibt online-first via
// Cloudflare KV). Der Service Worker existiert nur für Web Push.

const SW_VERSION = '2026-06-16-v2-force-reload';

self.addEventListener('install', (event) => {
  // Sofort aktivieren — kein Wait auf Tab-Reload
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await self.clients.claim();
    // Bei jeder SW-Update force-reload aller controlled Clients —
    // löst das hartnäckige iOS-PWA-HTML-Caching.
    const allClients = await self.clients.matchAll({ type: 'window' });
    for (const client of allClients) {
      try {
        if ('navigate' in client) {
          await client.navigate(client.url);
        }
      } catch {}
    }
  })());
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: 'SportsBrain', body: event.data ? event.data.text() : '' };
  }

  const title = payload.title || 'SportsBrain';
  const options = {
    body:        payload.body || '',
    icon:        payload.icon || '/sportsbrain/icon-192.png',
    badge:       payload.badge || '/sportsbrain/icon-192.png',
    tag:         payload.tag || 'sportsbrain',
    renotify:    payload.renotify !== false,
    requireInteraction: !!payload.require,
    data: {
      url:       payload.url || '/sportsbrain/',
      kind:      payload.kind || 'generic',
      ts:        Date.now(),
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/sportsbrain/';
  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // PWA bereits offen? Fokussieren + navigieren
    for (const client of allClients) {
      if (client.url.includes('/sportsbrain') && 'focus' in client) {
        await client.focus();
        if ('navigate' in client) {
          try { await client.navigate(targetUrl); } catch {}
        }
        return;
      }
    }
    // Sonst neuen Tab öffnen
    if (self.clients.openWindow) {
      await self.clients.openWindow(targetUrl);
    }
  })());
});
