/**
 * UI-only bucket mapping for Knowledge Base redesign (PR-2).
 *
 * Maps existing `kind` slugs into eleven merchant-facing sections.
 * Does NOT change backend registry, prompt overlay groups, or API payloads.
 */

export type UiBucketId =
  | 'store_info'
  | 'shipping'
  | 'payment'
  | 'policies'
  | 'sales_rules'
  | 'escalation'
  | 'faq'
  | 'product_notes'
  | 'media'
  | 'assistant_behavior'
  | 'review'

export interface UiBucketDef {
  id: UiBucketId
  order: number
  label_ar: string
  description_ar: string
  /** Registry kind slugs assigned to this bucket (empty = special UI bucket). */
  kinds: readonly string[]
  sensitive?: boolean
}

/** Behavioral kinds — same set as backend BEHAVIORAL_KINDS (minus escalation split for UI). */
export const BEHAVIOR_KINDS_UI = [
  'forbidden_phrases',
  'allowed_style',
  'response_tone',
  'emoji_policy',
  'compliance_rules',
  'owner_identity',
  'assistant_identity',
  'reply_style',
  'dialect',
] as const

export const ESCALATION_KINDS_UI = ['escalation_rules'] as const

export const PAYMENT_KINDS = ['payment_method', 'bank_transfer', 'cod'] as const

export const SHIPPING_KINDS = [
  'shipping_carrier',
  'shipping_zones',
  'cold_shipping',
  'summer_note',
] as const

export const POLICY_KINDS = ['return_policy', 'warranty'] as const

export const PRODUCT_KINDS = [
  'product_usage',
  'product_recipe',
  'product_benefit',
  'product_storage',
  'product_compare',
  'goal_based_recommendation',
] as const

export const STORE_KINDS = [
  'store_story',
  'branches',
  'working_hours',
  'custom',
] as const

export const REVIEW_KINDS = ['quick_update'] as const

export const UI_BUCKETS: readonly UiBucketDef[] = [
  {
    id: 'store_info',
    order: 1,
    label_ar: 'معلومات المتجر',
    description_ar: 'القصة، الفروع، أوقات العمل، الموقع، وروابط مهمة — بدون أسلوب المساعد.',
    kinds: STORE_KINDS,
  },
  {
    id: 'shipping',
    order: 2,
    label_ar: 'الشحن والتوصيل',
    description_ar: 'الشركات، المناطق، المدد، الاستثناءات، وتكاليف الشحن.',
    kinds: SHIPPING_KINDS,
  },
  {
    id: 'payment',
    order: 3,
    label_ar: 'الدفع والتحويل',
    description_ar: 'طرق الدفع، الحسابات، الدفع عند الاستلام، وإثبات التحويل.',
    kinds: PAYMENT_KINDS,
    sensitive: true,
  },
  {
    id: 'policies',
    order: 4,
    label_ar: 'السياسات',
    description_ar: 'الاسترجاع، الاستبدال، الضمان، والشكاوى.',
    kinds: POLICY_KINDS,
  },
  {
    id: 'sales_rules',
    order: 5,
    label_ar: 'قواعد البيع والعروض',
    description_ar: 'متى يُقترح بديل أو عرض — يُكمّل من إعدادات نحلة الذكية.',
    kinds: [],
  },
  {
    id: 'escalation',
    order: 6,
    label_ar: 'التصعيد والتواصل',
    description_ar: 'أرقام التواصل والتصعيد — حقائق تشغيلية حساسة (معاينة قبل الإرسال لاحقاً).',
    kinds: ESCALATION_KINDS_UI,
    sensitive: true,
  },
  {
    id: 'faq',
    order: 7,
    label_ar: 'الأسئلة الشائعة',
    description_ar: 'أسئلة متكررة وأجوبتها الجاهزة.',
    kinds: ['faq'],
  },
  {
    id: 'product_notes',
    order: 8,
    label_ar: 'توضيحات المنتجات',
    description_ar: 'الاستخدام والفروقات والتخزين — لا تضع الأسعار أو التوفر هنا.',
    kinds: PRODUCT_KINDS,
  },
  {
    id: 'media',
    order: 9,
    label_ar: 'الوسائط والمرفقات',
    description_ar: 'صور، فيديو، باركود، وخرائط مرتبطة بالمعرفة.',
    kinds: [],
  },
  {
    id: 'assistant_behavior',
    order: 10,
    label_ar: 'سلوك المساعد',
    description_ar: 'النبرة والعبارات الممنوعة — شخصية مرنة، ليست حقائق تشغيلية.',
    kinds: BEHAVIOR_KINDS_UI,
  },
  {
    id: 'review',
    order: 11,
    label_ar: 'المراجعة والتحذيرات',
    description_ar: 'اقتراحات التحسين، التنظيم، والمسودات التي تحتاج مراجعة.',
    kinds: REVIEW_KINDS,
  },
] as const

const _kindToBucket = new Map<string, UiBucketId>()

for (const bucket of UI_BUCKETS) {
  for (const kind of bucket.kinds) {
    _kindToBucket.set(kind, bucket.id)
  }
}

/** Resolve UI bucket for a section kind; unknown kinds fall back to store_info. */
export function uiBucketForKind(kind: string): UiBucketId {
  return _kindToBucket.get((kind || '').trim().toLowerCase()) ?? 'store_info'
}

/** Prompt overlay group headings (facts block) — mirrors tenant_overlay _GROUP_HEADINGS_AR. */
export const PROMPT_FACT_GROUP_AR: Record<number, string> = {
  1: 'تحديثات سريعة من التاجر',
  2: 'معلومات المتجر',
  3: 'سياسات البيع',
  4: 'سياسات الشحن',
  5: 'معلومات إضافية عن المنتجات',
}

/** Default kind when adding a section inside a UI bucket. */
export function defaultKindForBucket(bucketId: UiBucketId): string {
  const map: Record<UiBucketId, string> = {
    store_info: 'store_story',
    shipping: 'shipping_zones',
    payment: 'payment_method',
    policies: 'return_policy',
    sales_rules: 'custom',
    escalation: 'escalation_rules',
    faq: 'faq',
    product_notes: 'product_usage',
    media: 'payment_method',
    assistant_behavior: 'response_tone',
    review: 'quick_update',
  }
  return map[bucketId]
}
