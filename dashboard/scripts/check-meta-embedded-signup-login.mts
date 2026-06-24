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

let failed = 0

const opts = buildEmbeddedSignupFbLoginOptions('998852939415057')
if (opts.response_type !== 'code') {
  failed++
  console.error(`FAIL response_type — got '${opts.response_type}', want 'code'`)
} else {
  console.log('OK   response_type === code')
}

if ('override_default_response_type' in opts) {
  failed++
  console.error('FAIL override_default_response_type must not be set')
} else {
  console.log('OK   no override_default_response_type')
}

for (const forbidden of ["response_type: 'code,token'", 'response_type: "code,token"', "response_type: 'token'", 'override_default_response_type']) {
  if (connectPage.includes(forbidden)) {
    failed++
    console.error(`FAIL WhatsAppConnect.tsx still contains ${forbidden}`)
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
