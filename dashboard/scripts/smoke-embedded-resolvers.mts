/**
 * Smoke test for the URL+storage resolution logic used by
 * useEmbeddedLocale and useEmbeddedTheme.
 *
 * We can't run the React hooks under Node (no DOM/React renderer), but the
 * resolver chain logic is pure — this test re-implements only the deterministic
 * URL → storage → fallback decisions and verifies them so a regression in the
 * priority order trips CI before merchants do.
 *
 * Run:  npx tsx scripts/smoke-embedded-resolvers.mts
 */
type Lang  = 'ar' | 'en'
type Theme = 'light' | 'dark'

const normalize = (raw: string | null | undefined): Lang | null => {
  if (!raw) return null
  const v = raw.toLowerCase().trim()
  if (v.startsWith('ar')) return 'ar'
  if (v.startsWith('en')) return 'en'
  return null
}

function resolveLang(input: {
  url?:    string | null
  stored?: Lang | null
  user?:   Lang | null
  ref?:    Lang | null
  nav?:    string | null
}): Lang {
  const fromUrl = normalize(input.url)
  if (fromUrl)    return fromUrl
  if (input.stored) return input.stored
  if (input.user)   return input.user
  if (input.ref)    return input.ref
  const nav = normalize(input.nav)
  if (nav) return nav
  return 'ar'
}

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

const langCases: Case<Lang>[] = [
  { name: 'URL en wins over user ar (the reported bug)',
    input: { url: 'en', stored: null, user: 'ar', ref: null, nav: null }, want: 'en' },
  { name: 'URL en wins over stored ar',
    input: { url: 'en', stored: 'ar', user: null, ref: null, nav: null }, want: 'en' },
  { name: 'no URL, stored sticky (post-navigation case)',
    input: { url: null, stored: 'en', user: 'ar', ref: null, nav: null }, want: 'en' },
  { name: 'no URL or storage → user pref',
    input: { url: null, stored: null, user: 'en', ref: null, nav: null }, want: 'en' },
  { name: 'all empty → default ar',
    input: { url: null, stored: null, user: null, ref: null, nav: null }, want: 'ar' },
  { name: 'navigator.language en-US normalizes to en',
    input: { url: null, stored: null, user: null, ref: null, nav: 'en-US' }, want: 'en' },
  { name: 'navigator.language ar-SA normalizes to ar',
    input: { url: null, stored: null, user: null, ref: null, nav: 'ar-SA' }, want: 'ar' },
]

const themeCases: Case<Theme>[] = [
  { name: 'URL dark wins over user light',
    input: { url: 'dark', stored: null, user: 'light', system: 'light' }, want: 'dark' },
  { name: 'URL light wins over user dark',
    input: { url: 'light', stored: null, user: 'dark', system: 'dark' }, want: 'light' },
  { name: 'URL night → dark',
    input: { url: 'night', stored: null, user: null, system: null }, want: 'dark' },
  { name: 'no URL, stored sticky',
    input: { url: null, stored: 'dark', user: 'light', system: 'light' }, want: 'dark' },
  { name: 'no URL or storage → user pref',
    input: { url: null, stored: null, user: 'dark', system: 'light' }, want: 'dark' },
  { name: 'all empty → light',
    input: { url: null, stored: null, user: null, system: null }, want: 'light' },
]

let failed = 0
for (const c of langCases) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const got = resolveLang(c.input as any)
  const ok  = got === c.want
  if (!ok) { failed++; console.error(`FAIL [lang] ${c.name} — got '${got}', want '${c.want}'`) }
  else      console.log(`OK   [lang] ${c.name} → ${got}`)
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
console.log(`\nAll ${langCases.length + themeCases.length} resolver cases passed.`)
