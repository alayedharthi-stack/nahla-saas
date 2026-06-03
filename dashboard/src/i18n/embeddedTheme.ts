/**
 * Pure theme resolution for Salla embedded surfaces (no React).
 */
import {
  isSallaEmbeddedIframe,
  resolveEmbeddedLang,
  readUrlEmbeddedLang,
  readStoredEmbedLang,
  readStoredUserLang,
  readSallaReferrerLang,
  readNavigatorLang,
  readDocumentLang,
  isDocumentRtl,
} from './embeddedLocale'

export type EmbeddedTheme = 'light' | 'dark'

export type EmbeddedThemeSource =
  | 'url'
  | 'salla'
  | 'stored'
  | 'user'
  | 'system'
  | 'default'

export const EMBED_THEME_STORAGE_KEY       = 'nahla-embedded-theme'
export const EMBED_THEME_SOURCE_KEY        = 'nahla-embedded-theme-source'
export const USER_THEME_STORAGE_KEY        = 'nahla-theme'
export const SALLA_THEME_EVENT             = 'nahla:salla-theme'

export type TrustedEmbedThemeSource = 'url' | 'salla'

export function normalizeEmbeddedTheme(raw: string | null | undefined): EmbeddedTheme | null {
  if (!raw) return null
  const v = String(raw).toLowerCase().trim()
  if (v === 'dark' || v === 'night') return 'dark'
  if (v === 'light' || v === 'day')  return 'light'
  return null
}

export function logEmbeddedThemeResolved(
  theme: EmbeddedTheme,
  source: EmbeddedThemeSource,
): void {
  // eslint-disable-next-line no-console
  console.info('[SallaEmbeddedTheme] resolved theme=%s source=%s', theme, source)
}

export function readUrlEmbeddedTheme(search?: string): EmbeddedTheme | null {
  try {
    const sp = new URLSearchParams(search ?? window.location.search)
    return normalizeEmbeddedTheme(
      sp.get('theme') || sp.get('color_scheme') || sp.get('mode') || sp.get('appearance'),
    )
  } catch {
    return null
  }
}

/** Infer theme from Salla host URL (referrer or iframe query before bootstrap strips it). */
export function readSallaReferrerTheme(): EmbeddedTheme | null {
  try {
    const ref = document.referrer || ''
    if (ref) {
      if (/[/?&](theme|mode|color_scheme|appearance)=(dark|night)\b/i.test(ref)) return 'dark'
      if (/[/?&](theme|mode|color_scheme|appearance)=(light|day)\b/i.test(ref))  return 'light'
      if (/\.salla\./i.test(ref) && /\/(dark|night)(\/|$|\?)/i.test(ref)) return 'dark'
      if (/\.salla\./i.test(ref) && /\/(light|day)(\/|$|\?)/i.test(ref))  return 'light'
    }
  } catch { /* ignore */ }
  return null
}

export function readStoredEmbedTheme(): EmbeddedTheme | null {
  try {
    return normalizeEmbeddedTheme(localStorage.getItem(EMBED_THEME_STORAGE_KEY))
  } catch {
    return null
  }
}

/** Only reuse localStorage when it was set by Salla (postMessage / SDK) or URL handoff. */
export function readTrustedStoredEmbedTheme(): EmbeddedTheme | null {
  try {
    const src = localStorage.getItem(EMBED_THEME_SOURCE_KEY)
    if (src !== 'salla' && src !== 'url') return null
    return readStoredEmbedTheme()
  } catch {
    return null
  }
}

export function readStoredUserResolvedTheme(): EmbeddedTheme | null {
  try {
    const v = localStorage.getItem(USER_THEME_STORAGE_KEY)
    if (v === 'light' || v === 'dark') return v
    if (v === 'system') {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    }
  } catch { /* ignore */ }
  return null
}

export function readSystemTheme(): EmbeddedTheme | null {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return null
  }
}

function pickThemeFromRecord(rec: Record<string, unknown>): EmbeddedTheme | null {
  if (rec.is_dark === true || rec.isDark === true || rec.dark_mode === true) return 'dark'
  if (rec.is_dark === false || rec.isDark === false || rec.dark_mode === false) return 'light'

  const layout = (rec.layout && typeof rec.layout === 'object')
    ? rec.layout as Record<string, unknown>
    : null
  const context = (rec.context && typeof rec.context === 'object')
    ? rec.context as Record<string, unknown>
    : null
  const appearance = (rec.appearance && typeof rec.appearance === 'object')
    ? rec.appearance as Record<string, unknown>
    : null

  return normalizeEmbeddedTheme(
    String(rec.theme ?? rec.mode ?? rec.color_scheme ?? rec.appearance ?? rec.colorMode ?? ''),
  )
    || (layout ? pickThemeFromRecord(layout) : null)
    || (context ? pickThemeFromRecord(context) : null)
    || (appearance ? pickThemeFromRecord(appearance) : null)
}

/** Parse theme from Salla postMessage / SDK init state / context.provide payload. */
export function extractThemeFromPostMessage(data: unknown): EmbeddedTheme | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  const direct = pickThemeFromRecord(d)
  if (direct) return direct

  const payload = (d.payload && typeof d.payload === 'object') ? d.payload as Record<string, unknown> : null
  const salla   = (d.salla && typeof d.salla === 'object') ? d.salla as Record<string, unknown> : null
  const nested  = (d.data && typeof d.data === 'object') ? d.data as Record<string, unknown> : null

  if (payload) {
    const fromPayload = pickThemeFromRecord(payload)
    if (fromPayload) return fromPayload
  }
  if (salla) {
    const fromSalla = pickThemeFromRecord(salla)
    if (fromSalla) return fromSalla
  }
  if (nested) {
    const fromNested = pickThemeFromRecord(nested)
    if (fromNested) return fromNested
  }
  return null
}

