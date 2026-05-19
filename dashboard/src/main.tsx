import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { logNahlaRuntimeBoot } from './lib/logRuntimeBoot'
import { initSentry } from './lib/sentry'

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
