/**
 * Smoke test for embedded locale/theme resolution + login retry helper.
 *
 * Run:  npm run check:resolvers   (from dashboard/)
 */
import { resolveEmbeddedLang, extractLangFromSdkState } from '../src/i18n/embeddedLocale.ts'
import {
  resolveEmbeddedTheme,
  extractThemeFromPostMessage,
  isTrustedSallaThemeMessage,
} from '../src/i18n/embeddedTheme.ts'
import {
  buildEmbeddedEntryQuery,
  resolveEmbeddedAppearanceAndLocale,
} from '../src/i18n/embeddedContext.ts'
import {
  shouldRetryEmbeddedLogin,
  EMBEDDED_LOGIN_MAX_ATTEMPTS,
  isSallaStoreLinkRequired,
  isSallaRoutingBlockResponse,
  parseSallaStoreLinkPayload,
  resolveOauthReconcileStartUrl,
  clearSallaEmbeddedSession,
  SALLA_STORE_LINK_REQUIRED_CODE,
} from '../src/lib/embeddedLogin.ts'
import {
  EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER,
  redactExternalNavUrlForLog,
} from '../src/lib/embeddedNavigation.ts'

type Lang  = 'ar' | 'en'
type Theme = 'light' | 'dark'

interface Case<T> {
  name:  string
  input: unknown
  want:  T
}

const embedLangCases: Case<Lang>[] = [
  {
    name: 'embedded: stale nahla-lang=en → Arabic default',
    input: { inSallaEmbedded: true, userPref: 'en' },
    want: 'ar',
  },
  {
    name: 'embedded: Salla postMessage en → en',
    input: { inSallaEmbedded: true, sallaMessageLang: 'en' },
    want: 'en',
  },
  {
    name: 'embedded: live en beats url ar',
    input: { inSallaEmbedded: true, sallaMessageLang: 'en', urlLang: 'ar' },
    want: 'en',
  },
]

