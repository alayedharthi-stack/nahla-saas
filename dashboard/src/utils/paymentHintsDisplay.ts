export interface PaymentEvidenceHints {
  payment_evidence_status?: string | null
  bank_name?: string | null
  amount?: string | null
  transfer_date?: string | null
  reference_number?: string | null
  sender_name?: string | null
  iban_masked?: string | null
}

export const DOCUMENT_CARD_FALLBACK_AR = 'تم استلام ملف PDF ويمكن فتحه أو تحميله.'
export const IMAGE_CARD_FALLBACK_AR = 'تم استلام الصورة ويمكن فتحها.'

const HINT_LABELS: Record<string, string> = {
  bank_name: 'البنك',
  amount: 'المبلغ',
  transfer_date: 'التاريخ',
  reference_number: 'رقم العملية',
  sender_name: 'المحول',
  iban_masked: 'آيبان',
}

export function paymentHintLines(
  hints: PaymentEvidenceHints | null | undefined,
): Array<{ label: string; value: string }> {
  if (!hints) return []
  const lines: Array<{ label: string; value: string }> = []
  for (const [key, label] of Object.entries(HINT_LABELS)) {
    const value = hints[key as keyof PaymentEvidenceHints]
    if (value && String(value).trim()) {
      lines.push({ label, value: String(value).trim() })
    }
  }
  return lines
}

export function isPaymentMediaKind(kind: string | null | undefined): boolean {
  return !!kind && kind.startsWith('payment_')
}
