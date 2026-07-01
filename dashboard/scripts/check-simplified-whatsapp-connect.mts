/**
 * Guard: merchant WhatsApp Connect page shows only simplified paths.
 *
 * Run: npm run check:simplified-whatsapp-connect   (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dir = dirname(fileURLToPath(import.meta.url))
const connectPage = readFileSync(
  join(__dir, '../src/pages/WhatsAppConnect.tsx'),
  'utf8',
)
const arLocale = readFileSync(join(__dir, '../src/i18n/ar.ts'), 'utf8')
const enLocale = readFileSync(join(__dir, '../src/i18n/en.ts'), 'utf8')

let failed = 0

const required = [
  'MetaEmbeddedOptionCard',
  'AssistedConnectFlow',
  'requestAssistedConnect',
  'metaApprovalNotice',
  'metaConnectBtn',
  'launchSignup',
  'buildEmbeddedSignupFbLoginOptions',
  'embeddedInCard',
  'a.submitBtn',
  '/help/whatsapp-manual-setup',
]

for (const needle of required) {
  if (!connectPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppConnect.tsx missing required marker: ${needle}`)
  } else {
    console.log(`OK   contains ${needle}`)
  }
}

const forbidden = [
  "setMode('manual'",
  "setMode('direct'",
  "setMode('coexistence'",
  "setMode('embedded'",
  'wc.page.modes.manual',
  'wc.page.modes.otp',
  'wc.page.modes.coexistence',
  '<ManualConnectForm',
  '<CoexistenceFlow',
  'metaConnectDisabledBtn',
  'bg-[#1877F2]/40',
]

for (const needle of forbidden) {
  if (connectPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppConnect.tsx still exposes merchant path: ${needle}`)
  } else {
    console.log(`OK   hidden ${needle}`)
  }
}

if (!connectPage.includes('onClick={launchSignup}')) {
  failed++
  console.error('FAIL Meta connect button must call launchSignup (enabled trial path)')
} else {
  console.log('OK   Meta button calls launchSignup')
}

const arNotice =
  'الربط المباشر عبر Meta قد لا يكتمل حالياً حتى تنتهي موافقة Meta الرسمية'
const enNotice =
  'Direct Meta connection may not complete until Meta approval is finalized'

if (!arLocale.includes(arNotice)) {
  failed++
  console.error('FAIL ar.ts missing Meta approval notice copy')
} else {
  console.log('OK   ar.ts Meta approval notice')
}

if (!enLocale.includes(enNotice)) {
  failed++
  console.error('FAIL en.ts missing Meta approval notice copy')
} else {
  console.log('OK   en.ts Meta approval notice')
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll simplified WhatsApp Connect checks passed.')
