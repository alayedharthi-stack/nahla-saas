/**
 * Shopify catalog import UI — pure model and source checks (no browser).
 *
 * Run: npm run check:shopify-catalog-import-ui (from dashboard/)
 *
 * Proves: the UI flag is env-only and default off; "imported" is shown only
 * with committed evidence of the current installation; a products-only import
 * never implies inventory; reasons map to fixed copy (never server text);
 * support impersonation cannot start; the card's static step is unchanged
 * while the flag is off.
 */
import { readFileSync } from 'node:fs'
import {
  IMPORT_REASON_CODES,
  displayState,
  importStepValue,
  parseImportStatus,
  projectImportFailure,
  reasonKey,
  startAllowed,
} from '../src/lib/shopifyConnection/catalogImport'
import { shopifyConnectionAr, shopifyConnectionEn } from '../src/i18n/shopifyConnectionLabels'

let passed = 0
let failed = 0
function assert(name: string, ok: boolean) {
  if (ok) {
    passed++
    console.log(`OK   ${name}`)
  } else {
    failed++
    console.error(`FAIL ${name}`)
  }
}

const src = (rel: string) => readFileSync(new URL(rel, import.meta.url), 'utf8')

const evidence = (over: Record<string, unknown> = {}) => ({
  completed_at: '2026-10-11T05:00:00+00:00', api_version: '2026-10', run: 1, products: 3, variants: 4,
  inventory_items: 4, removed: { products: 0, variants: 0 }, current_installation: true,
  inventory: { mode: 'scope_missing', locations: null, levels: null }, ...over,
})
const entry = (imp: Record<string, unknown>, status = 'active') => ({
  shop_domain: 'generic-shoes-store.myshopify.com', connection_status: status,
  import: { state: 'not_started', reason: null, started_at: null, last_success: null, can_start: true, ...imp },
})
const view = (connections: unknown[]) => parseImportStatus({ available: true, api_version: '2026-10', connections })

// ── Model ────────────────────────────────────────────────────────────────────
{
  const v = view([entry({ state: 'imported', last_success: evidence(), can_start: true })])
  assert('imported with committed current evidence', v?.entries[0].state === 'imported')
  assert('products-only import reports inventory not read', v?.entries[0].lastSuccess?.inventory.mode === 'scope_missing'
    && v?.entries[0].lastSuccess?.inventory.locations === null)
  const noEvidence = view([entry({ state: 'imported', last_success: null })])
  assert('imported without evidence is never shown as imported', noEvidence?.entries[0].state === 'unknown')
  const previous = view([entry({ state: 'imported', last_success: evidence({ current_installation: false }) })])
  assert('evidence of a previous installation is not "imported"', previous?.entries[0].state === 'unknown')
  const zeroInventory = view([entry({ state: 'imported', last_success: evidence({
    inventory: { mode: 'scope_missing', locations: 0, levels: 0 } }) })])
  assert('scope_missing with numeric inventory counts is malformed', zeroInventory?.entries[0].lastSuccess === null
    && zeroInventory?.entries[0].state === 'unknown')
  const perLocation = view([entry({ state: 'imported', last_success: evidence({
    inventory: { mode: 'per_location', locations: 2, levels: 5 } }) })])
  assert('per-location evidence carries counts', perLocation?.entries[0].lastSuccess?.inventory.mode === 'per_location')
  const err = view([entry({ state: 'error', reason: 'catalog_too_large', last_success: evidence() })])
  assert('error keeps last success evidence', err?.entries[0].state === 'error' && err?.entries[0].lastSuccess !== null)
  assert('error reason maps to a fixed key', err?.entries[0].reasonKey === 'tooLarge')
  const odd = view([entry({ state: 'error', reason: 'Traceback: shpat_secret leaked' })])
  assert('unknown reason text maps to generic', odd?.entries[0].reasonKey === 'generic')
  const weird = view([entry({ state: 'done' })])
  assert('unknown state is unknown', weird?.entries[0].state === 'unknown')
  const importing = view([entry({ state: 'importing', can_start: true })])
  assert('no start while importing', importing?.entries[0].canStart === false)
  const inactive = view([entry({ state: 'not_started', can_start: true }, 'disconnected')])
  assert('no start without an active connection', inactive?.entries[0].canStart === false)
  assert('malformed status is null', parseImportStatus({ connections: 'x' }) === null && parseImportStatus(null) === null)
  assert('step value defaults to not_started', importStepValue(null) === 'not_started')
  const done = view([entry({ state: 'imported', last_success: evidence(), can_start: true })])!.entries[0]
  const ok = { available: true, parentActive: true, readOnly: false, busy: false }
  assert('start allowed only when every authority agrees', startAllowed(done, ok)
    && !startAllowed(done, { ...ok, available: false }) && !startAllowed(done, { ...ok, parentActive: false })
    && !startAllowed(done, { ...ok, readOnly: true }) && !startAllowed(done, { ...ok, busy: true }))
  const unavailable = parseImportStatus({ available: false, connections: [entry({ can_start: true })] })!
  assert('available=false never offers start even if an entry says can_start',
    !startAllowed(unavailable.entries[0], { ...ok, available: unavailable.available }))
  assert('a shop the parent no longer reports active is never shown complete',
    displayState(done, false) === 'unknown' && displayState(done, true) === 'imported')
  assert('every backend reason code maps to a key', [...IMPORT_REASON_CODES].every((c) => reasonKey(c) !== null))
  assert('failure projection never reads messages',
    projectImportFailure({ status: 409, code: 'import_in_progress', message: 'x' }).kind === 'refused'
    && projectImportFailure({ status: 404 }).kind === 'feature_off'
    && projectImportFailure({ status: 503 }).kind === 'uncertain'
    && projectImportFailure(new Error('boom')).kind === 'uncertain'
    && projectImportFailure({ status: 403, code: 'support_session_refused' }).kind === 'refused')
}

