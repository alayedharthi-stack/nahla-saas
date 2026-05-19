/**
 * useEmbeddedTheme — Salla-aware theme resolution.
 * ─────────────────────────────────────────────────
 * Priority chain (highest → lowest):
 *   1. URL param  ?theme=dark|light
 *   2. Salla postMessage event (salla::theme | theme::changed | ...)
 *   3. localStorage `nahla-embedded-theme` (sticky for the iframe session)
 *   4. Nahla user preference (`nahla-theme` via useTheme)
 *   5. prefers-color-scheme (inherited from Salla iframe host on most browsers)
 *
 * Cross-origin parent inspection is wrapped in try/catch — Salla's dashboard
 * lives on a different origin, so direct DOM reads will throw SecurityError.
 * We rely on postMessage as the primary cross-origin channel.
 *
 * Returns the same shape as useTheme but with the resolved theme reflecting
 * Salla's preference when this page is embedded inside Salla.
 */
import { useEffect, useState, useCallback } from 'react'
import { useTheme, type ThemeMode } from './useTheme'

const EMBED_STORAGE_KEY  = 'nahla-embedded-theme'
const GLOBAL_STORAGE_KEY = 'nahla-theme'   // also written by useTheme — kept in sync

/**
 * Cross-propagate a Salla-resolved theme into the global Nahla preference so
 * that when the merchant clicks "Open Nahla dashboard" (which leaves the
 * embedded surface and lands on a fresh dashboard page) the same theme is
 * already in effect.  We only write explicit 'dark' / 'light' values — we
 * never overwrite a user-chosen 'system' mode.
 */
function propagateThemeToGlobal(theme: Resolved): void {
  try {
    localStorage.setItem(GLOBAL_STORAGE_KEY, theme)
  } catch { /* localStorage blocked */ }
  // Notify any live useTheme() hook in another tab/iframe in this origin so
  // the dashboard reflects the new value without a page reload.
  try { window.dispatchEvent(new CustomEvent('nahla:theme-change', { detail: theme })) }
  catch { /* ignore */ }
}

type Resolved = 'light' | 'dark'

function readUrlTheme(): Resolved | null {
  try {
    const p = new URLSearchParams(window.location.search)
    const v = (p.get('theme') || p.get('color_scheme') || p.get('mode') || '').toLowerCase()
    if (v === 'dark' || v === 'night') return 'dark'
    if (v === 'light' || v === 'day')  return 'light'
  } catch { /* ignore */ }
  return null
}

function readStoredEmbed(): Resolved | null {
  try {
    const v = localStorage.getItem(EMBED_STORAGE_KEY)
    if (v === 'dark' || v === 'light') return v
  } catch { /* ignore */ }
  return null
}

function readSystemDark(): Resolved | null {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch { return null }
}

function isInsideIframe(): boolean {
  try { return window.self !== window.top } catch { return true }
}

export interface UseEmbeddedThemeReturn {
  /** Final resolved theme — always 'light' or 'dark'. */
  theme: Resolved
  isDark: boolean
  /** Where the theme was resolved from (for debugging). */
  source: 'url' | 'salla' | 'stored' | 'user' | 'system' | 'default'
  /** Whether this surface is rendered inside the Salla iframe. */
  embedded: boolean
  /** Override the embedded theme (persisted to localStorage). */
  setOverride: (theme: Resolved | null) => void
}

export function useEmbeddedTheme(): UseEmbeddedThemeReturn {
  const { theme: userTheme } = useTheme()
  const embedded = isInsideIframe()

  const resolve = useCallback((): { theme: Resolved; source: UseEmbeddedThemeReturn['source'] } => {
    const url = readUrlTheme()
    if (url) {
      // CRITICAL: persist URL-resolved theme to embed storage so that subsequent
      // React Router navigations within the Salla iframe (e.g. /app/salla → /app/entry)
      // keep the same theme, even though navigate() strips the original query string.
      try { localStorage.setItem(EMBED_STORAGE_KEY, url) } catch { /* ignore */ }
      // Also propagate to the global Nahla preference so that when the
      // merchant opens the full dashboard the theme matches.
      propagateThemeToGlobal(url)
      return { theme: url, source: 'url' }
    }
    const stored = readStoredEmbed()
    if (stored) {
      // The stored embed value was originally set by a URL / postMessage
      // resolution — propagate it once more in case the global key was
      // cleared by another flow (logout, etc.).
      propagateThemeToGlobal(stored)
      return { theme: stored, source: 'stored' }
    }
    if (userTheme === 'dark' || userTheme === 'light') {
      // Only treat as "user" choice when they have an explicit preference.
      // useTheme resolves 'system' → light/dark, but we can't distinguish here.
      // Fall back to userTheme regardless — it's the safest default.
      return { theme: userTheme, source: 'user' }
    }
    const sys = readSystemDark(); if (sys) return { theme: sys, source: 'system' }
    return { theme: 'light', source: 'default' }
  }, [userTheme])

  const [state, setState] = useState(resolve)

  // Re-resolve whenever Nahla user preference changes
  useEffect(() => { setState(resolve()) }, [resolve])

  // Listen for postMessage from Salla host frame
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e?.data
      if (!d || typeof d !== 'object') return
      // Common Salla / generic shapes we accept
      const type = String(d.event || d.type || '').toLowerCase()
      if (!type.includes('theme') && !type.includes('color') && !type.includes('appearance')) return
      const raw = String(d.theme || d.mode || d.value || d?.payload?.theme || '').toLowerCase()
      const next: Resolved | null =
        raw === 'dark' || raw === 'night'  ? 'dark'  :
        raw === 'light' || raw === 'day'   ? 'light' :
        null
      if (!next) return
      try { localStorage.setItem(EMBED_STORAGE_KEY, next) } catch { /* ignore */ }
      propagateThemeToGlobal(next)
      setState({ theme: next, source: 'salla' })
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  // Live system theme tracking (when our chain falls back to system)
  useEffect(() => {
    if (state.source !== 'system' && state.source !== 'default') return
    let mql: MediaQueryList
    try { mql = window.matchMedia('(prefers-color-scheme: dark)') }
    catch { return }
    const onChange = () => setState({
      theme:  mql.matches ? 'dark' : 'light',
      source: 'system',
    })
    mql.addEventListener?.('change', onChange)
    return () => mql.removeEventListener?.('change', onChange)
  }, [state.source])

  const setOverride = useCallback((next: Resolved | null) => {
    try {
      if (next === null) localStorage.removeItem(EMBED_STORAGE_KEY)
      else               localStorage.setItem(EMBED_STORAGE_KEY, next)
    } catch { /* ignore */ }
    setState(next ? { theme: next, source: 'stored' } : resolve())
  }, [resolve])

  // Also reflect on the document so global CSS (e.g. body bg) responds.
  useEffect(() => {
    if (!embedded) return
    const root = document.documentElement
    root.classList.toggle('dark', state.theme === 'dark')
    root.setAttribute('data-theme', state.theme)
    root.style.colorScheme = state.theme
  }, [embedded, state.theme])

  return {
    theme:   state.theme,
    isDark:  state.theme === 'dark',
    source:  state.source,
    embedded,
    setOverride,
  }
}

// Re-export for convenience
export type { ThemeMode }
