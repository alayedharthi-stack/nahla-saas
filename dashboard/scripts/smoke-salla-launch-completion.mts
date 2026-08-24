/**
 * Smoke checks for SallaLaunch post-OAuth completion handoff (Repair A).
 * Run: npx --yes tsx@4 scripts/smoke-salla-launch-completion.mts
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const ROOT = join(import.meta.dirname, '..')
const source = readFileSync(join(ROOT, 'src/pages/SallaLaunch.tsx'), 'utf8')

let failed = 0
function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const replaceIdx = source.indexOf('window.history.replaceState')
const fetchIdx = source.indexOf('fetch(`${API_BASE}/salla/session/resolve-launch`')

assert('L sanitizeInternalNextPath present', source.includes('function sanitizeInternalNextPath'))
assert('L token stripped before resolve-launch fetch', replaceIdx >= 0 && fetchIdx >= 0 && replaceIdx < fetchIdx)
assert('L stale session cleared before persist', source.indexOf('clearEmbeddedSession()') < source.indexOf("localStorage.setItem('nahla_token'"))
assert('M external next URLs rejected', source.includes("trimmed.includes('://')"))

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll SallaLaunch completion smoke checks passed')
