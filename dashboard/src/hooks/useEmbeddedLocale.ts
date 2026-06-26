/**
 * useEmbeddedLocale — Salla-aware locale resolution for embedded surfaces.
 *
 * Priority inside Salla iframe (see `resolveEmbeddedLang` in embeddedLocale.ts):
 *   live Salla (SDK/postMessage) → referrer → embed storage → URL → Arabic default
 */
import { useEffect, useState, useCallback, useRef } from 'react'
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
  isTrustedSallaLangMessage,
  persistEmbeddedLangWithSource,
  SALLA_LANG_EVENT,
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
  const sallaLiveRef = useRef<EmbeddedLang | null>(null)

  const resolve = useCallback((): { lang: EmbeddedLang; source: EmbeddedLangSource } => {
    const embedded = isSallaEmbeddedIframe()
    const result = resolveEmbeddedLang({
      urlLang:          readUrlEmbeddedLang(),
      sallaMessageLang: sallaLiveRef.current,
      embedStored:      readStoredEmbedLang(),
      userPref:         embedded ? null : readStoredUserLang(),
      referrerLang:     readSallaReferrerLang(),
      navigatorLang:    embedded ? null : readNavigatorLang(),
      documentLang:     readDocumentLang(),
      documentRtl:      isDocumentRtl(),
      inSallaEmbedded:  embedded,
    })
    persistEmbeddedLangWithSource(result.lang, result.source)
    return result
  }, [])

  const [state, setState] = useState(resolve)

  const applyHostLang = useCallback((lang: EmbeddedLang) => {
    sallaLiveRef.current = lang
    const result = resolveEmbeddedLang({
      urlLang:          readUrlEmbeddedLang(),
      sallaMessageLang: lang,
      embedStored:      readStoredEmbedLang(),
      userPref:         null,
      referrerLang:     readSallaReferrerLang(),
      navigatorLang:    null,
      documentLang:     readDocumentLang(),
      documentRtl:      isDocumentRtl(),
      inSallaEmbedded:  isSallaEmbeddedIframe(),
    })
    persistEmbeddedLangWithSource(result.lang, result.source)
    setState(result)
  }, [])

  useEffect(() => { setState(resolve()) }, [resolve])

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e?.data
      if (!isTrustedSallaLangMessage(d)) {
        const loose = extractLangFromPostMessage(d)
        if (!loose) return
        applyHostLang(loose)
        return
      }
      const next = extractLangFromPostMessage(d)
      if (!next) return
      applyHostLang(next)
    }
    const onSallaEvent = (e: Event) => {
      const next = (e as CustomEvent<EmbeddedLang>).detail
      if (next === 'ar' || next === 'en') applyHostLang(next)
    }
    window.addEventListener('message', onMsg)
    window.addEventListener(SALLA_LANG_EVENT, onSallaEvent)
    return () => {
      window.removeEventListener('message', onMsg)
      window.removeEventListener(SALLA_LANG_EVENT, onSallaEvent)
    }
  }, [applyHostLang])

  const setLang = useCallback((next: EmbeddedLang | null) => {
    try {
      if (next === null) localStorage.removeItem(EMBED_LANG_STORAGE_KEY)
      else               persistEmbeddedLangWithSource(next, 'stored')
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
