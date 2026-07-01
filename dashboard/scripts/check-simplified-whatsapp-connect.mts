/**
 * Guard: merchant WhatsApp Connect page shows only simplified paths.
 *
 * Run: npm run check:simplified-whatsapp-connect   (from dashboard/)
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dir = dirname(fileURLToPath(import.meta.url))
const dashboardSrc = join(__dir, '../src')
const connectPage = readFileSync(
  join(__dir, '../src/pages/WhatsAppConnect.tsx'),
  'utf8',
)
const manualSetupPage = readFileSync(
  join(__dir, '../src/pages/WhatsAppManualSetup.tsx'),
  'utf8',
)
const arLocale = readFileSync(join(__dir, '../src/i18n/ar.ts'), 'utf8')
const enLocale = readFileSync(join(__dir, '../src/i18n/en.ts'), 'utf8')

let failed = 0

const required = [
  'MetaEmbeddedOptionCard',
  'AssistedConnectFlow',
  'ManualSetupGuideButton',
  'requestAssistedConnect',
  'metaApprovalNotice',
  'metaConnectBtn',
  'launchSignup',
  'buildEmbeddedSignupFbLoginOptions',
  'embeddedInCard',
  'a.submitBtn',
  '/help/whatsapp-manual-setup',
  'manualSetupLink',
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
  "const [contactPhone, setContactPhone] = useState(status?.phone_number",
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

const arGuideMeta = 'تحتاج مساعدة؟ افتح دليل الربط اليدوي'
const arGuideAssisted = 'اقرأ دليل الربط اليدوي'
const enGuideMeta = 'Need help? Open the manual setup guide'
const enGuideAssisted = 'Read the manual setup guide'

for (const [label, text, locale] of [
  ['ar meta guide', arGuideMeta, arLocale],
  ['ar assisted guide', arGuideAssisted, arLocale],
  ['en meta guide', enGuideMeta, enLocale],
  ['en assisted guide', enGuideAssisted, enLocale],
] as const) {
  if (!locale.includes(text)) {
    failed++
    console.error(`FAIL missing ${label} copy`)
  } else {
    console.log(`OK   ${label} copy`)
  }
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

const forbiddenPhones = ['0549815590', '549815590']

function walkTsFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      walkTsFiles(full, out)
    } else if (/\.(tsx?|mts)$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

let forbiddenPhoneFound = false
for (const file of walkTsFiles(dashboardSrc)) {
  const content = readFileSync(file, 'utf8')
  for (const phone of forbiddenPhones) {
    if (content.includes(phone)) {
      forbiddenPhoneFound = true
      failed++
      console.error(`FAIL forbidden phone ${phone} in ${file.replace(dashboardSrc, 'src')}`)
    }
  }
}
if (!forbiddenPhoneFound) {
  console.log('OK   no forbidden default phone numbers in dashboard/src')
}

const manualSetupRequired = [
  'ماذا تحتاج قبل البدء؟',
  'ماذا يحدث بعد طلب المساعدة؟',
  'الكتالوج خطوة لاحقة',
  '/whatsapp-connect',
  'PATH_EXISTING_ACCOUNT',
  'PATH_NEW_ACCOUNT',
  'GUIDE_STEPS',
]

for (const needle of manualSetupRequired) {
  if (!manualSetupPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppManualSetup.tsx missing required marker: ${needle}`)
  } else {
    console.log(`OK   manual setup contains ${needle}`)
  }
}

const manualSetupForbiddenSections = [
  'بيانات قد يطلبها فريق نحلة',
  'قائمة الصور المطلوبة',
  'NAHLA_NEEDS',
  'HELP_MANUAL_SETUP_IMAGES.map',
]

for (const needle of manualSetupForbiddenSections) {
  if (manualSetupPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppManualSetup.tsx exposes internal section: ${needle}`)
  } else {
    console.log(`OK   manual setup hides ${needle}`)
  }
}

const manualSetupForbiddenTerms = [
  'Permanent System User Access Token',
  'Phone Number ID',
  'WhatsApp Business Account ID',
  'Business ID',
  'Meta Catalog ID',
  'Access Token',
  'WABA',
]

/** Strip asset filename lines — internal dev paths only, not merchant UI copy. */
const manualSetupPublicSource = manualSetupPage
  .split('\n')
  .filter(line => !/\.png['"]/.test(line))
  .join('\n')

for (const term of manualSetupForbiddenTerms) {
  if (manualSetupPublicSource.includes(term)) {
    failed++
    console.error(`FAIL WhatsAppManualSetup.tsx exposes internal term: ${term}`)
  } else {
    console.log(`OK   manual setup hides ${term}`)
  }
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}
console.log('\nAll simplified WhatsApp Connect checks passed.')
