/**
 * UI-only classification hints for Nahla Intelligence settings (PR-2D).
 * Does not change what is saved or how prompts consume fields.
 */

export type InstructionCategory =
  | 'style'
  | 'sales_policy'
  | 'escalation'
  | 'operational_fact'
  | 'forbidden'

const OPERATIONAL_PATTERNS = [
  /\d+\s*(?:ريال|ر\.?\s*س)/i,
  /(?:\+?966|0)?5\d{8}/,
  /(?:شحن|توصيل|مخزون|سعر|iban|آيبان)/i,
]

const FORBIDDEN_PATTERNS = [
  /(?:حبيبي|قلبي|أنا\s+إنسان|موظف\s+حقيقي|person\s+real)/i,
  /(?:قل\s+دائما|عبارة\s+ثابتة|fixed\s+phrase)/i,
]

export function classifyInstructionText(text: string): InstructionCategory[] {
  const t = (text || '').trim()
  if (!t) return []
  const cats = new Set<InstructionCategory>()
  if (FORBIDDEN_PATTERNS.some(p => p.test(t))) cats.add('forbidden')
  if (OPERATIONAL_PATTERNS.some(p => p.test(t))) cats.add('operational_fact')
  if (/(?:خصم|كوبون|عرض|بديل|تفاوض|جملة)/i.test(t)) cats.add('sales_policy')
  if (/(?:حوّل|تصعيد|موظف|بشري|شكوى)/i.test(t)) cats.add('escalation')
  if (/(?:نبرة|ود|رسمي|اختصار|إيموجي|لهجة)/i.test(t)) cats.add('style')
  if (cats.size === 0) cats.add('style')
  return [...cats]
}

export const CATEGORY_LABEL_AR: Record<InstructionCategory, string> = {
  style: 'أسلوب',
  sales_policy: 'سياسة بيع',
  escalation: 'تصعيد',
  operational_fact: 'حقيقة تشغيلية',
  forbidden: 'تعليمات ممنوعة',
}

export function CategoryBadges({ text }: { text: string }) {
  const cats = classifyInstructionText(text)
  if (!cats.length) return null
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {cats.map(c => (
        <span
          key={c}
          className={
            c === 'operational_fact'
              ? 'text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-medium'
              : c === 'forbidden'
                ? 'text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-800 font-medium'
                : 'text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-medium'
          }
        >
          {CATEGORY_LABEL_AR[c]}
        </span>
      ))}
    </div>
  )
}

export function OperationalFactWarning({ text }: { text: string }) {
  const cats = classifyInstructionText(text)
  if (!cats.includes('operational_fact')) return null
  return (
    <p className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 mt-1 leading-relaxed">
      هذه المعلومة يُفضّل نقلها إلى{' '}
      <a href="/knowledge-base" className="font-semibold underline">
        قاعدة المعرفة
      </a>{' '}
      أو الكتالوج — الحقائق التشغيلية لا تُدار من شخصية المساعد.
    </p>
  )
}
