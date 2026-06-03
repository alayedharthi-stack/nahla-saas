/**
 * Smoke test for embedded locale/theme resolution + login retry helper.
 *
 * Run:  npm run check:resolvers   (from dashboard/)
 */
import { resolveEmbeddedLang } from '../src/i18n/embeddedLocale.ts'
import { resolveEmbeddedTheme } from '../src/i18n/embeddedTheme.ts'
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
    input: { inSallaEmbedded: true, userPref: 'en', embedStored: null, referrerLang: null },
    want: 'ar',
  },
  {
    name: 'embedded: URL ?lang=en wins',
    input: { inSallaEmbedded: true, urlLang: 'en', userPref: 'ar' },
    want: 'en',
  },
  {
    name: 'standalone: user pref en still works',
    input: { inSallaEmbedded: false, userPref: 'en' },
    want: 'en',
  },
]

const embedThemeCases: Case<Theme>[] = [
  {
    name: 'embedded: stale nahla-embedded-theme=dark → light default',
    input: { inSallaEmbedded: true, embedStored: 'dark', userResolved: 'dark' },
    want: 'light',
  },
  {
    name: 'embedded: OS dark ignored → light default',
    input: { inSallaEmbedded: true, systemTheme: 'dark' },
    want: 'light',
  },
  {
    name: 'embedded: URL ?theme=dark wins',
    input: { inSallaEmbedded: true, urlTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: Salla postMessage dark',
    input: { inSallaEmbedded: true, sallaMessageTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'standalone: stored dark preserved outside iframe',
    input: { inSallaEmbedded: false, embedStored: 'dark' },
    want: 'dark',
  },
]

let failed = 0
for (const c of embedLangCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveEmbeddedLang(c.input as any).lang
  const ok  = got === c.want
  if (!ok) { failed++; console.error(`FAIL [embed-lang] ${c.name} — got '${got}', want '${c.want}'`) }
  else      console.log(`OK   [embed-lang] ${c.name} → ${got}`)
}
for (const c of embedThemeCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveEmbeddedTheme(c.input as any).theme
  const ok  = got === c.want
  if (!ok) { failed++; console.error(`FAIL [embed-theme] ${c.name} — got '${got}', want '${c.want}'`) }
  else      console.log(`OK   [embed-theme] ${c.name} → ${got}`)
}

// Login retry helper
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
if (shouldRetryEmbeddedLogin(new Error('network'), 1)) {
  failed++
  console.error('FAIL [login] should not retry on generic Error')
} else {
  console.log('OK   [login] generic Error → no retry')
}

if (failed > 0) {
  console.error(`\n${failed} case(s) failed`)
  process.exit(1)
}
console.log(`\nAll ${embedLangCases.length + embedThemeCases.length + 3} cases passed.`)
