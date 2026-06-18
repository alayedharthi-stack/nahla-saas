/**
 * Route-level checks for plan feature display copy.
 *
 * Run: npm run check:plan-features   (from dashboard/)
 */
import { displayPlanFeature, featureHasEmoji } from '../src/lib/planFeatures.ts'

let failed = 0

function assert(name: string, ok: boolean) {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const samples = [
  '📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا',
  '🛒 استرجاع السلات المتروكة (3 مراحل) + كوبونات تلقائية',
  'الردود الذكية للمبيعات وخدمة العملاء',
]

for (const raw of samples) {
  const clean = displayPlanFeature(raw)
  assert(`display strips emoji from sample: ${raw.slice(0, 12)}…`, !featureHasEmoji(clean))
  assert(`display keeps Arabic text: ${raw.slice(0, 12)}…`, clean.length > 8)
}

if (failed > 0) {
  console.error(`\n${failed} plan feature check(s) failed`)
  process.exit(1)
}
console.log('\nAll plan feature checks passed.')
