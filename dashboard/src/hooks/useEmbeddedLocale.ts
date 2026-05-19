/**
 * useEmbeddedLocale — Salla-aware locale resolution for embedded surfaces.
 * ────────────────────────────────────────────────────────────────────────
 * Priority chain (highest → lowest):
 *   1. URL param  ?lang=ar|en   (also: ?locale=, ?language=)
 *   2. Salla postMessage event (salla::locale | locale::changed | ...)
 *   3. localStorage `nahla-embedded-lang` (sticky for the iframe session)
 *   4. Nahla user preference (`nahla-lang`)
 *   5. document.referrer URL contains /en/ or /ar/
 *   6. navigator.language
 *   7. Arabic (default for Saudi market)
 *
 * Returns an i18n helper that pulls strings from the embedded dictionary.
 */
import { useEffect, useState, useCallback } from 'react'
import { EMBEDDED_STRINGS, type EmbeddedLang, type EmbeddedStrings } from '../i18n/embedded'

const EMBED_STORAGE_KEY = 'nahla-embedded-lang'
const USER_STORAGE_KEY  = 'nahla-lang'

function normalize(raw: string | null | undefined): EmbeddedLang | null {
  if (!raw) return null
  const lower = String(raw).toLowerCase().trim()
  if (lower.startsWith('ar')) return 'ar'
  if (lower.startsWith('en')) return 'en'
  return null
}

function readUrlLang(): EmbeddedLang | null {
  try {
    const p = new URLSearchParams(window.location.search)
    return normalize(p.get('lang') || p.get('locale') || p.get('language'))
  } catch { return null }
}

function readStoredEmbed(): EmbeddedLang | null {
  try { return normalize(localStorage.getItem(EMBED_STORAGE_KEY)) }
  catch { return null }
}

function readUserPref(): EmbeddedLang | null {
  try { return normalize(localStorage.getItem(USER_STORAGE_KEY)) }
  catch { return null }
}

function readReferrer(): EmbeddedLang | null {
  try {
    const ref = document.referrer
    if (!ref) return null
    if (/[/?&._-](en|english)([/?&._-]|$)/i.test(ref)) return 'en'
    if (/[/?&._-](ar|arabic)([/?&._-]|$)/i.test(ref))  return 'ar'
    // Salla often sets /en or /ar in the path
    if (/\.salla\./i.test(ref) && /\/en(\/|$)/i.test(ref)) return 'en'
    if (/\.salla\./i.test(ref) && /\/ar(\/|$)/i.test(ref)) return 'ar'
  } catch { /* ignore */ }
  return null
}

function readNavigatorLang(): EmbeddedLang | null {
  try { return normalize(navigator.language) }
  catch { return null }
}

export interface UseEmbeddedLocaleReturn<S extends EmbeddedStrings = EmbeddedStrings> {
  lang:  EmbeddedLang
  dir:   'rtl' | 'ltr'
  isRTL: boolean
  /** Resolved string dictionary for this locale. */
  t:     S
  /** Where the locale was resolved from (for debugging). */
  source: 'url' | 'salla' | 'stored' | 'user' | 'referrer' | 'navigator' | 'default'
  /** Override the embedded locale (persisted to localStorage). */
  setLang: (lang: EmbeddedLang | null) => void
}

export function useEmbeddedLocale(): UseEmbeddedLocaleReturn {
  const resolve = useCallback((): { lang: EmbeddedLang; source: UseEmbeddedLocaleReturn['source'] } => {
    const url = readUrlLang();        if (url)        return { lang: url,        source: 'url' }
    const stored = readStoredEmbed(); if (stored)     return { lang: stored,     source: 'stored' }
    const user = readUserPref();      if (user)       return { lang: user,       source: 'user' }
    const ref = readReferrer();       if (ref)        return { lang: ref,        source: 'referrer' }
    const nav = readNavigatorLang();  if (nav)        return { lang: nav,        source: 'navigator' }
    return { lang: 'ar', source: 'default' }
  }, [])

  const [state, setState] = useState(resolve)

  useEffect(() => { setState(resolve()) }, [resolve])

  // Listen for postMessage from Salla host frame
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e?.data
      if (!d || typeof d !== 'object') return
      const type = String(d.event || d.type || '').toLowerCase()
      if (!type.includes('locale') && !type.includes('lang') && !type.includes('language')) return
      const next = normalize(d.lang || d.locale || d.language || d.value || d?.payload?.locale || d?.payload?.lang)
      if (!next) return
      try { localStorage.setItem(EMBED_STORAGE_KEY, next) } catch { /* ignore */ }
      setState({ lang: next, source: 'salla' })
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  const setLang = useCallback((next: EmbeddedLang | null) => {
    try {
      if (next === null) localStorage.removeItem(EMBED_STORAGE_KEY)
      else               localStorage.setItem(EMBED_STORAGE_KEY, next)
    } catch { /* ignore */ }
    setState(next ? { lang: next, source: 'stored' } : resolve())
  }, [resolve])

  const dir   = state.lang === 'ar' ? 'rtl' : 'ltr'
  const isRTL = dir === 'rtl'
  const t     = EMBEDDED_STRINGS[state.lang]

  // Reflect on <html> for CSS direction-aware rules
  useEffect(() => {
    try {
      const root = document.documentElement
      root.lang = state.lang
      root.dir  = dir
    } catch { /* ignore */ }
  }, [state.lang, dir])

  return { lang: state.lang, dir, isRTL, t, source: state.source, setLang }
}
