// MUST stay first: takes and scrubs a Shopify connection return fragment
// before Sentry, SEO, the runtime boot log, auth or the router read the URL.
import './lib/shopifyConnection/bootCapture'
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { logNahlaRuntimeBoot } from './lib/logRuntimeBoot'
import { initSentry } from './lib/sentry'
import { bootstrapPreferences } from './lib/bootstrapPreferences'
import { applyPublicSeo } from './seo/publicSeo'
import { describeReviewApiBaseFailure, reviewApiBaseCheck } from './lib/reviewEnvironment'

// Apply theme + locale BEFORE React mounts so the first paint matches the
// merchant's preference — eliminates flash-of-wrong-theme and flash-of-wrong-
// direction.  Also consumes the Salla embedded handoff (`?theme=…&lang=…`)
// emitted by SallaEntryScreen's "Open Nahla dashboard" CTA, persisting the
// values to localStorage and stripping them from the URL.
bootstrapPreferences()

// Apply initial-path SEO before React mounts so crawlers and the first paint
// receive route-correct metadata immediately.
applyPublicSeo()

// Initialise Sentry FIRST so any error during app bootstrap (router
// registration, lazy imports, etc.) is captured. No-op when
// VITE_SENTRY_DSN is unset, so this is safe in dev / preview builds.
initSentry()

logNahlaRuntimeBoot()

// Catalog review environment: refuse to mount the app when the bundle has no
// explicit review API base or points at production. Static screen, no API call.
const reviewBoot = reviewApiBaseCheck()
if (reviewBoot.enabled && !reviewBoot.ok) {
  const reason = describeReviewApiBaseFailure(reviewBoot)
  // eslint-disable-next-line no-console
  console.error('[review-env] ' + reason)
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <main
        dir="ltr"
        style={{ fontFamily: 'system-ui, sans-serif', maxWidth: 640, margin: '10vh auto', padding: 24, lineHeight: 1.6 }}
      >
        <h1 style={{ fontSize: 20, marginBottom: 8 }}>Review environment is not configured</h1>
        <p>{reason}</p>
        <p style={{ color: '#64748b', fontSize: 14 }}>
          This build refuses to fall back to the production API. Rebuild with the review API base set.
        </p>
      </main>
    </React.StrictMode>,
  )
} else {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
}