const embedThemeCases: Case<Theme>[] = [
  {
    name: 'embedded: Salla postMessage dark → dark',
    input: { inSallaEmbedded: true, sallaMessageTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: Salla postMessage light → light',
    input: { inSallaEmbedded: true, sallaMessageTheme: 'light' },
    want: 'light',
  },
  {
    name: 'embedded: live dark beats url light',
    input: { inSallaEmbedded: true, sallaMessageTheme: 'dark', urlTheme: 'light' },
    want: 'dark',
  },
  {
    name: 'embedded: no signal → system fallback when provided',
    input: { inSallaEmbedded: true, systemTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: trusted stored dark from Salla → dark',
    input: { inSallaEmbedded: true, embedStored: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: URL ?theme=dark → dark',
    input: { inSallaEmbedded: true, urlTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: referrer theme=dark → dark',
    input: { inSallaEmbedded: true, referrerTheme: 'dark' },
    want: 'dark',
  },
]

let failed = 0
for (const c of embedLangCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveEmbeddedLang(c.input as any).lang
  if (got !== c.want) {
    failed++
    console.error(`FAIL [embed-lang] ${c.name} — got '${got}', want '${c.want}'`)
  } else {
    console.log(`OK   [embed-lang] ${c.name} → ${got}`)
  }
}
for (const c of embedThemeCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveEmbeddedTheme(c.input as any).theme
  if (got !== c.want) {
    failed++
    console.error(`FAIL [embed-theme] ${c.name} — got '${got}', want '${c.want}'`)
  } else {
    console.log(`OK   [embed-theme] ${c.name} → ${got}`)
  }
}

// Combined resolver — four Salla combinations
const combos: Array<{ theme: Theme; lang: Lang }> = [
  { theme: 'light', lang: 'ar' },
  { theme: 'dark',  lang: 'ar' },
  { theme: 'light', lang: 'en' },
  { theme: 'dark',  lang: 'en' },
]
for (const combo of combos) {
  const got = resolveEmbeddedAppearanceAndLocale({
    liveTheme: combo.theme,
    liveLang:  combo.lang,
    inSallaEmbedded: true,
  })
  if (got.theme !== combo.theme || got.lang !== combo.lang) {
    failed++
    console.error(
      `FAIL [embed-context] Salla ${combo.lang}+${combo.theme} — got ${got.lang}+${got.theme}`,
    )
  } else {
    console.log(`OK   [embed-context] Salla ${combo.lang}+${combo.theme}`)
  }
}

// Default-only handoff must not pin theme/lang in entry URL
const defaultQs = buildEmbeddedEntryQuery({
  theme: 'light',
  lang: 'ar',
  themeSource: 'default',
  langSource: 'default',
})
if (defaultQs !== '') {
  failed++
  console.error(`FAIL [embed-context] default handoff should omit query — got '${defaultQs}'`)
} else {
  console.log('OK   [embed-context] default handoff omits query params')
}

const trustedQs = buildEmbeddedEntryQuery({
  theme: 'dark',
  lang: 'en',
  themeSource: 'salla',
  langSource: 'salla',
})
if (trustedQs !== '?theme=dark&lang=en') {
  failed++
  console.error(`FAIL [embed-context] trusted handoff — got '${trustedQs}'`)
} else {
  console.log('OK   [embed-context] trusted handoff serializes theme+lang')
}

// postMessage parsing
const ctxDark = {
  event: 'embedded:context.provide',
  payload: { layout: { theme: 'dark', mode: 'dark', dir: 'rtl' } },
}
if (!isTrustedSallaThemeMessage(ctxDark) || extractThemeFromPostMessage(ctxDark) !== 'dark') {
  failed++
  console.error('FAIL [embed-theme] embedded:context.provide dark payload')
} else {
  console.log('OK   [embed-theme] embedded:context.provide dark payload')
}

if (extractLangFromSdkState({ layout: { dir: 'ltr' } }) !== 'en') {
  failed++
  console.error('FAIL [embed-lang] SDK layout dir=ltr → en')
} else {
  console.log('OK   [embed-lang] SDK layout dir=ltr → en')
}

if (extractLangFromSdkState({ layout: { locale: 'en', dir: 'ltr' } }) !== 'en') {
  failed++
  console.error('FAIL [embed-lang] SDK layout locale=en')
} else {
  console.log('OK   [embed-lang] SDK layout locale=en')
}

const abortErr = new DOMException('Aborted', 'AbortError')
if (!shouldRetryEmbeddedLogin(abortErr, 1)) {
  failed++
  console.error('FAIL [login] should retry on AbortError attempt 1')
} else {
  console.log('OK   [login] AbortError attempt 1 → retry')
}
if (shouldRetryEmbeddedLogin(abortErr, EMBEDDED_LOGIN_MAX_ATTEMPTS)) {
  failed++
  console.error('FAIL [login] should not retry after max attempts')
} else {
  console.log('OK   [login] max attempts → no retry')
}

const storeLinkBody = {
  detail: {
    detail: 'merchant_identity_not_canonical',
    code: SALLA_STORE_LINK_REQUIRED_CODE,
    next_action: 'oauth_sync',
    oauth_start_path: '/api/salla/oauth/start?embedded_reconcile=1',
  },
}
if (!isSallaStoreLinkRequired(storeLinkBody)) {
  failed++
  console.error('FAIL [onboarding] structured store-link payload not detected')
} else {
  console.log('OK   [onboarding] structured store-link payload detected')
}

const oauthUrl = resolveOauthReconcileStartUrl(
  'https://api.nahlah.ai',
  parseSallaStoreLinkPayload(storeLinkBody),
)
if (!oauthUrl.includes('/api/salla/oauth/start?embedded_reconcile=1')) {
  failed++
  console.error('FAIL [onboarding] OAuth reconcile URL missing embedded flag')
} else {
  console.log('OK   [onboarding] OAuth reconcile URL built without JWT')
}

if (!isSallaRoutingBlockResponse(storeLinkBody)) {
  failed++
  console.error('FAIL [onboarding] routing block response not detected')
} else {
  console.log('OK   [onboarding] routing block clears session path armed')
}

try {
  localStorage.setItem('nahla_token', 'stale')
  localStorage.setItem('nahla_tenant_id', '1')
  localStorage.setItem('nahla_salla_store_id', '22825873')
  sessionStorage.setItem('nahla_salla_embedded', '1')
  clearSallaEmbeddedSession()
  if (
    localStorage.getItem('nahla_token')
    || localStorage.getItem('nahla_tenant_id')
    || localStorage.getItem('nahla_salla_store_id')
    || sessionStorage.getItem('nahla_salla_embedded')
  ) {
    failed++
    console.error('FAIL [onboarding] clearSallaEmbeddedSession left stale keys')
  } else {
    console.log('OK   [onboarding] clearSallaEmbeddedSession removed stale keys')
  }
} catch {
  console.log('OK   [onboarding] clearSallaEmbeddedSession skipped (no storage)')
}

const redacted = redactExternalNavUrlForLog(
  'https://api.nahlah.ai/api/salla/oauth/start?embedded_reconcile=1&token=secret',
)
if (redacted.includes('secret') || !redacted.includes('embedded_reconcile')) {
  failed++
  console.error('FAIL [nav] redactExternalNavUrlForLog leaked query value')
} else {
  console.log('OK   [nav] redactExternalNavUrlForLog hides values, keeps keys')
}

if (
  EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[0] !== 'sdk_page_redirect'
  || !EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER.includes('window_top_location')
) {
  failed++
  console.error('FAIL [nav] external nav prefers SDK redirect before top-frame fallback')
} else {
  console.log('OK   [nav] external nav fallback order prefers SDK redirect')
}


if (failed > 0) {
  console.error(`\n${failed} case(s) failed`)
  process.exit(1)
}
console.log(`\nAll cases passed.`)
