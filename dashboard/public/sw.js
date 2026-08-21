/* Nahla merchant PWA — v7: bump registration URL + cache name so stale
   pre-choice-screen WhatsApp Connect shells are dropped after deploy. */

const CACHE_NAME = 'nahlah-v7'

const STATIC_ASSETS = ['/logo.png', '/manifest.json']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)),
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))),
    ),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return
  const reqUrl = event.request.url
  if (reqUrl.includes('/api/') || reqUrl.includes('api.nahlah')) return

  const url = new URL(event.request.url)
  const sameOrigin = url.origin === self.location.origin
  const isHtmlNav =
    event.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')

  /* HTML shells & navigations → network-first */
  if (isHtmlNav) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request)),
    )
    return
  }

  /* Bundles & module CSS — network-first, cache as offline fallback only */
  const isAppBundle =
    sameOrigin &&
    (url.pathname.startsWith('/assets/') ||
      url.pathname.endsWith('.js') ||
      url.pathname.endsWith('.css'))

  if (isAppBundle) {
    event.respondWith(
      fetch(event.request)
        .then((networkRes) => {
          if (networkRes.ok) {
            const clone = networkRes.clone()
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone))
          }
          return networkRes
        })
        .catch(() => caches.match(event.request)),
    )
    return
  }

  /* Other same-origin GETs (e.g. fonts copied locally) → stale-while-revalidate lite */
  if (sameOrigin) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const net = fetch(event.request)
          .then((networkRes) => {
            if (networkRes.ok) {
              const clone = networkRes.clone()
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone))
            }
            return networkRes
          })
          .catch(() => cached)
        return cached ?? net
      }),
    )
  }
})
