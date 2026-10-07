/**
 * CI guard: the /catalog Meta consent card must treat the callback fragment
 * (#meta_catalog_consent=<code>) as a hint only. "connected" is claimed only
 * when the fresh authenticated status shows an active authorization for
 * exactly the approved catalog and business.
 *
 * Run: npm run check:meta-catalog-consent-result
 */
import { readFileSync } from 'node:fs'
import { resolveConsentBanner } from '../src/lib/metaCatalogConsentResult'

let failed = 0
function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const CATALOG = '880000000000001'
const BUSINESS = '770000000000001'
const known = new Set(['connected', 'denied', 'replayed', 'error'])
const approved = { catalog_id: CATALOG, business_id: BUSINESS }
const base = { available: true, reason: null, requested_scopes: ['catalog_management', 'business_management'], approved }

const active = { ...base, authorization: { state: 'active' as const, catalog_id: CATALOG, business_id: BUSINESS } }
const none = { ...base, authorization: { state: 'none' as const } }
const inactive = { ...base, authorization: { state: 'inactive' as const, catalog_id: CATALOG, business_id: BUSINESS } }
const expired = { ...base, authorization: { state: 'expired' as const, catalog_id: CATALOG, business_id: BUSINESS } }
const otherCatalog = { ...base, authorization: { state: 'active' as const, catalog_id: '880000000000009', business_id: BUSINESS } }
const otherBusiness = { ...base, authorization: { state: 'active' as const, catalog_id: CATALOG, business_id: '770000000000009' } }
const unavailable = { available: false, reason: 'disabled', requested_scopes: [], authorization: { state: 'none' as const } }

const okBanner = resolveConsentBanner('connected', active, false, known)
assert('connected + fresh active matching authorization → ok', okBanner?.tone === 'ok' && okBanner.key === 'connected')

for (const [name, status] of [
  ['state none', none], ['state inactive', inactive], ['state expired', expired],
  ['active for another catalog', otherCatalog], ['active for another business', otherBusiness],
  ['feature unavailable', unavailable],
] as const) {
  const b = resolveConsentBanner('connected', status as never, false, known)
  assert(`crafted connected + ${name} → not confirmed`, b?.tone === 'warn' && b.key === 'notConfirmed', JSON.stringify(b))
}
const activeButUnavailable = { ...active, available: false, reason: 'entitlement_missing' }
const unavailableBanner = resolveConsentBanner('connected', activeButUnavailable as never, false, known)
assert('connected + active row but available=false → not confirmed',
  unavailableBanner?.tone === 'warn' && unavailableBanner.key === 'notConfirmed')
const staleActive = resolveConsentBanner('connected', active, true, known)
assert('connected + stale active status but fresh read failed → not confirmed',
  staleActive?.tone === 'warn' && staleActive.key === 'notConfirmed')
const failedStatus = resolveConsentBanner('connected', null, true, known)
assert('connected + status read failed → not confirmed', failedStatus?.tone === 'warn' && failedStatus.key === 'notConfirmed')
assert('connected + status still loading → nothing shown yet', resolveConsentBanner('connected', null, false, known) === null)
const denied = resolveConsentBanner('denied', active, false, known)
assert('failure code stays a warning even with an active authorization', denied?.tone === 'warn' && denied.key === 'denied')
const unknown = resolveConsentBanner('made_up_code', active, false, known)
assert('unknown code → generic error, never ok', unknown?.tone === 'warn' && unknown.key === 'error')
assert('no hint → nothing', resolveConsentBanner(null, active, false, known) === null)

// The card must use the resolver, never the raw fragment, for the success tone.
const card = readFileSync(new URL('../src/components/catalog/CatalogMetaConsentCard.tsx', import.meta.url), 'utf8')
assert('card derives the banner from resolveConsentBanner', card.includes('resolveConsentBanner(result, status, loadError'))
assert('card never trusts the fragment for success', !/result\s*===\s*'connected'/.test(card))
assert('card accepts only Meta dialog URLs', card.includes("META_DIALOG_ORIGIN = 'https://www.facebook.com'"))

// Result banners never claim storage state they cannot prove: a storage or
// generic failure can follow a committed row or a lost commit acknowledgment,
// and an earlier authorization may still exist. Those codes say the save
// could not be confirmed and point to the fresh status instead.
for (const [lang, banned, refresh] of [
  ['en', 'Nothing was stored', 'Refresh the status'],
  ['ar', 'لم يُحفظ أي شيء', 'حدّث الحالة'],
] as const) {
  const src = readFileSync(new URL(`../src/i18n/${lang}.ts`, import.meta.url), 'utf8')
  const start = src.indexOf('    metaConsent: {')
  const block = src.slice(start, src.indexOf('    whatsappSync: {', start))
  assert(`${lang}: metaConsent block found`, start >= 0 && block.includes('results: {'))
  assert(`${lang}: no unproven "nothing stored" claim`, !block.includes(banned))
  for (const key of ['storage_unavailable', 'persist_unverified', 'error']) {
    const line = block.split('\n').find((l) => l.trimStart().startsWith(`${key}:`)) ?? ''
    assert(`${lang}: ${key} points to a status refresh`, line.includes(refresh), line.trim())
  }
}

if (failed) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nmeta catalog consent result policy OK')
