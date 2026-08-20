/**
 * Guard: Embedded Signup FB.login must use authorization-code flow only.
 *
 * Run: npm run check:meta-embedded-signup-login   (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  buildCoexistenceEmbeddedSignupFbLoginOptions,
  buildEmbeddedSignupFbLoginOptions,
  parseEmbeddedSignupWindowMessage,
} from '../src/lib/metaEmbeddedSignupLogin.ts'

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

if (opts.extras.feature !== 'whatsapp_embedded_signup') {
  failed++
  console.error(`FAIL default extras.feature — got '${opts.extras.feature}', want 'whatsapp_embedded_signup'`)
} else {
  console.log('OK   default extras.feature === whatsapp_embedded_signup')
}

const coexistenceOpts = buildCoexistenceEmbeddedSignupFbLoginOptions('998852939415057')
if (coexistenceOpts.extras.featureType !== 'whatsapp_business_app_onboarding') {
  failed++
  console.error(`FAIL coexistence featureType — got '${coexistenceOpts.extras.featureType}', want 'whatsapp_business_app_onboarding'`)
} else {
  console.log('OK   coexistence featureType === whatsapp_business_app_onboarding')
}

if (coexistenceOpts.extras.sessionInfoVersion !== '3') {
  failed++
  console.error(`FAIL coexistence sessionInfoVersion — got '${coexistenceOpts.extras.sessionInfoVersion}', want '3'`)
} else {
  console.log('OK   coexistence sessionInfoVersion === 3')
}

if (coexistenceOpts.response_type !== 'code') {
  failed++
  console.error(`FAIL coexistence response_type — got '${coexistenceOpts.response_type}', want 'code'`)
} else {
  console.log('OK   coexistence response_type === code')
}

if ('feature' in coexistenceOpts.extras) {
  failed++
  console.error('FAIL coexistence extras must not include legacy extras.feature')
} else {
  console.log('OK   coexistence extras omit extras.feature')
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
  console.error('FAIL WhatsAppConnect.tsx must use buildEmbeddedSignupFbLoginOptions (primary path)')
} else {
  console.log('OK   WhatsAppConnect uses default login options helper')
}

if (!connectPage.includes('buildCoexistenceEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must keep buildCoexistenceEmbeddedSignupFbLoginOptions (explicit path)')
} else {
  console.log('OK   WhatsAppConnect keeps coexistence login options helper')
}

if (!connectPage.includes('launchSignup')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx primary Meta CTA must use launchSignup')
} else {
  console.log('OK   primary Meta path uses standard Cloud API helper')
}

if (!connectPage.includes('launchCoexistenceSignup')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must keep launchCoexistenceSignup as an explicit choice')
} else {
  console.log('OK   explicit Coexistence path remains available')
}

const compactCta = connectPage.split('Compact card CTA')[1] || ''
const compactSignup = compactCta.indexOf('onClick={launchSignup}')
const compactCoex = compactCta.indexOf('onClick={launchCoexistenceSignup}')
if (compactSignup < 0 || compactCoex < 0 || compactSignup > compactCoex) {
  failed++
  console.error('FAIL compact card primary CTA must be launchSignup, with launchCoexistenceSignup secondary')
} else {
  console.log('OK   compact card default CTA is standard Cloud API')
}

const fullCta = connectPage.split('Main CTA — standard Cloud API')[1] || ''
const fullSignup = fullCta.indexOf('onClick={launchSignup}')
const fullCoex = fullCta.indexOf('onClick={launchCoexistenceSignup}')
if (fullSignup < 0 || fullCoex < 0 || fullSignup > fullCoex) {
  failed++
  console.error('FAIL full flow primary CTA must be launchSignup, with launchCoexistenceSignup secondary')
} else {
  console.log('OK   full flow default CTA is standard Cloud API')
}

if (!connectPage.includes('handleExchange(code, undefined)')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must call handleExchange(code, undefined) for cloud API path')
} else {
  console.log('OK   standard exchange uses auth code only')
}

if (connectPage.includes('access_token = accessToken') || connectPage.includes("payload.access_token")) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must not send access_token from FB.login')
} else {
  console.log('OK   exchange payload omits access_token')
}

const officialCoexistencePayload = parseEmbeddedSignupWindowMessage({
  data: { waba_id: '123', phone_number_id: '456' },
  type: 'WA_EMBEDDED_SIGNUP',
  event: 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING',
  version: 3,
})
if (
  officialCoexistencePayload?.event !== 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING'
  || officialCoexistencePayload.waba_id !== '123'
  || officialCoexistencePayload.phone_number_id !== '456'
) {
  failed++
  console.error('FAIL parser must unwrap official nested data.waba_id / data.phone_number_id')
} else {
  console.log('OK   parser unwraps official nested WA_EMBEDDED_SIGNUP data')
}

if (parseEmbeddedSignupWindowMessage({ event: 'FINISH', waba_id: '1' }) !== null) {
  failed++
  console.error('FAIL parser must ignore payloads without type=WA_EMBEDDED_SIGNUP')
} else {
  console.log('OK   parser ignores non-WA_EMBEDDED_SIGNUP messages')
}

if (!connectPage.includes("connection_mode: 'coexistence'") && !connectPage.includes('connectionMode: \'coexistence\'')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must send connection_mode coexistence on the explicit Coexistence exchange')
} else {
  console.log('OK   coexistence connection_mode present on explicit path')
}

if (!connectPage.includes('confirm-standard-cloud-api')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must call confirm-standard-cloud-api after coexistence_not_eligible')
} else {
  console.log('OK   standard Cloud API confirm path is present')
}

const safetyNoteIdx = connectPage.indexOf('simp.coexistenceSafetyNote')
const syncingIdx = connectPage.indexOf("stage === 'syncing-phone'")
const syncingEnd = connectPage.indexOf('Compact card CTA', syncingIdx)
if (safetyNoteIdx < 0) {
  failed++
  console.error('FAIL coexistence safety copy must remain for the explicit Coexistence choice')
} else if (syncingIdx >= 0 && safetyNoteIdx > syncingIdx && (syncingEnd < 0 || safetyNoteIdx < syncingEnd)) {
  failed++
  console.error('FAIL Business App safety copy must not render inside the generic Cloud API syncing stage')
} else {
  console.log('OK   Business App safety copy is not bound to generic Cloud API syncing')
}

if ('redirect_uri' in opts || 'redirect_uri' in coexistenceOpts) {
  failed++
  console.error('FAIL FB.login options must not set redirect_uri (SDK popup owns the dialog)')
} else {
  console.log('OK   FB.login options omit redirect_uri')
}

if (connectPage.includes('payload.redirect_uri') || /redirect_uri:\s*(window\.|location\.|document\.)/.test(connectPage)) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must not send browser-derived redirect_uri on exchange')
} else {
  console.log('OK   exchange payload omits client redirect_uri')
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll meta embedded signup login checks passed.')
