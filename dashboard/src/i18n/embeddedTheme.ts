/**
 * Pure theme resolution for Salla embedded surfaces (no React).
 * Shared by bootstrapPreferences, useEmbeddedTheme, and CI smoke tests.
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

export const EMBED_THEME_STORAGE_KEY = 'nahla-embedded-theme'
export const USER_THEME_STORAGE_KEY  = 'nahla-theme'

export function normalizeEmbeddedTheme(raw: string | null | undefined): EmbeddedTheme | null {
  if (!raw) return null
  const v = String(raw).toLowerCase().trim()
  if (v === 'dark' || v === 'night') return 'dark'
  if (v === 'light' || v === 'day')  return 'light'
  return null
}

export function readUrlEmbeddedTheme(search?: string): EmbeddedTheme | null {
  try {
    const sp = new URLSearchParams(search ?? window.location.search)
    return normalizeEmbeddedTheme(
      sp.get('theme') || sp.get('color_scheme') || sp.get('mode'),
    )
  } catch {
    return null
  }
}

export function readStoredEmbedTheme(): EmbeddedTheme | null {
  try {
    return normalizeEmbeddedTheme(localStorage.getItem(EMBED_THEME_STORAGE_KEY))
  } catch {
    return null
  }
}

/** Resolved user theme from `nahla-theme` (light | dark | system→OS). */
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

export function extractThemeFromPostMessage(data: unknown): EmbeddedTheme | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  const payload = (d.payload && typeof d.payload === 'object') ? d.payload as Record<string, unknown> : null
  const salla   = (d.salla && typeof d.salla === 'object') ? d.salla as Record<string, unknown> : null
  const nested  = (d.data && typeof d.data === 'object') ? d.data as Record<string, unknown> : null
  const raw = String(
    d.theme ?? d.mode ?? d.color_scheme ?? d.appearance ?? d.value ?? '',
  ) || (payload ? String(payload.theme ?? payload.mode ?? payload.color_scheme ?? '') : '')
    || (salla ? String(salla.theme ?? salla.mode ?? salla.color_scheme ?? '') : '')
    || (nested ? String(nested.theme ?? nested.mode ?? '') : '')
  return normalizeEmbeddedTheme(raw)
}

/** Ignore noisy host frames — only Salla/embedded theme events are trusted. */
export function isTrustedSallaThemeMessage(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const rec  = data as Record<string, unknown>
  const type = String(rec.event || rec.type || '').toLowerCase()
  if (type.includes('theme') || type.includes('color') || type.includes('appearance')) {
    return extractThemeFromPostMessage(data) !== null
  }
  if (type.includes('embedded') || type.includes('salla')) {
    return extractThemeFromPostMessage(data) !== null
  }
  return false
}

export interface ResolveEmbeddedThemeInput {
  urlTheme?:           EmbeddedTheme | null
  embedStored?:        EmbeddedTheme | null
  userResolved?:       EmbeddedTheme | null
  systemTheme?:        EmbeddedTheme | null
  sallaMessageTheme?:  EmbeddedTheme | null
  inSallaEmbedded?:    boolean
}

/**
 * Inside Salla iframe: default **light** (matches Salla dashboard).
 * Stale `nahla-embedded-theme=dark` is ignored — only explicit `?theme=`
 * or a trusted Salla postMessage may select dark.
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
  // Legacy localStorage dark must not override Salla's light chrome.
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

export function applyEmbeddedThemeToDocument(theme: EmbeddedTheme): void {
  try {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.setAttribute('data-theme', theme)
    root.style.colorScheme = theme
  } catch { /* ignore */ }
}

/** Build /app/entry query string with resolved lang + theme for SPA handoff. */
export function buildEmbeddedEntryQuery(searchParams?: URLSearchParams): string {
  const sp = searchParams ?? new URLSearchParams(window.location.search)
  const search = sp.toString() ? `?${sp}` : undefined
  const { lang } = resolveEmbeddedLang({
    urlLang:           readUrlEmbeddedLang(search),
    embedStored:       readStoredEmbedLang(),
    userPref:          readStoredUserLang(),
    referrerLang:      readSallaReferrerLang(),
    navigatorLang:     readNavigatorLang(),
    documentLang:      readDocumentLang(),
    documentRtl:       isDocumentRtl(),
    inSallaEmbedded:   isSallaEmbeddedIframe(),
  })
  const inEmbed = isSallaEmbeddedIframe()
  const { theme } = resolveEmbeddedTheme({
    urlTheme:          readUrlEmbeddedTheme(search),
    embedStored:       inEmbed ? null : readStoredEmbedTheme(),
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
