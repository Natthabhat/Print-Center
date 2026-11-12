// Print Center service worker: shows print notifications. It does not cache pages.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {body: event.data ? event.data.text() : ''}; }
  event.waitUntil(self.registration.showNotification(data.title || 'Print Center', {
    body: data.body || '', tag: data.tag || undefined, renotify: Boolean(data.tag),
    icon: '/icons/icon-192.png', data: {url: data.url || '/'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
    const open = windows.find(w => new URL(w.url).origin === self.location.origin && 'focus' in w);
    return open ? open.focus() : self.clients.openWindow(url);
  }));
});