// ── Copy ─────────────────────────────────────────────────────────────────────
{
  for (const [lang, labels] of [['ar', shopifyConnectionAr], ['en', shopifyConnectionEn]] as const) {
    const c = labels.catalogImport
    const keys = ['connection', 'permission', 'tooLarge', 'changed', 'interrupted', 'unavailable', 'inProgress',
      'sessionChanged', 'generic'] as const
    assert(`${lang}: every reason key has copy`, keys.every((k) => typeof c.reasons[k] === 'string' && c.reasons[k].length > 0))
    assert(`${lang}: mirror note says not sent to channels`, /WhatsApp|واتساب/.test(c.mirrorNote) && /Meta/.test(c.mirrorNote))
    assert(`${lang}: inventory-not-read copy exists`, c.inventoryNotRead.length > 0)
  }
}

// ── Sources ──────────────────────────────────────────────────────────────────
{
  const flags = src('../src/lib/platformFeatureFlags.ts')
  const start = flags.indexOf('export function isShopifyCatalogImportUiEnabled')
  const fn = flags.slice(start, flags.indexOf('}', start))
  assert('import UI flag is env-only and default off', start >= 0
    && fn.includes('isTruthyEnv(import.meta.env.VITE_SHOPIFY_CATALOG_IMPORT_UI)') && !fn.includes('localStorage'))
  const card = src('../src/components/integrations/ShopifyConnectionCard.tsx')
  assert('card keeps the static not-started step', card.includes(
    '<span className="text-slate-500" data-testid="shopify-step-import" data-step={steps.productImport}>— {copy.steps.productImportNotStarted}</span>'))
  assert('card renders the import step only behind the flag', card.includes('isShopifyCatalogImportUiEnabled() && state.load === \'ready\''))
  const step = src('../src/components/integrations/ShopifyCatalogImportStep.tsx')
  const code = step.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  assert('step: start refused for read-only sessions', code.includes('if (readOnly || starting || parentBusy) return')
    && code.includes('startAllowed(entry, { available, parentActive, readOnly, busy: parentBusy })'))
  assert('step: re-reads and fails closed on every parent read', code.includes('}, [refresh, parentStatus])')
    && code.includes("setLoad({ phase: 'loading' })"))
  assert('step: renders the evidence API version', code.includes('evidence.apiVersion'))
  assert('card binds the step to the parent status', card.includes('parentStatus={state.status}'))
  assert('card: only an in-flight connect or disconnect is busy (a failed connect is not)',
    card.includes("state.start.phase === 'starting' || state.start.phase === 'redirecting'")
    && !card.includes("state.start.phase !== 'idle'"))
  assert('step: no browser storage', !/localStorage|sessionStorage|indexedDB|document\.cookie/.test(code))
  assert('step: no console logging', !/console\./.test(code))
  assert('step: never renders an error message', !/\b(err|error|e|exc)\.message\b/.test(code))
  assert('step: no navigation', !/location\.(assign|href|replace)/.test(code))
  assert('step: session-bound responses', code.includes('key !== sessionKey()'))
  const model = src('../src/lib/shopifyConnection/catalogImport.ts')
  assert('model: no package imports', !/^import /m.test(model))
}

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) {
  console.error(`${failed} shopify catalog import UI check(s) failed`)
  process.exit(1)
}
console.log('shopify catalog import UI checks OK')
