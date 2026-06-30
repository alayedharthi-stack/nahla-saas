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

let failed = 0

const required = [
  'MetaEmbeddedOptionCard',
  'AssistedConnectFlow',
  'requestAssistedConnect',
  'metaConnectDisabledBtn',
  'a.submitBtn',
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
  'wc.page.modes.manual',
  'wc.page.modes.otp',
  '<ManualConnectForm',
  '<CoexistenceFlow',
]

for (const needle of forbidden) {
  if (connectPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppConnect.tsx still exposes merchant path: ${needle}`)
  } else {
    console.log(`OK   hidden ${needle}`)
  }
}

if (!connectPage.includes('disabled') || !connectPage.includes('type="button"')) {
  failed++
  console.error('FAIL Meta connect button must remain disabled for merchants when signup is off')
} else {
  console.log('OK   disabled Meta button pattern present')
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll simplified WhatsApp Connect checks passed.')
