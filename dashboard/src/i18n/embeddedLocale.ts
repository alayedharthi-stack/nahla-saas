/**
 * Pure locale resolution for Salla embedded surfaces (no React).
 * Shared by bootstrapPreferences, useEmbeddedLocale, and CI smoke tests.
 */
export type EmbeddedLang = 'ar' | 'en'

export type EmbeddedLangSource =
  | 'url'
  | 'salla'
  | 'stored'
  | 'referrer'
  | 'navigator'
  | 'user'
  | 'default'

export const EMBED_LANG_STORAGE_KEY = 'nahla-embedded-lang'
export const USER_LANG_STORAGE_KEY  = 'nahla-lang'
export const SALLA_LANG_EVENT       = 'nahla:salla-lang'

export function normalizeEmbeddedLang(raw: string | null | undefined): EmbeddedLang | null {
  if (!raw) return null
  const lower = String(raw).toLowerCase().trim()
  if (lower.startsWith('ar')) return 'ar'
  if (lower.startsWith('en')) return 'en'
  return null
}

/** True when Nahla runs inside Salla's merchant iframe (not standalone dashboard). */
export function isSallaEmbeddedIframe(): boolean {
  try {
    if (window.self === window.top) return false
    const path = window.location.pathname
    if (
      path.startsWith('/app/salla') ||
      path.startsWith('/app/entry') ||
      path === '/salla'
    ) {
      return true
    }
    if (localStorage.getItem('nahla_salla_embedded') === '1') return true
    const ref = document.referrer || ''
    if (/s\.salla\.sa/i.test(ref) && /embedded/i.test(ref)) return true
    if (/\.salla\.sa/i.test(ref) && /embedded/i.test(ref)) return true
  } catch { /* ignore */ }
  return false
}

export function readUrlEmbeddedLang(search?: string): EmbeddedLang | null {
  try {
    const sp = new URLSearchParams(search ?? window.location.search)
    return normalizeEmbeddedLang(sp.get('lang') || sp.get('locale') || sp.get('language'))
  } catch {
    return null
  }
}

export function readStoredEmbedLang(): EmbeddedLang | null {
  try {
    return normalizeEmbeddedLang(localStorage.getItem(EMBED_LANG_STORAGE_KEY))
  } catch {
    return null
  }
}

export function readStoredUserLang(): EmbeddedLang | null {
  try {
    return normalizeEmbeddedLang(localStorage.getItem(USER_LANG_STORAGE_KEY))
  } catch {
    return null
  }
}

/** Infer locale from Salla host URL when the iframe was opened from Partners. */
export function readSallaReferrerLang(): EmbeddedLang | null {
  try {
    const ref = document.referrer || ''
    if (!ref) return null
    if (/[/?&._-](en|english)([/?&._-]|$)/i.test(ref)) return 'en'
    if (/[/?&._-](ar|arabic)([/?&._-]|$)/i.test(ref)) return 'ar'
    if (/\.salla\./i.test(ref) && /\/en(\/|$)/i.test(ref)) return 'en'
    if (/\.salla\./i.test(ref) && /\/ar(\/|$)/i.test(ref)) return 'ar'
    // s.salla.sa/embedded/... with Arabic UI — no /en/ segment → Arabic
    if (/s\.salla\.sa/i.test(ref) && /embedded/i.test(ref) && !/\/en(\/|$)/i.test(ref)) {
      return 'ar'
    }
  } catch { /* ignore */ }
  return null
}

export function readNavigatorLang(): EmbeddedLang | null {
  try {
    return normalizeEmbeddedLang(navigator.language)
  } catch {
    return null
  }
}

export function readDocumentLang(): EmbeddedLang | null {
  try {
    return normalizeEmbeddedLang(document.documentElement.lang)
  } catch {
    return null
  }
}

export function isDocumentRtl(): boolean {
  try {
    return document.documentElement.dir === 'rtl'
  } catch {
    return false
  }
}

function pickLangFromRecord(rec: Record<string, unknown>): EmbeddedLang | null {
  const direct = normalizeEmbeddedLang(
    String(rec.lang ?? rec.locale ?? rec.language ?? rec.value ?? ''),
  )
  if (direct) return direct

  const dir = String(rec.dir ?? rec.direction ?? '').toLowerCase()
  if (dir === 'rtl') return 'ar'
  if (dir === 'ltr') return 'en'

  const layout = (rec.layout && typeof rec.layout === 'object')
    ? rec.layout as Record<string, unknown>
    : null
  const context = (rec.context && typeof rec.context === 'object')
    ? rec.context as Record<string, unknown>
    : null

  return (layout ? pickLangFromRecord(layout) : null)
    || (context ? pickLangFromRecord(context) : null)
}