/** Theme from `embedded.init()` resolve value (`state.layout`, etc.). */
export function extractThemeFromSdkState(state: unknown): EmbeddedTheme | null {
  return extractThemeFromPostMessage(state)
}

export function isTrustedSallaThemeMessage(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const rec  = data as Record<string, unknown>
  const type = String(rec.event || rec.type || '').toLowerCase()

  if (
    type.includes('context.provide')
    || type.includes('context::provide')
    || type.includes('embedded:context')
  ) {
    return extractThemeFromPostMessage(data) !== null
  }
  if (type.includes('theme') || type.includes('color') || type.includes('appearance')) {
    return extractThemeFromPostMessage(data) !== null
  }
  if (type.includes('embedded') || type.includes('salla')) {
    return extractThemeFromPostMessage(data) !== null
  }
  return false
}

export function notifySallaHostTheme(theme: EmbeddedTheme): void {
  try {
    window.dispatchEvent(new CustomEvent(SALLA_THEME_EVENT, { detail: theme }))
  } catch { /* ignore */ }
}

export interface ResolveEmbeddedThemeInput {
  urlTheme?:           EmbeddedTheme | null
  embedStored?:        EmbeddedTheme | null
  userResolved?:       EmbeddedTheme | null
  systemTheme?:        EmbeddedTheme | null
  sallaMessageTheme?:  EmbeddedTheme | null
  referrerTheme?:      EmbeddedTheme | null
  inSallaEmbedded?:    boolean
}

/**
 * Inside Salla iframe:
 *   URL → live Salla signal (postMessage / SDK / referrer) → trusted stored → light default.
 * Stale `nahla-embedded-theme=dark` without `nahla-embedded-theme-source=salla|url` is ignored.
 */
export function resolveEmbeddedTheme(input: ResolveEmbeddedThemeInput = {}): {
  theme: EmbeddedTheme
  source: EmbeddedThemeSource
} {
  const embedded = input.inSallaEmbedded ?? isSallaEmbeddedIframe()

  if (input.urlTheme) {
    return { theme: input.urlTheme, source: 'url' }
  }
  if (input.sallaMessageTheme) {
    return { theme: input.sallaMessageTheme, source: 'salla' }
  }
  if (input.referrerTheme) {
    return { theme: input.referrerTheme, source: 'salla' }
  }
  if (embedded && input.embedStored) {
    return { theme: input.embedStored, source: 'stored' }
  }
  if (!embedded && input.embedStored) {
    return { theme: input.embedStored, source: 'stored' }
  }

  if (embedded) {
    return { theme: 'light', source: 'default' }
  }

  if (input.userResolved) {
    return { theme: input.userResolved, source: 'user' }
  }
  if (input.systemTheme) {
    return { theme: input.systemTheme, source: 'system' }
  }
  return { theme: 'light', source: 'default' }
}

export function persistEmbeddedTheme(theme: EmbeddedTheme): void {
  try { localStorage.setItem(EMBED_THEME_STORAGE_KEY, theme) } catch { /* ignore */ }
  try { localStorage.setItem(USER_THEME_STORAGE_KEY, theme) } catch { /* ignore */ }
  applyEmbeddedThemeToDocument(theme)
  try {
    window.dispatchEvent(new CustomEvent('nahla:theme-change', { detail: theme }))
  } catch { /* ignore */ }
}

export function persistEmbeddedThemeWithSource(
  theme: EmbeddedTheme,
  source: EmbeddedThemeSource,
): void {
  persistEmbeddedTheme(theme)
  try {
    const tag: TrustedEmbedThemeSource | 'default' =
      source === 'url' ? 'url'
      : (source === 'salla' || source === 'stored') ? 'salla'
      : 'default'
    localStorage.setItem(EMBED_THEME_SOURCE_KEY, tag)
  } catch { /* ignore */ }
}

export function applyEmbeddedThemeToDocument(theme: EmbeddedTheme): void {
  try {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.setAttribute('data-theme', theme)
    root.style.colorScheme = theme
  } catch { /* ignore */ }
}

export function buildEmbeddedEntryQuery(searchParams?: URLSearchParams): string {
  const sp = searchParams ?? new URLSearchParams(window.location.search)
  const search = sp.toString() ? `?${sp}` : undefined
  const inEmbed = isSallaEmbeddedIframe()
  const { lang } = resolveEmbeddedLang({
    urlLang:           readUrlEmbeddedLang(search),
    embedStored:       readStoredEmbedLang(),
    userPref:          readStoredUserLang(),
    referrerLang:      readSallaReferrerLang(),
    navigatorLang:     readNavigatorLang(),
    documentLang:      readDocumentLang(),
    documentRtl:       isDocumentRtl(),
    inSallaEmbedded:   inEmbed,
  })
  const { theme } = resolveEmbeddedTheme({
    urlTheme:          readUrlEmbeddedTheme(search),
    embedStored:       inEmbed ? readTrustedStoredEmbedTheme() : readStoredEmbedTheme(),
    referrerTheme:     inEmbed ? readSallaReferrerTheme() : null,
    userResolved:      inEmbed ? null : readStoredUserResolvedTheme(),
    systemTheme:       inEmbed ? null : readSystemTheme(),
    inSallaEmbedded:   inEmbed,
  })
  const out = new URLSearchParams()
  out.set('lang', lang)
  out.set('theme', theme)
  const qs = out.toString()
  return qs ? `?${qs}` : ''
}
