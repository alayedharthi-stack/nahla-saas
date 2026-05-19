/**
 * Tiny parity check for the embedded i18n dictionary.
 * ─────────────────────────────────────────────────────
 * Verifies that every leaf path that exists in `ar` also exists in `en`
 * (and vice-versa), and that no translation is an empty string except
 * for keys explicitly whitelisted as intentionally empty.
 *
 * Run:  npx tsx scripts/check-embedded-i18n.mts
 * Exit: 0 on success, 1 on any mismatch.
 */
import { EMBEDDED_STRINGS } from '../src/i18n/embedded'

type Path = string

function paths(obj: unknown, prefix = ''): Path[] {
  if (obj === null || typeof obj !== 'object') return [prefix]
  return Object.entries(obj as Record<string, unknown>)
    .flatMap(([k, v]) => paths(v, prefix ? `${prefix}.${k}` : k))
}

const ar = paths(EMBEDDED_STRINGS.ar).sort()
const en = paths(EMBEDDED_STRINGS.en).sort()
const inArNotEn = ar.filter(p => !en.includes(p))
const inEnNotAr = en.filter(p => !ar.includes(p))

if (inArNotEn.length || inEnNotAr.length) {
  console.error('FAIL: i18n parity broken')
  if (inArNotEn.length) console.error('  Missing in EN:', inArNotEn)
  if (inEnNotAr.length) console.error('  Missing in AR:', inEnNotAr)
  process.exit(1)
}

// Whitelist intentionally-empty leaves (currently just storeNameEmpty placeholder).
const allowEmpty = new Set(['status.storeNameEmpty'])

for (const lang of ['ar', 'en'] as const) {
  for (const p of paths(EMBEDDED_STRINGS[lang])) {
    const value = p.split('.').reduce<unknown>(
      (acc, key) => (acc as Record<string, unknown> | undefined)?.[key],
      EMBEDDED_STRINGS[lang],
    )
    if (typeof value !== 'string') {
      console.error('FAIL: non-string leaf', lang, p, value)
      process.exit(1)
    }
    if (value.length === 0 && !allowEmpty.has(p)) {
      console.error('FAIL: empty translation', lang, p)
      process.exit(1)
    }
  }
}

console.log(`OK i18n parity — ${ar.length} keys x 2 langs (ar, en)`)
