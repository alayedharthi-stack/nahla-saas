export interface TermsPatch {
  value?: string
  expires?: string
}

export interface ValidationResult {
  valid: boolean
  error?: string
}

const DECIMAL_RE = /^(\d+\.?\d*|\.\d+)$/

function exceedsHundred(s: string): boolean {
  const dot = s.indexOf('.')
  const intStr = dot === -1 ? s : s.slice(0, dot)
  const fracStr = dot === -1 ? '' : s.slice(dot + 1)
  const intVal = parseInt(intStr || '0', 10)
  if (intVal > 100) return true
  if (intVal === 100 && fracStr.replace(/0+$/, '') !== '') return true
  return false
}

function canonicalDecimal(s: string): string {
  const dot = s.indexOf('.')
  const intStr = dot === -1 ? s : s.slice(0, dot)
  const fracStr = dot === -1 ? '' : s.slice(dot + 1)
  const intNorm = intStr.replace(/^0+/, '') || '0'
  const fracNorm = fracStr.replace(/0+$/, '')
  return fracNorm ? `${intNorm}.${fracNorm}` : intNorm
}

/** Validate coupon value: strict decimal grammar; percentage must be ≤ 100 (exact). */
export function validateValue(raw: string, type: 'percentage' | 'fixed'): ValidationResult {
  if (!DECIMAL_RE.test(raw)) return { valid: false, error: 'القيمة يجب أن تكون رقمًا' }
  if (canonicalDecimal(raw) === '0') return { valid: false, error: 'القيمة يجب أن تكون أكبر من صفر' }
  if (type === 'percentage' && exceedsHundred(raw)) return { valid: false, error: 'نسبة الخصم لا يمكن أن تتجاوز 100%' }
  return { valid: true }
}

/**
 * Strict YYYY-MM-DD validator — rejects JS date rollover (e.g. 2023-02-30).
 * Empty string passes (means "no expiry").
 */
export function validateDate(dateStr: string): ValidationResult {
  if (!dateStr) return { valid: true }
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr)
  if (!match) return { valid: false, error: 'صيغة التاريخ غير صحيحة (YYYY-MM-DD مطلوب)' }
  const y = parseInt(match[1], 10)
  const m = parseInt(match[2], 10)
  const d = parseInt(match[3], 10)
  const dt = new Date(Date.UTC(y, m - 1, d))
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() + 1 !== m || dt.getUTCDate() !== d) {
    return { valid: false, error: 'التاريخ غير صحيح' }
  }
  return { valid: true }
}

/**
 * Convert ISO datetime (or plain YYYY-MM-DD) to YYYY-MM-DD in Asia/Riyadh timezone.
 * Plain dates are validated and returned as-is; invalid plain dates return ''.
 * Naive ISO datetimes (has T, no Z/offset) are treated as UTC per backend contract.
 * Returns '' for falsy/unparseable input.
 */
export function toRiyadhDate(isoStr: string | null | undefined): string {
  if (!isoStr) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(isoStr)) {
    const y = parseInt(isoStr.slice(0, 4), 10)
    const m = parseInt(isoStr.slice(5, 7), 10)
    const d = parseInt(isoStr.slice(8, 10), 10)
    const dt = new Date(Date.UTC(y, m - 1, d))
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() + 1 !== m || dt.getUTCDate() !== d) return ''
    return isoStr
  }
  try {
    const hasT = isoStr.includes('T')
    const hasTZ = /[Zz]$/.test(isoStr) || /[+-]\d{2}:\d{2}$/.test(isoStr)
    const src = hasT && !hasTZ ? isoStr + 'Z' : isoStr
    const date = new Date(src)
    if (isNaN(date.getTime())) return ''
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Riyadh',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(date)
    const get = (t: string) => parts.find(p => p.type === t)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return ''
  }
}

/**
 * Build a partial PATCH body containing only changed value/expires.
 * Value comparison is canonical (10.10 == 10.1); patch carries exact input string.
 * Returns:
 *   TermsPatch — one or more changed fields
 *   string     — Arabic error/no-change message
 */
export function buildPatch(
  original: {
    value: number | string
    expires: string
    salla_coupon_id?: string | null
    type: 'percentage' | 'fixed'
  },
  newValueRaw: string,
  newExpiresRaw: string,
): TermsPatch | string {
  const patch: TermsPatch = {}

  const origCanon = canonicalDecimal(String(original.value))
  const newTrimmed = newValueRaw.trim()
  const newCanon = canonicalDecimal(newTrimmed)
  if (newCanon !== origCanon) {
    patch.value = newTrimmed
  }

  const origDate = toRiyadhDate(original.expires)
  const newDate = newExpiresRaw.trim()
  if (newDate !== origDate) {
    if (!newDate && original.salla_coupon_id) {
      return 'لا يمكن إزالة تاريخ انتهاء الصلاحية لكوبون مرتبط بسلة'
    }
    patch.expires = newDate
  }

  if (Object.keys(patch).length === 0) {
    return 'لم يتم إجراء أي تغييرات'
  }

  return patch
}
