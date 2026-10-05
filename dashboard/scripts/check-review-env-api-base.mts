/**
 * CI guard: the catalog_management review environment build must fail closed
 * on a missing or production API base, and the rule must be enforced at build
 * time (vite.config.ts) and at runtime (auth.ts + main.tsx) from the same
 * policy module.
 *
 * Run: npm run check:review-env-api-base
 */
import { readFileSync } from 'node:fs'
import {
  checkReviewApiBase,
  describeReviewApiBaseFailure,
  REVIEW_ENV_FLAG,
} from '../src/lib/reviewEnvironmentPolicy.js'

let failed = 0
function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

// ── Policy table ─────────────────────────────────────────────────────────────
const review = { [REVIEW_ENV_FLAG]: '1' }
assert('review + no api base → api_base_missing',
  checkReviewApiBase({ ...review }).failure === 'api_base_missing')
assert('review + production api → api_base_is_production',
  checkReviewApiBase({ ...review, VITE_API_BASE: 'https://api.nahlah.ai' }).failure === 'api_base_is_production')
assert('review + production railway fallback → api_base_is_production',
  checkReviewApiBase({ ...review, VITE_API_BASE: 'https://nahla-saas-production.up.railway.app/' }).failure === 'api_base_is_production')
assert('review + legacy alias pointing at production is still rejected',
  checkReviewApiBase({ ...review, VITE_API_URL: 'https://api.nahlah.ai' }).failure === 'api_base_is_production')
assert('review + Dockerfile localhost default → api_base_is_localhost_default',
  checkReviewApiBase({ ...review, VITE_API_BASE: 'http://localhost:8000' }).failure === 'api_base_is_localhost_default')
assert('review + plain http review host → api_base_not_https',
  checkReviewApiBase({ ...review, VITE_API_BASE: 'http://api.catalog-review.nahlah.ai' }).failure === 'api_base_not_https')
assert('review + explicit review api → ok',
  checkReviewApiBase({ ...review, VITE_API_BASE: 'https://api.catalog-review.nahlah.ai' }).ok === true)
assert('flag off + no api base → ok (production behaviour unchanged)',
  checkReviewApiBase({}).ok === true && checkReviewApiBase({}).enabled === false)
assert('flag off + production api → ok (production behaviour unchanged)',
  checkReviewApiBase({ VITE_API_BASE: 'https://api.nahlah.ai' }).ok === true)
assert('failure description names the variable to set',
  describeReviewApiBaseFailure(checkReviewApiBase({ ...review })).includes('VITE_API_BASE'))

// ── Wiring: build-time and runtime both use the policy ───────────────────────
const viteConfig = readFileSync(new URL('../vite.config.ts', import.meta.url), 'utf8')
assert('vite.config.ts enforces the policy at build time',
  viteConfig.includes('checkReviewApiBase(') && viteConfig.includes('throw new Error('))

const authSource = readFileSync(new URL('../src/auth.ts', import.meta.url), 'utf8')
assert('auth.ts fails closed in review mode (no production default, no override)',
  authSource.includes('assertReviewApiBase()') && authSource.includes('isReviewEnvironment()'))

const mainSource = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8')
assert('main.tsx renders a failure screen instead of the app when misconfigured',
  mainSource.includes('reviewApiBaseCheck()'))

// ── No silent production fallback anywhere in src (literal production API hosts) ──
import { readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
const SRC = new URL('../src/', import.meta.url).pathname
const ALLOWED_LITERAL_FILES = new Set([
  'lib/reviewEnvironmentPolicy.js', 'lib/reviewEnvironmentPolicy.d.ts', // the policy and its deny-list
  'auth.ts',                        // historic production default, bypassed in review mode
  'pages/Login.tsx',                // operator diagnostics panel (hidden in review mode)
  'pages/Integrations.tsx',         // documentation of webhook URLs shown to merchants
  'pages/Settings.tsx',             // read-only display of the baked API base
  'i18n/en.ts', 'i18n/ar.ts', 'i18n/embedded.ts',
])
const offenders: string[] = []
function walk(dir: string) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { walk(p); continue }
    if (!/\.(ts|tsx)$/.test(name)) continue
    const rel = relative(SRC, p).replace(/\\/g, '/')
    if (ALLOWED_LITERAL_FILES.has(rel)) continue
    const src = readFileSync(p, 'utf8')
    if (/https:\/\/api\.nahlah\.ai|nahla-saas-production\.up\.railway\.app/.test(src)) offenders.push(rel)
  }
}
walk(SRC)
assert('no literal production API host outside the allow-listed files', offenders.length === 0, offenders.join(', '))

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nreview-env api-base policy OK')
