/* Serves each finished report at a real URL — /reports/<host>-site-audit.pdf —
   so the browser's built-in PDF viewer names the download after the site
   instead of the random id a blob: URL carries. The page stores the PDF in
   Cache Storage; this worker answers requests for it from there. Nothing is
   fetched from the server, which keeps the service stateless. */

const CACHE = 'audit-reports';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/reports/')) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(url.pathname);
    if (hit) return hit;
    return new Response('This report is no longer available in this browser. Run the audit again.', {
      status: 404,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  })());
});
