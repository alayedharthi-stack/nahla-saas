/**
 * Unified Salla embedded appearance + locale resolution (no React).
 */
import {
  type EmbeddedLang,
  type EmbeddedLangSource,
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
import {
  type EmbeddedTheme,
  type EmbeddedThemeSource,
  resolveEmbeddedTheme,
  readUrlEmbeddedTheme,
  readTrustedStoredEmbedTheme,
  readSallaReferrerTheme,
  readStoredUserResolvedTheme,
  readSystemTheme,
} from './embeddedTheme'

export interface EmbeddedAppearanceLocale {
  theme:       EmbeddedTheme
  lang:        EmbeddedLang
  themeSource: EmbeddedThemeSource
  langSource:  EmbeddedLangSource
}

export interface ResolveEmbeddedContextInput {
  search?:           string
  liveTheme?:        EmbeddedTheme | null
  liveLang?:         EmbeddedLang | null
  inSallaEmbedded?:  boolean
}

/** Sources safe to serialize into /app/entry handoff query params. */
export function isEmbedHandoffSource(
  source: EmbeddedThemeSource | EmbeddedLangSource,
): boolean {
  return source === 'url' || source === 'salla' || source === 'stored' || source === 'referrer'
}

export function resolveEmbeddedAppearanceAndLocale(
  input: ResolveEmbeddedContextInput = {},
): EmbeddedAppearanceLocale {
  const embedded = input.inSallaEmbedded ?? isSallaEmbeddedIframe()
  const search   = input.search

  const { theme, source: themeSource } = resolveEmbeddedTheme({
    urlTheme:          readUrlEmbeddedTheme(search),
    sallaMessageTheme: input.liveTheme ?? null,
    embedStored:       embedded ? readTrustedStoredEmbedTheme() : null,
    referrerTheme:     embedded ? readSallaReferrerTheme() : null,
    userResolved:      embedded ? null : readStoredUserResolvedTheme(),
    systemTheme:       embedded ? readSystemTheme() : readSystemTheme(),
    inSallaEmbedded:   embedded,
  })

  const { lang, source: langSource } = resolveEmbeddedLang({
    urlLang:           readUrlEmbeddedLang(search),
    sallaMessageLang:  input.liveLang ?? null,
    embedStored:       readStoredEmbedLang(),
    userPref:          embedded ? null : readStoredUserLang(),
    referrerLang:      readSallaReferrerLang(),
    navigatorLang:     embedded ? null : readNavigatorLang(),
    documentLang:      readDocumentLang(),
    documentRtl:       isDocumentRtl(),
    inSallaEmbedded:   embedded,
  })

  return { theme, lang, themeSource, langSource }
}

/** Build /app/entry query — omits theme/lang when source is default/system only. */
export function buildEmbeddedEntryQuery(
  ctx?: Partial<EmbeddedAppearanceLocale> & { search?: string },
): string {
  const resolved = resolveEmbeddedAppearanceAndLocale({
    search:          ctx?.search,
    inSallaEmbedded: true,
  })

  const theme       = ctx?.theme       ?? resolved.theme
  const lang        = ctx?.lang        ?? resolved.lang
  const themeSource = ctx?.themeSource ?? resolved.themeSource
  const langSource  = ctx?.langSource  ?? resolved.langSource

  const out = new URLSearchParams()
  if (isEmbedHandoffSource(themeSource)) out.set('theme', theme)
  if (isEmbedHandoffSource(langSource))  out.set('lang', lang)

  const qs = out.toString()
  return qs ? `?${qs}` : ''
}
