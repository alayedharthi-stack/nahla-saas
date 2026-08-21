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
  'MetaOnboardingModeChoice',
  'openOnboardingModeChoice',
  'requestAssistedConnect',
  'metaConnectBtn',
  'launchSignup',
  'launchCoexistenceSignup',
  'buildEmbeddedSignupFbLoginOptions',
  'buildCoexistenceEmbeddedSignupFbLoginOptions',
  'embeddedInCard',
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
  'ManualSetupGuideButton',
]

for (const needle of forbidden) {
  if (connectPage.includes(needle)) {
    failed++
    console.error(`FAIL WhatsAppConnect.tsx still exposes merchant path: ${needle}`)
  } else {
    console.log(`OK   hidden ${needle}`)
  }
}

if (!connectPage.includes('onClick={openOnboardingModeChoice}')) {
  failed++
  console.error('FAIL Meta connect button must open the mode choice without calling FB.login')
} else {
  console.log('OK   Meta button opens onboarding mode choice')
}

if (connectPage.includes('onClick={launchSignup}') || connectPage.includes('onClick={launchCoexistenceSignup}')) {
  failed++
  console.error('FAIL first Meta CTA must not call launchSignup or launchCoexistenceSignup')
} else {
  console.log('OK   Meta entry does not silently start Embedded Signup')
}

if (connectPage.includes('<AssistedConnectFlow')) {
  failed++
  console.error('FAIL merchant first screen must not mount AssistedConnectFlow')
} else {
  console.log('OK   AssistedConnectFlow is not mounted on the merchant page')
}

if (connectPage.includes('chooseMethodTitle')) {
  failed++
  console.error('FAIL merchant first screen must not render chooseMethodTitle')
} else {
  console.log('OK   chooseMethodTitle is not rendered')
}

const metaCardStart = connectPage.indexOf('function MetaEmbeddedOptionCard')
const metaCardEnd = connectPage.indexOf('function AssistedConnectFlow', metaCardStart)
const metaCard = metaCardStart >= 0 && metaCardEnd > metaCardStart
  ? connectPage.slice(metaCardStart, metaCardEnd)
  : ''
const firstScreenForbidden = [
  'metaSteps',
  'metaCardTitle',
  'metaApprovalNotice',
  'metaExistingAccountHint',
  'manualSetup',
  'coexistenceConnectBtn',
  'metaCloudApiConnectBtn',
]
for (const needle of firstScreenForbidden) {
  if (metaCard.includes(needle)) {
    failed++
    console.error(`FAIL MetaEmbeddedOptionCard first screen still renders ${needle}`)
  } else {
    console.log(`OK   first screen hides ${needle}`)
  }
}
if (!metaCard.includes('<EmbeddedSignupFlow embeddedInCard')) {
  failed++
  console.error('FAIL MetaEmbeddedOptionCard must render only EmbeddedSignupFlow')
} else {
  console.log('OK   first screen is a single EmbeddedSignupFlow CTA')
}

const channelsHub = readFileSync(join(__dir, '../src/pages/ChannelsHub.tsx'), 'utf8')
if (channelsHub.includes('/help/whatsapp-manual-setup')) {
  failed++
  console.error('FAIL ChannelsHub must not show Manual Setup as a merchant connection card')
} else {
  console.log('OK   ChannelsHub hides Manual Setup card')
}

const sidebar = readFileSync(join(__dir, '../src/components/layout/Sidebar.tsx'), 'utf8')
const merchantNav = sidebar.slice(sidebar.indexOf('const MERCHANT_NAV_GROUPS'))
if (merchantNav.includes('/help/whatsapp-manual-setup')) {
  failed++
  console.error('FAIL merchant sidebar must not include Manual Setup navigation')
} else {
  console.log('OK   merchant sidebar hides Manual Setup')
}

const indexHtml = readFileSync(join(__dir, '../index.html'), 'utf8')
const swSource = readFileSync(join(__dir, '../public/sw.js'), 'utf8')
if (!indexHtml.includes("register('/sw.js?v=7')")) {
  failed++
  console.error('FAIL dashboard/index.html must register a cache-busted service worker')
} else {
  console.log('OK   service worker registration is cache-busted')
}
if (!swSource.includes("CACHE_NAME = 'nahlah-v7'")) {
  failed++
  console.error('FAIL dashboard/public/sw.js must bump CACHE_NAME to nahlah-v7')
} else {
  console.log('OK   service worker cache name is nahlah-v7')
}

const arChoice = 'ربط تطبيق WhatsApp Business الموجود على جوالي'
const enChoice = 'Connect the WhatsApp Business app already on my phone'
const arApi = 'ربط رقم عبر WhatsApp API'
const enApi = 'Connect a number via WhatsApp API'
const arEntry = 'ربط واتساب عبر Meta'
const enEntry = 'Connect WhatsApp via Meta'

for (const [label, text, locale] of [
  ['ar entry CTA', arEntry, arLocale],
  ['en entry CTA', enEntry, enLocale],
  ['ar Business App choice', arChoice, arLocale],
  ['en Business App choice', enChoice, enLocale],
  ['ar API choice', arApi, arLocale],
  ['en API choice', enApi, enLocale],
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
