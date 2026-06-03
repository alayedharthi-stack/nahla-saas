/**
 * Smoke test for embedded locale/theme resolution + login retry helper.
 *
 * Run:  npm run check:resolvers   (from dashboard/)
 */
import { resolveEmbeddedLang } from '../src/i18n/embeddedLocale.ts'
import {
  resolveEmbeddedTheme,
  extractThemeFromPostMessage,
  isTrustedSallaThemeMessage,
} from '../src/i18n/embeddedTheme.ts'
import {
  shouldRetryEmbeddedLogin,
  EMBEDDED_LOGIN_MAX_ATTEMPTS,
} from '../src/lib/embeddedLogin.ts'

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
    name: 'embedded: stale stored dark without trusted source → light default',
    input: { inSallaEmbedded: true, embedStored: null, sallaMessageTheme: null },
    want: 'light',
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

// postMessage parsing
const ctxDark = {
  event: 'embedded:context.provide',
  payload: { layout: { theme: 'dark', mode: 'dark' } },
}
if (!isTrustedSallaThemeMessage(ctxDark) || extractThemeFromPostMessage(ctxDark) !== 'dark') {
  failed++
  console.error('FAIL [embed-theme] embedded:context.provide dark payload')
} else {
  console.log('OK   [embed-theme] embedded:context.provide dark payload')
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

if (failed > 0) {
  console.error(`\n${failed} case(s) failed`)
  process.exit(1)
}
console.log(`\nAll cases passed.`)
