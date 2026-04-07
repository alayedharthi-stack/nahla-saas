import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

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
  plugins: [react()],
  server: {
    port: 3000,
    headers: SALLA_IFRAME_HEADERS,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  preview: {
    // Production preview server (used by Railway to serve the built SPA)
    host: true,
    headers: SALLA_IFRAME_HEADERS,
  },
})
