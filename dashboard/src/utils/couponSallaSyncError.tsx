/**
 * Coupon ↔ Salla sync error display helpers.
 *
 * TODO before Salla sign-off / merchant release:
 * Hide technical error details from merchant-facing UI and keep only the Arabic
 * friendly summary. Set SHOW_COUPON_SYNC_TECH_DETAILS to false.
 */
import type { ReactNode } from 'react'

/** Temporary: show short technical hints during Salla integration testing. */
export const SHOW_COUPON_SYNC_TECH_DETAILS = true

export const COUPON_SALLA_SYNC_TIMEOUT_FRIENDLY =
  'استغرقت مزامنة كوبونات سلة وقتًا أطول من المتوقع. قد تكتمل العملية بعد لحظات، حدّث الصفحة أو أعد المحاولة.'

export const COUPON_SALLA_SYNC_TIMEOUT_TECHNICAL = 'request timeout after 25s'

const FIELD_LABEL_AR: Record<string, string> = {
  start_date: 'تاريخ بداية الكوبون غير مقبول من سلة',
  expiry_date: 'تاريخ انتهاء الكوبون غير مقبول من سلة',
  expire_date: 'تاريخ انتهاء الكوبون غير مقبول من سلة',
  type: 'نوع الخصم غير مقبول من سلة',
  amount: 'قيمة الخصم غير مقبولة من سلة',
  code: 'كود الكوبون غير مقبول من سلة',
  usage_limit: 'حد الاستخدام غير مقبول من سلة',
  minimum_amount: 'الحد الأدنى للطلب غير مقبول من سلة',
}

export interface CouponSallaSyncErrorDisplay {
  friendly: string
  technical?: string
  fullText: string
}

function _collectInvalidFields(value: unknown, out: Set<string>): void {
  if (value == null) return
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === 'string' && item.trim()) out.add(item.trim())
      else if (item && typeof item === 'object' && 'field' in item) {
        const field = String((item as { field?: unknown }).field || '').trim()
        if (field) out.add(field)
      }
    }
    return
  }
  if (typeof value === 'object') {
    for (const v of Object.values(value as Record<string, unknown>)) {
      _collectInvalidFields(v, out)
    }
  }
}

