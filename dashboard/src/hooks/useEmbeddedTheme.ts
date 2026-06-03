/**
 * useEmbeddedTheme — matches Salla merchant dashboard light/dark inside iframe.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { isSallaEmbeddedIframe } from '../i18n/embeddedLocale'
import {
  type EmbeddedTheme,
  type EmbeddedThemeSource,
  resolveEmbeddedTheme,
  readUrlEmbeddedTheme,
  readTrustedStoredEmbedTheme,
  readSallaReferrerTheme,
  readStoredUserResolvedTheme,
  readSystemTheme,
  extractThemeFromPostMessage,
  isTrustedSallaThemeMessage,
  persistEmbeddedThemeWithSource,
  applyEmbeddedThemeToDocument,
  logEmbeddedThemeResolved,
  SALLA_THEME_EVENT,
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
  const embedded     = isSallaEmbeddedIframe()
  const sallaLiveRef = useRef<EmbeddedTheme | null>(null)

  const resolve = useCallback((): { theme: EmbeddedTheme; source: EmbeddedThemeSource } => {
    const result = resolveEmbeddedTheme({
      urlTheme:          readUrlEmbeddedTheme(),
      sallaMessageTheme: sallaLiveRef.current,
      embedStored:       embedded ? readTrustedStoredEmbedTheme() : null,
      referrerTheme:     embedded ? readSallaReferrerTheme() : null,
      userResolved:      readStoredUserResolvedTheme(),
      systemTheme:       readSystemTheme(),
      inSallaEmbedded:   embedded,
    })
    logEmbeddedThemeResolved(result.theme, result.source)
    if (embedded) {
      persistEmbeddedThemeWithSource(result.theme, result.source)
    } else if (result.source === 'url' || result.source === 'stored' || result.source === 'salla') {
      persistEmbeddedThemeWithSource(result.theme, result.source)
    }
    return result
  }, [embedded])

  const [state, setState] = useState(resolve)

  const applyHostTheme = useCallback((theme: EmbeddedTheme) => {
    sallaLiveRef.current = theme
    const result = resolveEmbeddedTheme({
      urlTheme:          readUrlEmbeddedTheme(),
      sallaMessageTheme: theme,
      embedStored:       embedded ? readTrustedStoredEmbedTheme() : null,
      referrerTheme:     embedded ? readSallaReferrerTheme() : null,
      userResolved:      null,
      systemTheme:       null,
      inSallaEmbedded:   embedded,
    })
    logEmbeddedThemeResolved(result.theme, result.source)
    persistEmbeddedThemeWithSource(result.theme, result.source)
    setState(result)
  }, [embedded])

  useEffect(() => { setState(resolve()) }, [resolve])

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e?.data
      if (!isTrustedSallaThemeMessage(d)) return
      const next = extractThemeFromPostMessage(d)
      if (!next) return
      applyHostTheme(next)
    }
    const onSallaEvent = (e: Event) => {
      const next = (e as CustomEvent<EmbeddedTheme>).detail
      if (next === 'light' || next === 'dark') applyHostTheme(next)
    }
    window.addEventListener('message', onMsg)
    window.addEventListener(SALLA_THEME_EVENT, onSallaEvent)
    return () => {
      window.removeEventListener('message', onMsg)
      window.removeEventListener(SALLA_THEME_EVENT, onSallaEvent)
    }
  }, [applyHostTheme])

  const setOverride = useCallback((next: EmbeddedTheme | null) => {
    try {
      if (next === null) {
        localStorage.removeItem(EMBED_THEME_STORAGE_KEY)
        sallaLiveRef.current = null
      } else {
        sallaLiveRef.current = next
        persistEmbeddedThemeWithSource(next, 'stored')
      }
    } catch { /* ignore */ }
    setState(next ? { theme: next, source: 'stored' } : resolve())
  }, [resolve])

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
