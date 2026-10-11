import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const buildStamp =
  process.env.RAILWAY_GIT_COMMIT_SHA?.slice(0, 12) ??
  process.env.VERCEL_GIT_COMMIT_REF?.slice(0, 12) ??
  process.env.GITHUB_SHA?.slice(0, 12) ??
  `local-${process.env.npm_package_version ?? '0'}-${Date.now()}`

// Headers that allow Salla to embed app.nahlah.ai inside their iframe viewer
const SALLA_IFRAME_HEADERS = {
  'Content-Security-Policy':
    "frame-ancestors 'self' https://s.salla.sa https://*.salla.sa " +
    "https://store.salla.sa https://apps.salla.sa https://app.nahlah.ai " +
    "https://*.salla.com https://*.salla.store",
  // Remove X-Frame-Options so browsers rely on CSP frame-ancestors instead
  'X-Frame-Options': 'ALLOWALL',
  'X-Content-Type-Options': 'nosniff',
}

export default defineConfig({
  define: {
    __NAHLA_BUILD_STAMP__: JSON.stringify(buildStamp),
  },
  plugins: [react()],
  server: {
    port: 3000,
    // Dev only: Vite prepends its /@vite/client script before index.html's
    // static <meta name="referrer" content="strict-origin">, so the dev server
    // sends the same early policy as a header. index.html's inline script
    // still sets the per-route policy. Shipped HTML needs no header.
    headers: { ...SALLA_IFRAME_HEADERS, 'Referrer-Policy': 'strict-origin' },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  preview: {
    host: true,
    allowedHosts: true,
    headers: SALLA_IFRAME_HEADERS,
  },
})
