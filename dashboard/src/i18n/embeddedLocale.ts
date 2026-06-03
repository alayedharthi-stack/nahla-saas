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

export function extractLangFromPostMessage(data: unknown): EmbeddedLang | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  const payload = (d.payload && typeof d.payload === 'object') ? d.payload as Record<string, unknown> : null
  const salla   = (d.salla && typeof d.salla === 'object') ? d.salla as Record<string, unknown> : null
  const nested  = (d.data && typeof d.data === 'object') ? d.data as Record<string, unknown> : null
  return normalizeEmbeddedLang(
    String(d.lang ?? d.locale ?? d.language ?? d.value ?? ''),
  ) || normalizeEmbeddedLang(
    payload ? String(payload.lang ?? payload.locale ?? payload.language ?? '') : null,
  ) || normalizeEmbeddedLang(
    salla ? String(salla.lang ?? salla.locale ?? salla.language ?? '') : null,
  ) || normalizeEmbeddedLang(
    nested ? String(nested.lang ?? nested.locale ?? nested.language ?? '') : null,
  )
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
 * Inside Salla iframe we must NOT let a stale `nahla-lang=en` (main dashboard)
 * or `navigator.language=en` override Arabic — Salla's Arabic UI does not pass
 * ?lang=ar on /embedded/app/{id}/index. Safe default in that context is `ar`.
 * English is used only when URL, sticky embed storage, Salla postMessage, or
 * an explicit /en/ referrer segment says so.
 */
export function resolveEmbeddedLang(input: ResolveEmbeddedLangInput = {}): {
  lang: EmbeddedLang
  source: EmbeddedLangSource
} {
  const embedded = input.inSallaEmbedded ?? isSallaEmbeddedIframe()

  if (input.urlLang) {
    return { lang: input.urlLang, source: 'url' }
  }
  if (input.sallaMessageLang) {
    return { lang: input.sallaMessageLang, source: 'salla' }
  }
  if (input.embedStored) {
    return { lang: input.embedStored, source: 'stored' }
  }

  if (embedded) {
    if (input.referrerLang) {
      return { lang: input.referrerLang, source: 'referrer' }
    }
    if (input.documentLang === 'ar' || (input.documentRtl && input.documentLang !== 'en')) {
      return { lang: 'ar', source: 'referrer' }
    }
    // Embedded safe default — Arabic, not English
    return { lang: 'ar', source: 'default' }
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

/** Persist embed + global lang keys and reflect on <html>. */
export function persistEmbeddedLang(lang: EmbeddedLang): void {
  try { localStorage.setItem(EMBED_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  try { localStorage.setItem(USER_LANG_STORAGE_KEY, lang) } catch { /* ignore */ }
  try {
    const root = document.documentElement
    root.lang = lang
    root.dir  = lang === 'ar' ? 'rtl' : 'ltr'
  } catch { /* ignore */ }
  try {
    window.dispatchEvent(new CustomEvent('nahla:lang-change', { detail: lang }))
  } catch { /* ignore */ }
}

/** Build /app/entry query string fragment including resolved lang (and optional theme). */
export function buildEmbeddedEntryQuery(
  searchParams?: URLSearchParams,
  themeParams?: { theme?: string | null },
): string {
  const sp = searchParams ?? new URLSearchParams(window.location.search)
  const { lang } = resolveEmbeddedLang({
    urlLang:       readUrlEmbeddedLang(sp.toString() ? `?${sp}` : undefined),
    embedStored:   readStoredEmbedLang(),
    userPref:      readStoredUserLang(),
    referrerLang:  readSallaReferrerLang(),
    navigatorLang: readNavigatorLang(),
    documentLang:  readDocumentLang(),
    documentRtl:   isDocumentRtl(),
    inSallaEmbedded: isSallaEmbeddedIframe(),
  })
  const out = new URLSearchParams()
  out.set('lang', lang)
  const theme = themeParams?.theme
    ?? sp.get('theme')
    ?? sp.get('color_scheme')
    ?? sp.get('mode')
  if (theme) out.set('theme', theme)
  const qs = out.toString()
  return qs ? `?${qs}` : ''
}
