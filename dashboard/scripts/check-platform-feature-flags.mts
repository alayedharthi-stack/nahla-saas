/**
 * Regression checks for platform UX feature-flag scaffold.
 *
 * Run: npm run check:platform-feature-flags (from dashboard/)
 */
import { existsSync, readFileSync } from 'node:fs'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const flagsPath = new URL('../src/lib/platformFeatureFlags.ts', import.meta.url)

assert('platformFeatureFlags.ts exists', existsSync(flagsPath))

const source = existsSync(flagsPath) ? readFileSync(flagsPath, 'utf8') : ''

assert(
  'isNavSimplified8Enabled is exported',
  source.includes('export function isNavSimplified8Enabled'),
)

assert(
  'isOverviewCommandCenterEnabled is exported',
  source.includes('export function isOverviewCommandCenterEnabled'),
)

assert(
  'VITE_PLATFORM_NAV_SIMPLIFIED_8 env key is referenced',
  source.includes('VITE_PLATFORM_NAV_SIMPLIFIED_8'),
)

assert(
  'VITE_OVERVIEW_COMMAND_CENTER env key is referenced',
  source.includes('VITE_OVERVIEW_COMMAND_CENTER'),
)

assert(
  'missing env defaults to false (undefined guard)',
  source.includes('if (value === undefined) return false'),
)

assert(
  'only true/1 are treated as enabled',
  source.includes("normalized === 'true'") && source.includes("normalized === '1'"),
)

if (failed > 0) {
  console.error(`\n${failed} platform feature-flag check(s) failed`)
  process.exit(1)
}

console.log('\nAll platform feature-flag checks passed.')
