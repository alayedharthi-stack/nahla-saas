const CACHE_NAME = 'nahlah-v3'

// Only pre-cache static binary assets (not the HTML shell)
const STATIC_ASSETS = ['/logo.png', '/manifest.json']

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
  if (event.request.method !== 'GET') return
  // Never cache API calls
  if (event.request.url.includes('/api/') || event.request.url.includes('api.nahlah')) return

  const url = new URL(event.request.url)

  // HTML navigation requests → always network-first so new deployments take effect immediately
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    )
    return
  }

  // Hashed assets (/assets/index-XYZ.js) → cache-first (they never change once deployed)
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(res => {
      if (res.ok) {
        const clone = res.clone()
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone))
      }
      return res
    }))
  )
})
