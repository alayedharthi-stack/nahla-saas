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
  console.error('FAIL WhatsAppConnect.tsx must use buildEmbeddedSignupFbLoginOptions (API path)')
} else {
  console.log('OK   WhatsAppConnect uses default login options helper')
}

if (!connectPage.includes('buildCoexistenceEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx must use buildCoexistenceEmbeddedSignupFbLoginOptions (Business App path)')
} else {
  console.log('OK   WhatsAppConnect uses coexistence login options helper')
}

if (!connectPage.includes('openOnboardingModeChoice')) {
  failed++
  console.error('FAIL WhatsAppConnect.tsx Meta entry CTA must open a mode choice, not FB.login')
} else {
  console.log('OK   Meta entry requires an explicit onboarding mode choice')
}

if (connectPage.includes('onClick={launchSignup}') || connectPage.includes('onClick={launchCoexistenceSignup}')) {
  failed++
  console.error('FAIL Meta path buttons must not call launchSignup/launchCoexistenceSignup until after mode choice')
} else {
  console.log('OK   FB.login launchers are not bound to the first Meta CTA')
}

if (!connectPage.includes('onChooseCoexistence={launchCoexistenceSignup}')) {
  failed++
  console.error('FAIL Choice 1 must call only launchCoexistenceSignup')
} else {
  console.log('OK   Business App choice calls launchCoexistenceSignup')
}

if (!connectPage.includes('onChooseCloudApi={launchSignup}')) {
  failed++
  console.error('FAIL Choice 2 must call only launchSignup')
} else {
  console.log('OK   WhatsApp API choice calls launchSignup')
}

function extractCallback(source: string, name: string): string {
  const start = source.indexOf(`const ${name} = useCallback`)
  if (start < 0) return ''
  const rest = source.slice(start)
  const end = rest.search(/\n  \}, \[/)
  return end > 0 ? rest.slice(0, end) : rest.slice(0, 1200)
}

const coexistenceLauncher = extractCallback(connectPage, 'launchCoexistenceSignup')
const cloudApiLauncher = extractCallback(connectPage, 'launchSignup')
const entryOpener = extractCallback(connectPage, 'openOnboardingModeChoice')

if (!coexistenceLauncher.includes('buildCoexistenceEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL launchCoexistenceSignup must use buildCoexistenceEmbeddedSignupFbLoginOptions')
} else if (coexistenceLauncher.includes('buildEmbeddedSignupFbLoginOptions(')) {
  failed++
  console.error('FAIL launchCoexistenceSignup must not use the standard Cloud API builder')
} else {
  console.log('OK   coexistence launcher is bound only to the Business App builder')
}

if (!cloudApiLauncher.includes('buildEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL launchSignup must use buildEmbeddedSignupFbLoginOptions')
} else if (cloudApiLauncher.includes('buildCoexistenceEmbeddedSignupFbLoginOptions')) {
  failed++
  console.error('FAIL launchSignup must not use the Coexistence builder')
} else {
  console.log('OK   Cloud API launcher is bound only to the standard builder')
}

if (
  !entryOpener
  || entryOpener.includes('FB.login')
  || entryOpener.includes('launchSignup')
  || entryOpener.includes('launchCoexistenceSignup')
) {
  failed++
  console.error('FAIL openOnboardingModeChoice must not call FB.login or either launcher')
} else {
  console.log('OK   Meta entry opener does not start Embedded Signup')
}

const compactCta = connectPage.split('Compact card CTA')[1] || ''
if (!compactCta.includes('onClick={openOnboardingModeChoice}')) {
  failed++
  console.error('FAIL compact card first Meta CTA must open the mode choice')
} else {
  console.log('OK   compact card first Meta CTA requires a mode choice')
}

const choice1Idx = connectPage.indexOf('Choice 1 — Coexistence (WhatsApp Business App)')
const choice2Idx = connectPage.indexOf('Choice 2 — Standard Cloud API')
if (choice1Idx < 0 || choice2Idx < 0 || choice1Idx > choice2Idx) {
  failed++
  console.error('FAIL Coexistence Business App choice must be visually first / primary')
} else {
  console.log('OK   Coexistence is the primary recommended choice')
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

if (!connectPage.includes('no silent Cloud API conversion')) {
  failed++
  console.error('FAIL coexistence-not-eligible UI must require explicit merchant confirmation')
} else {
  console.log('OK   Coexistence ineligibility does not auto-convert to Cloud API')
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
