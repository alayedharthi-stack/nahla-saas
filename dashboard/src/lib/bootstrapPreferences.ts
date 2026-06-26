/**
 * bootstrapPreferences — early init for theme + locale.
 * ─────────────────────────────────────────────────────
 * Runs synchronously in `main.tsx` BEFORE React renders so:
 *   • the very first paint matches the merchant's preference (no FOUC)
 *   • <html dir/lang/class> are correct before the LanguageProvider mounts
 *   • Salla → Nahla preference handoff (?theme=… &lang=…) lands instantly
 *
 * Inside Salla iframe (`embeddedLocale.ts`): Arabic is the safe default;
 * a stale `nahla-lang=en` from the standalone dashboard must not win.
 */

import {
  EMBED_LANG_STORAGE_KEY,
  isSallaEmbeddedIframe,
  readDocumentLang,
  isDocumentRtl,
  readSallaReferrerLang,
  readStoredEmbedLang,
  resolveEmbeddedLang,
  persistEmbeddedLangWithSource,
} from '../i18n/embeddedLocale'
import {
  readTrustedStoredEmbedTheme,
  readSallaReferrerTheme,
  readStoredUserResolvedTheme,
  readSystemTheme,
  resolveEmbeddedTheme,
  persistEmbeddedThemeWithSource,
  logEmbeddedThemeResolved,
} from '../i18n/embeddedTheme'

type ThemeMode = 'light' | 'dark' | 'system'
type Lang      = 'ar' | 'en'

const THEME_KEY = 'nahla-theme'
const LANG_KEY  = 'nahla-lang'

function normTheme(raw: string | null | undefined): 'light' | 'dark' | null {
  if (!raw) return null
  const v = raw.toLowerCase().trim()
  if (v === 'dark' || v === 'night') return 'dark'
  if (v === 'light' || v === 'day')  return 'light'
  return null
}

function normLang(raw: string | null | undefined): Lang | null {
  if (!raw) return null
  const v = raw.toLowerCase().trim()
  if (v.startsWith('ar')) return 'ar'
  if (v.startsWith('en')) return 'en'
  return null
}

function systemPrefersDark(): boolean {
  try { return window.matchMedia('(prefers-color-scheme: dark)').matches }
  catch { return false }
}

function readStoredTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(THEME_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch { /* ignore */ }
  return 'system'
}

function readStoredLang(): Lang | null {
  try {
    const v = localStorage.getItem(LANG_KEY)
    if (v === 'ar' || v === 'en') return v
  } catch { /* ignore */ }
  return null
}

function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return mode
}

function applyTheme(resolved: 'light' | 'dark'): void {
  try {
    const root = document.documentElement
    root.classList.toggle('dark', resolved === 'dark')
    root.setAttribute('data-theme', resolved)
    root.style.colorScheme = resolved
  } catch { /* DOM not ready */ }
}

function applyLang(lang: Lang): void {
  try {
    const root = document.documentElement
    root.lang = lang
    root.dir  = lang === 'ar' ? 'rtl' : 'ltr'
  } catch { /* DOM not ready */ }
}

/**
 * Reads both preferences from URL + storage, applies them to <html>, and
 * persists URL-resolved values back to localStorage.  Returns a brief
 * summary for logging.
 */
export function bootstrapPreferences(): {
  theme: 'light' | 'dark'
  lang:  Lang
  source: { theme: 'url' | 'stored' | 'system' | 'embed'; lang: 'url' | 'stored' | 'default' | 'embed' }
} {
  // ── URL ──
  let urlTheme: 'light' | 'dark' | null = null
  let urlLang:  Lang | null = null
  let consumedAny = false

  try {
    const sp = new URLSearchParams(window.location.search)
    urlTheme = normTheme(sp.get('theme') || sp.get('color_scheme') || sp.get('mode'))
    urlLang  = normLang(sp.get('lang')   || sp.get('locale')       || sp.get('language'))

    // Strip the consumed keys but keep everything else (next=, token=, …).
    if (urlTheme || urlLang) {
      consumedAny = true
      ;['theme', 'color_scheme', 'mode', 'lang', 'locale', 'language']
        .forEach(k => sp.delete(k))
      const qs = sp.toString()
      const cleanUrl =
        window.location.pathname +
        (qs ? `?${qs}` : '') +
        window.location.hash
      try { window.history.replaceState(null, '', cleanUrl) } catch { /* ignore */ }
    }
  } catch { /* URL parsing failed */ }

  // ── Theme resolution ──
  let themeSource: 'url' | 'stored' | 'system' | 'embed'
  let resolvedTheme: 'light' | 'dark'
  const inEmbed = isSallaEmbeddedIframe()

  if (urlTheme) {
    themeSource   = 'url'
    resolvedTheme = urlTheme
    persistEmbeddedThemeWithSource(urlTheme, 'url')
  } else if (inEmbed) {
    const { theme, source } = resolveEmbeddedTheme({
      urlTheme:          null,
      embedStored:       readTrustedStoredEmbedTheme(),
      referrerTheme:     readSallaReferrerTheme(),
      userResolved:      null,
      systemTheme:       readSystemTheme(),
      inSallaEmbedded:   true,
    })
    resolvedTheme = theme
    themeSource   = source === 'stored' ? 'stored' : source === 'system' ? 'system' : 'embed'
    logEmbeddedThemeResolved(theme, source)
    persistEmbeddedThemeWithSource(theme, source)
  } else {
    const storedMode = readStoredTheme()
    resolvedTheme    = resolveTheme(storedMode)
    themeSource      = storedMode === 'system' ? 'system' : 'stored'
  }
  applyTheme(resolvedTheme)

  // ── Lang resolution ──
  let langSource: 'url' | 'stored' | 'default' | 'embed'
  let resolvedLang: Lang
  if (urlLang) {
    langSource   = 'url'
    resolvedLang = urlLang
    try { localStorage.setItem(LANG_KEY, urlLang) } catch { /* ignore */ }
    try { localStorage.setItem(EMBED_LANG_STORAGE_KEY, urlLang) } catch { /* ignore */ }
  } else if (inEmbed) {
    const { lang, source } = resolveEmbeddedLang({
      urlLang:          null,
      embedStored:      readStoredEmbedLang(),
      userPref:         null,
      referrerLang:     readSallaReferrerLang(),
      navigatorLang:    null,
      documentLang:     readDocumentLang(),
      documentRtl:      isDocumentRtl(),
      inSallaEmbedded:  true,
    })
    resolvedLang = lang
    langSource   = source === 'stored' ? 'stored' : source === 'default' ? 'default' : 'embed'
    persistEmbeddedLangWithSource(lang, source)
  } else {
    const stored = readStoredLang()
    if (stored) {
      langSource   = 'stored'
      resolvedLang = stored
    } else {
      langSource   = 'default'
      resolvedLang = 'ar'
    }
  }
  applyLang(resolvedLang)

  if (consumedAny) {
    // eslint-disable-next-line no-console
    console.info(
      '[bootstrap] applied Salla handoff preferences | theme=%s (%s) | lang=%s (%s)',
      resolvedTheme, themeSource, resolvedLang, langSource,
    )
  }

  return {
    theme:  resolvedTheme,
    lang:   resolvedLang,
    source: { theme: themeSource, lang: langSource },
  }
}
