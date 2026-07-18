import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { logNahlaRuntimeBoot } from './lib/logRuntimeBoot'
import { initSentry } from './lib/sentry'
import { bootstrapPreferences } from './lib/bootstrapPreferences'
import { applyPublicSeo } from './seo/publicSeo'

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

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
