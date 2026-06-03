/**
 * useEmbeddedTheme — Salla-aware theme for embedded surfaces.
 *
 * Inside Salla iframe: URL → postMessage → nahla-embedded-theme → **light** default.
 * Stale `nahla-theme=dark` / OS dark mode do not apply in iframe.
 *
 * Outside iframe: URL → embed storage → user → system → light.
 */
import { useEffect, useState, useCallback } from 'react'
import { isSallaEmbeddedIframe } from '../i18n/embeddedLocale'
import {
  type EmbeddedTheme,
  type EmbeddedThemeSource,
  resolveEmbeddedTheme,
  readUrlEmbeddedTheme,
  readStoredEmbedTheme,
  readStoredUserResolvedTheme,
  readSystemTheme,
  extractThemeFromPostMessage,
  persistEmbeddedTheme,
  applyEmbeddedThemeToDocument,
  EMBED_THEME_STORAGE_KEY,
} from '../i18n/embeddedTheme'

export interface UseEmbeddedThemeReturn {
  theme: EmbeddedTheme
  isDark: boolean
  source: EmbeddedThemeSource
  embedded: boolean
  setOverride: (theme: EmbeddedTheme | null) => void
}

export function useEmbeddedTheme(): UseEmbeddedThemeReturn {
  const embedded = isSallaEmbeddedIframe()

  const resolve = useCallback((): { theme: EmbeddedTheme; source: EmbeddedThemeSource } => {
    const { theme, source } = resolveEmbeddedTheme({
      urlTheme:          readUrlEmbeddedTheme(),
      embedStored:       readStoredEmbedTheme(),
      userResolved:      readStoredUserResolvedTheme(),
      systemTheme:       readSystemTheme(),
      inSallaEmbedded:   embedded,
    })
    if (embedded || source === 'url' || source === 'stored' || source === 'salla') {
      persistEmbeddedTheme(theme)
    }
    return { theme, source }
  }, [embedded])

  const [state, setState] = useState(resolve)

  useEffect(() => { setState(resolve()) }, [resolve])

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e?.data
      if (!d || typeof d !== 'object') return
      const rec  = d as Record<string, unknown>
      const type = String(rec.event || rec.type || '').toLowerCase()
      const next = extractThemeFromPostMessage(d)
      if (!next && !type.includes('theme') && !type.includes('color') && !type.includes('appearance')) {
        return
      }
      if (!next) return
      persistEmbeddedTheme(next)
      setState({ theme: next, source: 'salla' })
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  const setOverride = useCallback((next: EmbeddedTheme | null) => {
    try {
      if (next === null) localStorage.removeItem(EMBED_THEME_STORAGE_KEY)
      else               persistEmbeddedTheme(next)
    } catch { /* ignore */ }
    setState(next ? { theme: next, source: 'stored' } : resolve())
  }, [resolve])

  // Win over useTheme() when inside Salla iframe (hooks run after useTheme in tree).
  useEffect(() => {
    if (!embedded) return
    applyEmbeddedThemeToDocument(state.theme)
  }, [embedded, state.theme])

  return {
    theme:   state.theme,
    isDark:  state.theme === 'dark',
    source:  state.source,
    embedded,
    setOverride,
  }
}

export type { ThemeMode } from './useTheme'
