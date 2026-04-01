// ── Types ─────────────────────────────────────────────────────────────────────

export interface AiSalesAgentSettings {
  enable_ai_sales_agent:         boolean
  allow_product_recommendations: boolean
  allow_order_creation:          boolean
  allow_address_collection:      boolean
  allow_payment_link_sending:    boolean
  allow_cod_confirmation_flow:   boolean
  allow_human_handoff:           boolean
  confidence_threshold:          number
  handoff_phrases:               string[]
}

export interface AiSalesProcessResult {
  intent:            string
  intent_label:      string
  confidence:        number
  response_text:     string
  products_used:     boolean
  order_started:     boolean
  payment_link:      string | null
  handoff_triggered: boolean
}

export interface AiSalesCreateOrderIn {
  customer_phone: string
  customer_name:  string
  product_id?:    number
  product_name?:  string
  variant_id?:    number
  quantity:       number
  city?:          string
  address?:       string
  payment_method: 'cod' | 'pay_now'
  notes?:         string
}

export interface AiSalesOrderResult {
  order_id:     number
  order_status: string
  payment_link: string | null
  customer_id:  number
  total:        string
  message:      string
}

export interface AiSalesLogEntry {
  id:                number
  customer_phone:    string
  customer_name:     string
  message:           string
  intent:            string
  intent_label:      string
  confidence:        number
  response_text:     string
  product_used:      boolean
  order_created:     boolean
  payment_link_sent: boolean
  handoff_triggered: boolean
  order_id:          number | null
  timestamp:         string | null
}

export interface AiSalesLogsResponse {
  logs:   AiSalesLogEntry[]
  total:  number
  offset: number
  limit:  number
}

// ── API client ────────────────────────────────────────────────────────────────

async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': '1',
      ...(options?.headers ?? {}),
    },
    ...options,
  })
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}

export const aiSalesApi = {
  /** Get AI Sales Agent settings for this tenant. */
  getSettings: () =>
    apiCall<{ settings: AiSalesAgentSettings }>('/ai-sales/settings'),

  /** Save AI Sales Agent settings (partial update). */
  saveSettings: (patch: Partial<AiSalesAgentSettings>) =>
    apiCall<{ settings: AiSalesAgentSettings }>('/ai-sales/settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  /** Process an incoming WhatsApp message through the AI Sales Agent. */
  processMessage: (payload: { customer_phone: string; message: string; customer_name?: string }) =>
    apiCall<AiSalesProcessResult>('/ai-sales/process-message', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** Create an order draft from an AI sales conversation. */
  createOrder: (order: AiSalesCreateOrderIn) =>
    apiCall<AiSalesOrderResult>('/ai-sales/create-order', {
      method: 'POST',
      body: JSON.stringify(order),
    }),

  /** Fetch AI Sales Agent conversation logs. */
  getLogs: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.limit  !== undefined) qs.set('limit',  String(params.limit))
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs}` : ''
    return apiCall<AiSalesLogsResponse>(`/ai-sales/logs${query}`)
  },
}

// ── Display metadata ──────────────────────────────────────────────────────────

export const AI_SALES_INTENT_META: Record<string, { label: string; emoji: string; color: string }> = {
  ask_product:      { label: 'استفسار عن منتج',     emoji: '📦', color: 'blue'   },
  ask_price:        { label: 'استفسار عن السعر',    emoji: '💰', color: 'amber'  },
  ask_recommendation: { label: 'طلب توصية',          emoji: '⭐', color: 'yellow' },
  ask_shipping:     { label: 'استفسار عن الشحن',    emoji: '🚚', color: 'sky'    },
  ask_offer:        { label: 'استفسار عن العروض',   emoji: '🏷️', color: 'pink'   },
  order_product:    { label: 'طلب شراء منتج',       emoji: '🛍️', color: 'emerald'},
  pay_now:          { label: 'الدفع الإلكتروني',    emoji: '💳', color: 'violet' },
  cash_on_delivery: { label: 'الدفع عند الاستلام',  emoji: '💵', color: 'green'  },
  track_order:      { label: 'تتبع الطلب',          emoji: '📍', color: 'orange' },
  talk_to_human:    { label: 'التحدث مع موظف',      emoji: '👤', color: 'slate'  },
  general:          { label: 'عام',                 emoji: '💬', color: 'slate'  },
}

export const AI_SALES_PERMISSION_META: Array<{
  key:   keyof AiSalesAgentSettings
  label: string
  hint:  string
  icon:  string
}> = [
  {
    key:   'allow_product_recommendations',
    label: 'السماح بالتوصيات',
    hint:  'تعرض نهلة منتجات مقترحة بناءً على استفسار العميل',
    icon:  '⭐',
  },
  {
    key:   'allow_order_creation',
    label: 'السماح بإنشاء الطلبات',
    hint:  'تنشئ نهلة مسودة طلب مباشرةً من المحادثة',
    icon:  '🛍️',
  },
  {
    key:   'allow_address_collection',
    label: 'السماح بجمع العنوان',
    hint:  'تطلب نهلة المدينة والعنوان من العميل عند الحاجة',
    icon:  '📍',
  },
  {
    key:   'allow_payment_link_sending',
    label: 'السماح بإرسال روابط الدفع',
    hint:  'ترسل نهلة رابط دفع إلكتروني للعميل عند اختياره',
    icon:  '💳',
  },
  {
    key:   'allow_cod_confirmation_flow',
    label: 'السماح بالدفع عند الاستلام',
    hint:  'تنشئ نهلة طلبات COD وتُشغّل تدفق تأكيد الطيار التلقائي',
    icon:  '💵',
  },
  {
    key:   'allow_human_handoff',
    label: 'السماح بالتحويل لموظف',
    hint:  'تحوّل نهلة المحادثة لموظف بشري عند انخفاض الثقة أو طلب العميل',
    icon:  '👤',
  },
]
