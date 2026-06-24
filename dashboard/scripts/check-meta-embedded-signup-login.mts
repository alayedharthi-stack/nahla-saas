/**
 * Guard: Embedded Signup FB.login must use authorization-code flow only.
 *
 * Run: npm run check:meta-embedded-signup-login   (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildEmbeddedSignupFbLoginOptions } from '../src/lib/metaEmbeddedSignupLogin.ts'

const __dir = dirname(fileURLToPath(import.meta.url))
const connectPage = readFileSync(
  join(__dir, '../src/pages/WhatsAppConnect.tsx'),
  'utf8',
)
const loginLib = readFileSync(
  join(__dir, '../src/lib/metaEmbeddedSignupLogin.ts'),
  'utf8',
)

let failed = 0

const opts = buildEmbeddedSignupFbLoginOptions('998852939415057')
if (opts.response_type !== 'code') {
  failed++
  console.error(`FAIL response_type — got '${opts.response_type}', want 'code'`)
} else {
  console.log('OK   response_type === code')
}

if (opts.override_default_response_type !== true) {
  failed++
  console.error('FAIL override_default_response_type must be true')
} else {
  console.log('OK   override_default_response_type === true')
}

const forbiddenInSource = [
  "response_type: 'code,token'",
  'response_type: "code,token"',
  "response_type: 'token'",
  'response_type: "token"',
]
for (const src of [loginLib, connectPage]) {
  const label = src === loginLib ? 'metaEmbeddedSignupLogin.ts' : 'WhatsAppConnect.tsx'
  for (const forbidden of forbiddenInSource) {
    if (src.includes(forbidden)) {
      failed++
      console.error(`FAIL ${label} still contains ${forbidden}`)
    }
  }
}

if (!connectPage.includes('buildEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must use buildEmbeddedSignupFbLoginOptions')
} else {
  console.log('OK   WhatsAppConnect uses shared login options helper')
}

if (!connectPage.includes('handleExchange(code, undefined)')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must call handleExchange(code, undefined)')
} else {
  console.log('OK   exchange uses auth code only')
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll meta embedded signup login checks passed.')