function _extractInvalidFields(raw: string): string[] {
  const fields = new Set<string>()

  const jsonStart = raw.indexOf('{')
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart)) as Record<string, unknown>
      _collectInvalidFields(parsed.error, fields)
      _collectInvalidFields(parsed.fields, fields)
      if (parsed.error && typeof parsed.error === 'object') {
        const err = parsed.error as Record<string, unknown>
        _collectInvalidFields(err.fields, fields)
        _collectInvalidFields(err.invalid_fields, fields)
        if (err.alert && typeof err.alert === 'object') {
          _collectInvalidFields((err.alert as Record<string, unknown>).invalid_fields, fields)
        }
      }
      if (parsed.alert && typeof parsed.alert === 'object') {
        _collectInvalidFields((parsed.alert as Record<string, unknown>).invalid_fields, fields)
      }
    } catch {
      /* not JSON */
    }
  }

  const bracketMatch = raw.match(/invalid_fields[^[]*\[([^\]]+)\]/i)
  if (bracketMatch) {
    for (const part of bracketMatch[1].split(',')) {
      const cleaned = part.replace(/["'\s]/g, '')
      if (cleaned) fields.add(cleaned)
    }
  }

  const fieldsMatch = raw.match(/fields:\s*\[([^\]]+)\]/i)
  if (fieldsMatch) {
    for (const part of fieldsMatch[1].split(',')) {
      const cleaned = part.replace(/["'\s]/g, '')
      if (cleaned) fields.add(cleaned)
    }
  }

  return [...fields]
}

function _extractHttpStatus(raw: string): number | null {
  const m = raw.match(/(?:Salla )?HTTP\s+(\d{3})/i)
  return m ? Number(m[1]) : null
}

function _buildTechnicalSummary(raw: string): string | undefined {
  const status = _extractHttpStatus(raw)
  const fields = _extractInvalidFields(raw)

  if (status && fields.length) {
    return `Salla HTTP ${status} — invalid_fields: ${fields.join(', ')}`
  }
  if (status) {
    return `Salla HTTP ${status}`
  }
  if (fields.length) {
    return `invalid_fields: ${fields.join(', ')}`
  }

  const compact = raw
    .replace(/\s+/g, ' ')
    .replace(/^\{.*\}$/s, '')
    .trim()
  if (!compact || compact.length > 160) {
    if (compact.length > 160) return `${compact.slice(0, 157)}…`
    return undefined
  }
  return compact
}

function _friendlyFromFields(fields: string[]): string | null {
  for (const field of fields) {
    const label = FIELD_LABEL_AR[field]
    if (label) return `تعذر إرسال الكوبون إلى سلة: ${label}.`
  }
  return null
}

function _looksArabic(text: string): boolean {
  return /[\u0600-\u06FF]/.test(text)
}

export function isCouponSallaSyncTimeout(raw: string | null | undefined): boolean {
  const trimmed = String(raw || '').trim()
  if (!trimmed) return false
  return /انتهت مهلة الطلب|request timeout|signal timed out|aborted|timed out/i.test(trimmed)
}

export function formatCouponSallaSyncError(
  raw: string | null | undefined,
): CouponSallaSyncErrorDisplay {
  const trimmed = String(raw || '').trim()
  if (!trimmed) {
    return {
      friendly: 'تعذر إرسال الكوبون إلى سلة. تحقق من ربط API الكامل أو حاول مرة أخرى.',
      fullText: 'تعذر إرسال الكوبون إلى سلة. تحقق من ربط API الكامل أو حاول مرة أخرى.',
    }
  }

  if (isCouponSallaSyncTimeout(trimmed)) {
    const friendly = COUPON_SALLA_SYNC_TIMEOUT_FRIENDLY
    const technical = SHOW_COUPON_SYNC_TECH_DETAILS
      ? COUPON_SALLA_SYNC_TIMEOUT_TECHNICAL
      : undefined
    if (technical) {
      return {
        friendly,
        technical,
        fullText: `${friendly}\n\nتفاصيل تقنية للاختبار:\n${technical}`,
      }
    }
    return { friendly, fullText: friendly }
  }

  const fields = _extractInvalidFields(trimmed)
  const technical = _buildTechnicalSummary(trimmed)

  let friendly =
    _friendlyFromFields(fields)
    ?? (_looksArabic(trimmed) && !trimmed.includes('{')
      ? trimmed.split('\n')[0].trim()
      : null)

  if (!friendly) {
    const status = _extractHttpStatus(trimmed)
    if (status === 422) {
      friendly = 'تعذر إرسال الكوبون إلى سلة. تحقق من بيانات الكوبون (التواريخ أو الخصم) وحاول مرة أخرى.'
    } else if (trimmed.toLowerCase().includes('full api') || trimmed.includes('ربط API الكامل')) {
      friendly = trimmed.split('\n')[0].trim()
    } else {
      friendly = 'تعذر إرسال الكوبون إلى سلة. تحقق من ربط API الكامل أو حاول مرة أخرى.'
    }
  }

  if (SHOW_COUPON_SYNC_TECH_DETAILS && technical) {
    const fullText = `${friendly}\n\nتفاصيل تقنية للاختبار:\n${technical}`
    return { friendly, technical, fullText }
  }

  return { friendly, fullText: friendly }
}

export function formatCouponSallaSyncErrorAlert(
  raw: string | null | undefined,
): string {
  return formatCouponSallaSyncError(raw).fullText
}

export function CouponSallaSyncErrorHint({ error }: { error: string }): ReactNode {
  const { friendly, technical, fullText } = formatCouponSallaSyncError(error)
  return (
    <div className="flex flex-col gap-0.5 max-w-[12rem]" title={fullText}>
      <span className="text-[10px] text-red-600 leading-snug">{friendly}</span>
      {SHOW_COUPON_SYNC_TECH_DETAILS && technical ? (
        <span className="text-[9px] text-slate-500 font-mono leading-snug break-all">
          {technical}
        </span>
      ) : null}
    </div>
  )
}
