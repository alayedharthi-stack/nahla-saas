import { validateValue, validateDate, toRiyadhDate, buildPatch } from './couponTerms'

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(`FAIL: ${message}`)
}
function eq<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected)
    throw new Error(`FAIL: ${message} — expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}

// ── validateValue ──────────────────────────────────────────────────────────
assert(validateValue('10', 'fixed').valid, 'positive fixed passes')
assert(validateValue('49.99', 'fixed').valid, 'decimal fixed passes')
assert(validateValue('0.5', 'percentage').valid, 'decimal percentage passes')
assert(validateValue('99.99', 'percentage').valid, 'positive percentage passes')
assert(validateValue('100', 'percentage').valid, '100% boundary passes')
assert(validateValue('.5', 'fixed').valid, 'leading-dot decimal passes')
assert(!validateValue('100.01', 'percentage').valid, 'over 100% fails')
assert(!validateValue('100.00000000000000001', 'percentage').valid, 'exceeds 100 by epsilon fails')
assert(!validateValue('0', 'fixed').valid, 'zero fails')
assert(!validateValue('-5', 'fixed').valid, 'negative fails')
assert(!validateValue('abc', 'fixed').valid, 'NaN string fails')
assert(!validateValue('', 'fixed').valid, 'empty string fails')
assert(!validateValue('10abc', 'fixed').valid, 'trailing alpha fails')
assert(!validateValue('1,5', 'fixed').valid, 'comma separator fails')
assert(!validateValue('0x10', 'fixed').valid, 'hex prefix fails')
assert(!validateValue('1e2', 'fixed').valid, 'scientific notation fails')

// ── validateDate ───────────────────────────────────────────────────────────
assert(validateDate('').valid, 'empty passes — no expiry')
assert(validateDate('2025-03-15').valid, 'valid date passes')
assert(validateDate('2024-02-29').valid, 'leap year Feb 29 passes')
assert(!validateDate('2023-02-29').valid, 'non-leap Feb 29 fails')
assert(!validateDate('2023-02-30').valid, 'Feb 30 rollover rejected')
assert(!validateDate('2023-13-01').valid, 'month 13 rejected')
assert(!validateDate('2023-00-01').valid, 'month 0 rejected')
assert(!validateDate('2023/03/15').valid, 'slash separator rejected')
assert(!validateDate('20230315').valid, 'no separator rejected')
assert(!validateDate('2023-03-1').valid, 'single-digit day rejected')

// ── toRiyadhDate ───────────────────────────────────────────────────────────
eq(toRiyadhDate('2025-06-15'), '2025-06-15', 'plain date passthrough')
eq(toRiyadhDate(''), '', 'empty returns empty')
eq(toRiyadhDate(null), '', 'null returns empty')
eq(toRiyadhDate(undefined), '', 'undefined returns empty')
// Invalid plain dates
eq(toRiyadhDate('2023-02-30'), '', 'invalid plain date (Feb 30) returns empty')
eq(toRiyadhDate('2023-13-01'), '', 'invalid plain date (month 13) returns empty')
// Explicit Z — UTC midnight is 03:00 Riyadh, same calendar day
eq(toRiyadhDate('2025-06-15T00:00:00Z'), '2025-06-15', 'UTC midnight stays same Riyadh day')
// Explicit Z — UTC 22:00 → Riyadh 01:00 next day
eq(toRiyadhDate('2025-06-14T22:00:00Z'), '2025-06-15', 'UTC 22:00 crosses into next Riyadh day')
// Explicit Z — UTC 20:59 → Riyadh 23:59 same day
eq(toRiyadhDate('2025-06-14T20:59:00Z'), '2025-06-14', 'UTC 20:59 stays same Riyadh day')
// Naive datetime (no Z) must be treated as UTC — same result as explicit Z
eq(toRiyadhDate('2025-06-14T22:00:00'), '2025-06-15', 'naive datetime treated as UTC (zone-crossing)')
eq(toRiyadhDate('2025-06-15T00:00:00'), '2025-06-15', 'naive UTC midnight same Riyadh day')

// ── buildPatch ─────────────────────────────────────────────────────────────
const base = { value: 10, expires: '2025-12-31', salla_coupon_id: null, type: 'fixed' as const }

// No change
const noChange = buildPatch(base, '10', '2025-12-31')
assert(typeof noChange === 'string', 'no-change returns message string')

// Value only
const valOnly = buildPatch(base, '15', '2025-12-31')
assert(typeof valOnly === 'object', 'changed value returns patch object')
if (typeof valOnly === 'object') {
  eq(valOnly.value, '15', 'patch has new value as string')
  assert(!('expires' in valOnly), 'unchanged expires not in patch')
}

// Expires only
const expOnly = buildPatch(base, '10', '2026-01-01')
assert(typeof expOnly === 'object', 'changed expires returns patch object')
if (typeof expOnly === 'object') {
  eq(expOnly.expires, '2026-01-01', 'patch has new expires')
  assert(!('value' in expOnly), 'unchanged value not in patch')
}

// Both changed
const both = buildPatch(base, '20', '2026-03-01')
assert(typeof both === 'object', 'both changed returns patch')
if (typeof both === 'object') {
  eq(both.value, '20', 'both: value correct as string')
  eq(both.expires, '2026-03-01', 'both: expires correct')
}

// Clear expiry rejected for Salla-bound coupon
const bound = { ...base, salla_coupon_id: 'salla-123' }
const clearBound = buildPatch(bound, '10', '')
assert(typeof clearBound === 'string', 'clearing bound salla expiry returns error string')

// Clear expiry allowed for non-bound
const clearFree = buildPatch(base, '10', '')
assert(typeof clearFree === 'object', 'clearing non-bound expiry returns patch')
if (typeof clearFree === 'object') {
  eq(clearFree.expires, '', 'patch carries empty expires')
}

// Decimal value preserved as string
const decPatch = buildPatch(base, '10.5', '2025-12-31')
assert(typeof decPatch === 'object', 'decimal change returns patch')
if (typeof decPatch === 'object') {
  eq(decPatch.value, '10.5', 'decimal value string not truncated')
}

// Canonical semantic equality: 10.10 == 10.1 → no change
const semEq = buildPatch({ value: '10.10', expires: '', salla_coupon_id: null, type: 'fixed' }, '10.1', '')
assert(typeof semEq === 'string', '10.10 and 10.1 semantically equal = no change')

// High-precision string preserved exactly in patch
const hiPrec = buildPatch(base, '10.123456789', '2025-12-31')
assert(typeof hiPrec === 'object', 'high-precision value creates patch')
if (typeof hiPrec === 'object') {
  eq(hiPrec.value, '10.123456789', 'high-precision string preserved exactly')
}

// 1.00000000000000001 differs from 1 (canonical strings differ)
const hiPrecDiff = buildPatch({ value: '1', expires: '', salla_coupon_id: null, type: 'fixed' }, '1.00000000000000001', '')
assert(typeof hiPrecDiff === 'object', '1.00000000000000001 differs from 1 → patch')
if (typeof hiPrecDiff === 'object') {
  eq(hiPrecDiff.value, '1.00000000000000001', 'high-precision diff string preserved')
}

// Percentage string original — semantically equal
const pctBase = { value: '25.00', expires: '', salla_coupon_id: null, type: 'percentage' as const }
const pctSame = buildPatch(pctBase, '25', '')
assert(typeof pctSame === 'string', 'semantically equal string/number value = no change')

console.log('All couponTerms tests passed')
