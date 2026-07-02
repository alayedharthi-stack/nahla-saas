/**
 * Store coupon create helpers — manual `/coupons` modal only.
 * Keeps code generation + validation testable and out of the page component.
 */

/** Unambiguous uppercase charset (no O/0/I/1). */
const CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'

const CODE_MAX_LEN = 20
const CODE_PATTERN = /^[A-Z0-9_]+$/

export const COUPON_CODE_VALIDATION_AR =
  'استخدم حروف إنجليزية كبيرة وأرقام فقط، بدون مسافات.'

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** `datetime-local` value from a Date in local timezone. */
export function toDatetimeLocalValue(d: Date): string {
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` +
    `T${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  )
}

/** Default expiry: 7 days ahead at 23:59 local. */
export function defaultExpiryLocalValue(): string {
  const d = new Date()
  d.setDate(d.getDate() + 7)
  d.setHours(23, 59, 0, 0)
  return toDatetimeLocalValue(d)
}

export function splitDatetimeLocal(value: string): { date: string; time: string } {
  if (!value || !value.includes('T')) {
    return { date: '', time: '23:59' }
  }
  const [date, time] = value.split('T')
  return { date, time: (time || '23:59').slice(0, 5) }
}

export function combineDatetimeLocal(date: string, time: string): string {
  if (!date) return ''
  const t = (time || '23:59').slice(0, 5)
  return `${date}T${t}`
}

/** Arabic-friendly readout for the merchant under the picker. */
export function formatExpiryLocalAr(value: string): string {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  try {
    return d.toLocaleString('ar-SA', { dateStyle: 'medium', timeStyle: 'short' })
  } catch {
    return d.toLocaleString()
  }
}

export function normalizeCouponCode(raw: string): string {
  return raw.replace(/\s+/g, '').toUpperCase()
}

export function validateCouponCode(raw: string): string | null {
  const code = normalizeCouponCode(raw)
  if (!code) return 'كود الكوبون مطلوب'
  if (code.length > CODE_MAX_LEN) {
    return `الكود يجب ألا يتجاوز ${CODE_MAX_LEN} حرفاً`
  }
  if (!CODE_PATTERN.test(code)) {
    return COUPON_CODE_VALIDATION_AR
  }
  return null
}

function randomSuffix(length: number): string {
  let out = ''
  for (let i = 0; i < length; i += 1) {
    out += CODE_ALPHABET[Math.floor(Math.random() * CODE_ALPHABET.length)]
  }
  return out
}

/**
 * Short merchant-scoped code: NH{TENANT_ID}{RAND4}
 * e.g. NH33K7P9, NH1A8Q2
 */
export function generateStoreCouponCode(tenantId: number | null): string {
  const tenantPart =
    tenantId != null && Number.isFinite(tenantId) && tenantId > 0
      ? String(Math.trunc(tenantId))
      : ''
  const suffixLen = Math.max(4, CODE_MAX_LEN - 2 - tenantPart.length)
  const suffix = randomSuffix(Math.min(suffixLen, 5))
  return normalizeCouponCode(`NH${tenantPart}${suffix}`).slice(0, CODE_MAX_LEN)
}

/** Test hook — validate generated shape without ambiguous chars. */
export function isGeneratedCodeShape(code: string): boolean {
  const normalized = normalizeCouponCode(code)
  if (!normalized.startsWith('NH')) return false
  if (!CODE_PATTERN.test(normalized)) return false
  if (/[OIL01]/.test(normalized)) return false
  return normalized.length >= 6 && normalized.length <= CODE_MAX_LEN
}
