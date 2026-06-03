/**
 * Smoke test for embedded locale/theme resolution.
 *
 * Run:  npm run check:resolvers   (from dashboard/)
 */
import { resolveEmbeddedLang } from '../src/i18n/embeddedLocale.ts'

type Lang  = 'ar' | 'en'
type Theme = 'light' | 'dark'

const normTheme = (raw: string | null | undefined): Theme | null => {
  if (!raw) return null
  const v = raw.toLowerCase().trim()
  if (v === 'dark' || v === 'night') return 'dark'
  if (v === 'light' || v === 'day')  return 'light'
  return null
}

function resolveTheme(input: {
  url?:    string | null
  stored?: Theme | null
  user?:   Theme | null
  system?: Theme | null
}): Theme {
  const fromUrl = normTheme(input.url)
  if (fromUrl)      return fromUrl
  if (input.stored) return input.stored
  if (input.user)   return input.user
  if (input.system) return input.system
  return 'light'
}

interface Case<T> {
  name:  string
  input: unknown
  want:  T
}

const embedLangCases: Case<Lang>[] = [
  {
    name: 'embedded: stale nahla-lang=en → Arabic default (reported bug)',
    input: { inSallaEmbedded: true, userPref: 'en', embedStored: null, referrerLang: null },
    want: 'ar',
  },
  {
    name: 'embedded: navigator en-US ignored → Arabic default',
    input: { inSallaEmbedded: true, navigatorLang: 'en', userPref: 'en' },
    want: 'ar',
  },
  {
    name: 'embedded: URL ?lang=en wins',
    input: { inSallaEmbedded: true, urlLang: 'en', userPref: 'ar' },
    want: 'en',
  },
  {
    name: 'embedded: sticky nahla-embedded-lang=en preserved',
    input: { inSallaEmbedded: true, embedStored: 'en', userPref: 'ar' },
    want: 'en',
  },
  {
    name: 'embedded: Salla referrer /en/ → English',
    input: { inSallaEmbedded: true, referrerLang: 'en' },
    want: 'en',
  },
  {
    name: 'embedded: s.salla.sa/embedded without /en/ → Arabic via referrer',
    input: { inSallaEmbedded: true, referrerLang: 'ar' },
    want: 'ar',
  },
  {
    name: 'embedded: postMessage locale → en',
    input: { inSallaEmbedded: true, sallaMessageLang: 'en' },
    want: 'en',
  },
  {
    name: 'standalone: user pref en still works',
    input: { inSallaEmbedded: false, userPref: 'en' },
    want: 'en',
  },
]

const themeCases: Case<Theme>[] = [
  { name: 'URL dark wins over user light',
    input: { url: 'dark', stored: null, user: 'light', system: 'light' }, want: 'dark' },
  { name: 'URL light wins over user dark',
    input: { url: 'light', stored: null, user: 'dark', system: 'dark' }, want: 'light' },
  { name: 'all empty → light',
    input: { url: null, stored: null, user: null, system: null }, want: 'light' },
]

let failed = 0
for (const c of embedLangCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveEmbeddedLang(c.input as any).lang
  const ok  = got === c.want
  if (!ok) { failed++; console.error(`FAIL [embed-lang] ${c.name} — got '${got}', want '${c.want}'`) }
  else      console.log(`OK   [embed-lang] ${c.name} → ${got}`)
}
for (const c of themeCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveTheme(c.input as any)
  const ok  = got === c.want
  if (!ok) { failed++; console.error(`FAIL [theme] ${c.name} — got '${got}', want '${c.want}'`) }
  else      console.log(`OK   [theme] ${c.name} → ${got}`)
}

if (failed > 0) {
  console.error(`\n${failed} case(s) failed`)
  process.exit(1)
}
console.log(`\nAll ${embedLangCases.length + themeCases.length} resolver cases passed.`)
