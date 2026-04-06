const CACHE_NAME = 'nahlah-v1'
const STATIC_ASSETS = ['/', '/logo.png', '/manifest.json']

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  )
  self.skipWaiting()
})

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', event => {
  // Only cache GET requests; pass API calls through
  if (event.request.method !== 'GET') return
  if (event.request.url.includes('/api/') || event.request.url.includes('api.nahlah')) return

  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  )
})
