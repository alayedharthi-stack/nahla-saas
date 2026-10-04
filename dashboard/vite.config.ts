import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { checkReviewApiBase, describeReviewApiBaseFailure } from './src/lib/reviewEnvironmentPolicy'

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

export default defineConfig(({ mode }) => {
  // Catalog review environment build (VITE_NAHLA_CATALOG_REVIEW_ENV=1): the API
  // base must be explicit and must not be production. Vite gives process.env
  // precedence over .env.production, so the review service's variables win;
  // when they are missing the build FAILS here instead of shipping a bundle
  // that silently talks to api.nahlah.ai.
  const bakedEnv = { ...loadEnv(mode, process.cwd(), ''), ...process.env } as Record<string, string | undefined>
  const reviewCheck = checkReviewApiBase(bakedEnv)
  if (reviewCheck.enabled && !reviewCheck.ok) {
    throw new Error('[review-env] build refused: ' + describeReviewApiBaseFailure(reviewCheck))
  }
  if (reviewCheck.enabled) {
    // eslint-disable-next-line no-console
    console.log(`[review-env] building against review API host ${reviewCheck.host}`)
  }

  return {
  define: {
    __NAHLA_BUILD_STAMP__: JSON.stringify(buildStamp),
  },
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
    host: true,
    allowedHosts: true,
    headers: SALLA_IFRAME_HEADERS,
  },
  }
})