export function extractLangFromPostMessage(data: unknown): EmbeddedLang | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  const direct = pickLangFromRecord(d)
  if (direct) return direct

  const payload = (d.payload && typeof d.payload === 'object') ? d.payload as Record<string, unknown> : null
  const salla   = (d.salla && typeof d.salla === 'object') ? d.salla as Record<string, unknown> : null
  const nested  = (d.data && typeof d.data === 'object') ? d.data as Record<string, unknown> : null

  if (payload) {
    const fromPayload = pickLangFromRecord(payload)
    if (fromPayload) return fromPayload
  }
  if (salla) {
    const fromSalla = pickLangFromRecord(salla)
    if (fromSalla) return fromSalla
  }
  if (nested) {
    const fromNested = pickLangFromRecord(nested)
    if (fromNested) return fromNested
  }
  return null
}

/** Locale from `embedded.init()` resolve value (`state.layout`, etc.). */
export function extractLangFromSdkState(state: unknown): EmbeddedLang | null {
  return extractLangFromPostMessage(state)
}

export function isTrustedSallaLangMessage(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const rec  = data as Record<string, unknown>
  const type = String(rec.event || rec.type || '').toLowerCase()

  if (
    type.includes('context.provide')
    || type.includes('context::provide')
    || type.includes('embedded:context')
  ) {
    return extractLangFromPostMessage(data) !== null
  }
  if (type.includes('lang') || type.includes('locale') || type.includes('language')) {
    return extractLangFromPostMessage(data) !== null
  }
  if (type.includes('embedded') || type.includes('salla')) {
    return extractLangFromPostMessage(data) !== null
  }
  return false
}

export function notifySallaHostLang(lang: EmbeddedLang): void {
  try {
    window.dispatchEvent(new CustomEvent(SALLA_LANG_EVENT, { detail: lang }))
  } catch { /* ignore */ }
}

export interface ResolveEmbeddedLangInput {
  urlLang?:          EmbeddedLang | null
  embedStored?:      EmbeddedLang | null
  userPref?:         EmbeddedLang | null
  referrerLang?:     EmbeddedLang | null
  navigatorLang?:    EmbeddedLang | null
  documentLang?:     EmbeddedLang | null
  documentRtl?:      boolean
  sallaMessageLang?: EmbeddedLang | null
  inSallaEmbedded?:  boolean
}

/**
 * Resolve embedded UI language.
 *
 * Inside Salla iframe: live Salla signal (SDK/postMessage) beats URL handoff
 * and stale main-dashboard prefs. Safe default is `ar` when Salla sends nothing.
 */
export function resolveEmbeddedLang(input: ResolveEmbeddedLangInput = {}): {
  lang: EmbeddedLang
  source: EmbeddedLangSource
} {
  const embedded = input.inSallaEmbedded ?? isSallaEmbeddedIframe()

  if (input.sallaMessageLang) {
    return { lang: input.sallaMessageLang, source: 'salla' }
  }

  if (embedded) {
    if (input.referrerLang) {
      return { lang: input.referrerLang, source: 'referrer' }
    }
    if (input.embedStored) {
      return { lang: input.embedStored, source: 'stored' }
    }
    if (input.urlLang) {
      return { lang: input.urlLang, source: 'url' }
    }
    if (input.documentLang === 'ar' || (input.documentRtl && input.documentLang !== 'en')) {
      return { lang: 'ar', source: 'referrer' }
    }
    return { lang: 'ar', source: 'default' }
  }

  if (input.urlLang) {
    return { lang: input.urlLang, source: 'url' }
  }
  if (input.embedStored) {
    return { lang: input.embedStored, source: 'stored' }
  }
  if (input.userPref) {
    return { lang: input.userPref, source: 'user' }
  }
  if (input.referrerLang) {
    return { lang: input.referrerLang, source: 'referrer' }
  }
  if (input.navigatorLang) {
    return { lang: input.navigatorLang, source: 'navigator' }
  }
  return { lang: 'ar', source: 'default' }
}

export function applyEmbeddedLangToDocument(lang: EmbeddedLang): void {
  try {
    const root = document.documentElement
    root.lang = lang
    root.dir  = lang === 'ar' ? 'rtl' : 'ltr'
  } catch { /* ignore */ }
}

/** Persist embed + global lang keys and reflect on <html>. */
export function persistEmbeddedLang(lang: EmbeddedLang): void {
  try { localStorage.setItem(EMBED_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  try { localStorage.setItem(USER_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  applyEmbeddedLangToDocument(lang)
  try {
    window.dispatchEvent(new CustomEvent('nahla:lang-change', { detail: lang }))
  } catch { /* ignore */ }
}

/** Apply to DOM; persist only for trusted Salla / URL / stored / referrer sources. */
export function persistEmbeddedLangWithSource(
  lang: EmbeddedLang,
  source: EmbeddedLangSource,
): void {
  applyEmbeddedLangToDocument(lang)
  if (source === 'default') return
  try { localStorage.setItem(EMBED_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  if (source === 'url' || source === 'salla' || source === 'stored' || source === 'referrer') {
    try { localStorage.setItem(USER_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  }
  try {
    window.dispatchEvent(new CustomEvent('nahla:lang-change', { detail: lang }))
  } catch { /* ignore */ }
}

