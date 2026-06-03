/**
 * useEmbeddedLocale — Salla-aware locale resolution for embedded surfaces.
 *
 * Priority inside Salla iframe (see `resolveEmbeddedLang` in embeddedLocale.ts):
 *   URL → Salla postMessage → nahla-embedded-lang → referrer / RTL → Arabic default
 *   (stale `nahla-lang=en` from the main dashboard is NOT used in iframe)
 *
 * Outside iframe: URL → embed storage → user pref → referrer → navigator → ar
 */
import { useEffect, useState, useCallback } from 'react'
import { EMBEDDED_STRINGS, type EmbeddedLang, type EmbeddedStrings } from '../i18n/embedded'
import {
  type EmbeddedLangSource,
  resolveEmbeddedLang,
  readUrlEmbeddedLang,
  readStoredEmbedLang,
  readStoredUserLang,
  readSallaReferrerLang,
  readNavigatorLang,
  readDocumentLang,
  isDocumentRtl,
  isSallaEmbeddedIframe,
  extractLangFromPostMessage,
  persistEmbeddedLang,
  EMBED_LANG_STORAGE_KEY,
} from '../i18n/embeddedLocale'

export interface UseEmbeddedLocaleReturn<S extends EmbeddedStrings = EmbeddedStrings> {
  lang:  EmbeddedLang
  dir:   'rtl' | 'ltr'
  isRTL: boolean
  t:     S
  source: EmbeddedLangSource
  setLang: (lang: EmbeddedLang | null) => void
}

export function useEmbeddedLocale(): UseEmbeddedLocaleReturn {
  const resolve = useCallback((): { lang: EmbeddedLang; source: EmbeddedLangSource } => {
    const embedded = isSallaEmbeddedIframe()
    const { lang, source } = resolveEmbeddedLang({
      urlLang:          readUrlEmbeddedLang(),
      embedStored:      readStoredEmbedLang(),
      userPref:         readStoredUserLang(),
      referrerLang:     readSallaReferrerLang(),
      navigatorLang:    readNavigatorLang(),
      documentLang:     readDocumentLang(),
      documentRtl:      isDocumentRtl(),
      inSallaEmbedded:  embedded,
    })
    if (source === 'url' || source === 'stored' || source === 'salla' || source === 'default' || source === 'referrer') {
      persistEmbeddedLang(lang)
    } else if (embedded) {
      // Still mirror resolved lang into embed storage inside iframe
      persistEmbeddedLang(lang)
    }
    return { lang, source }
  }, [])

  const [state, setState] = useState(resolve)

  useEffect(() => { setState(resolve()) }, [resolve])

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const next = extractLangFromPostMessage(e?.data)
      if (!next) return
      persistEmbeddedLang(next)
      setState({ lang: next, source: 'salla' })
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  const setLang = useCallback((next: EmbeddedLang | null) => {
    try {
      if (next === null) localStorage.removeItem(EMBED_LANG_STORAGE_KEY)
      else               persistEmbeddedLang(next)
    } catch { /* ignore */ }
    setState(next ? { lang: next, source: 'stored' } : resolve())
  }, [resolve])

  const dir   = state.lang === 'ar' ? 'rtl' : 'ltr'
  const isRTL = dir === 'rtl'
  const t     = EMBEDDED_STRINGS[state.lang]

  useEffect(() => {
    try {
      const root = document.documentElement
      root.lang = state.lang
      root.dir  = dir
    } catch { /* ignore */ }
  }, [state.lang, dir])

  return { lang: state.lang, dir, isRTL, t, source: state.source, setLang }
}
