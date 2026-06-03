/**
 * Smoke test for embedded locale/theme resolution.
 *
 * Run:  npm run check:resolvers   (from dashboard/)
 */
import { resolveEmbeddedLang } from '../src/i18n/embeddedLocale.ts'
import { resolveEmbeddedTheme } from '../src/i18n/embeddedTheme.ts'

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
    name: 'embedded: nahla-theme=dark → light default (Salla light UI)',
    input: { inSallaEmbedded: true, userResolved: 'dark', systemTheme: 'dark' },
    want: 'light',
  },
  {
    name: 'embedded: OS dark ignored → light default',
    input: { inSallaEmbedded: true, systemTheme: 'dark' },
    want: 'light',
  },
  {
    name: 'embedded: URL ?theme=dark wins',
    input: { inSallaEmbedded: true, urlTheme: 'dark', userResolved: 'light' },
    want: 'dark',
  },
  {
    name: 'embedded: sticky nahla-embedded-theme=dark preserved',
    input: { inSallaEmbedded: true, embedStored: 'dark' },
    want: 'dark',
  },
  {
    name: 'embedded: Salla postMessage dark',
    input: { inSallaEmbedded: true, sallaMessageTheme: 'dark' },
    want: 'dark',
  },
  {
    name: 'standalone: user dark pref still works',
    input: { inSallaEmbedded: false, userResolved: 'dark' },
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

if (failed > 0) {
  console.error(`\n${failed} case(s) failed`)
  process.exit(1)
}
console.log(`\nAll ${embedLangCases.length + embedThemeCases.length} resolver cases passed.`)
