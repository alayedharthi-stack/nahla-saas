export interface PaymentEvidenceHints {
  payment_evidence_status?: string | null
  bank_name?: string | null
  bank_transfer_type?: string | null
  amount?: string | null
  amount_parse_confidence?: string | null
  transfer_date?: string | null
  reference_number?: string | null
  sender_name?: string | null
  from_account_masked?: string | null
  beneficiary_name?: string | null
  to_account?: string | null
  vat_percentage?: string | null
  vat_amount?: string | null
  fee_amount?: string | null
  total_charge_amount?: string | null
  iban_masked?: string | null
}

export const DOCUMENT_CARD_FALLBACK_AR = 'تم استلام ملف PDF ويمكن فتحه أو تحميله.'
export const IMAGE_CARD_FALLBACK_AR = 'تم استلام الصورة ويمكن فتحها.'

const HINT_LABELS: Record<string, string> = {
  amount: 'المبلغ المحوّل',
  bank_transfer_type: 'البنك/نوع التحويل',
  bank_name: 'البنك',
  beneficiary_name: 'المستفيد',
  from_account_masked: 'من حساب',
  sender_name: 'من حساب',
  to_account: 'إلى حساب',
  fee_amount: 'رسوم التحويل',
  vat_amount: 'ضريبة الرسوم',
  vat_percentage: 'نسبة الضريبة',
  total_charge_amount: 'إجمالي الرسوم',
  transfer_date: 'التاريخ',
  reference_number: 'الرقم المرجعي',
  iban_masked: 'آيبان',
}

const HINT_ORDER: Array<keyof PaymentEvidenceHints> = [
  'amount',
  'bank_transfer_type',
  'bank_name',
  'beneficiary_name',
  'from_account_masked',
  'sender_name',
  'to_account',
  'fee_amount',
  'vat_amount',
  'vat_percentage',
  'total_charge_amount',
  'transfer_date',
  'reference_number',
  'iban_masked',
]

function formatHintValue(key: keyof PaymentEvidenceHints, value: string): string {
  const trimmed = value.trim()
  if (key === 'amount' || key === 'fee_amount' || key === 'vat_amount' || key === 'total_charge_amount') {
    if (/^\d+(?:\.\d+)?$/.test(trimmed) && !trimmed.includes('ريال')) {
      return `${trimmed} ريال`
    }
  }
  return trimmed
}

export function paymentHintsConfidenceWarning(
  hints: PaymentEvidenceHints | null | undefined,
): string | null {
  const conf = (hints?.amount_parse_confidence || '').toLowerCase()
  if (conf === 'low' || conf === 'absent') {
    return 'تنبيه: استخراج المبلغ غير مؤكد — راجع الإيصال يدويًا.'
  }
  return null
}

export function paymentHintLines(
  hints: PaymentEvidenceHints | null | undefined,
): Array<{ label: string; value: string }> {
  if (!hints) return []
  const lines: Array<{ label: string; value: string }> = []
  const seenLabels = new Set<string>()
  for (const key of HINT_ORDER) {
    const raw = hints[key]
    if (!raw || !String(raw).trim()) continue
    const label = HINT_LABELS[key]
    if (!label || seenLabels.has(label)) continue
    seenLabels.add(label)
    lines.push({
      label,
      value: formatHintValue(key, String(raw)),
    })
  }
  return lines
}

export function isPaymentMediaKind(kind: string | null | undefined): boolean {
  return !!kind && kind.startsWith('payment_')
}
