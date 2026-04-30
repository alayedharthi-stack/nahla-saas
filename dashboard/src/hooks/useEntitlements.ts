/**
 * useEntitlements — Nahla Plan Entitlements Hook
 * ─────────────────────────────────────────────
 * Fetches current plan features + limits from GET /billing/entitlements.
 * Cached in memory for 2 minutes; refetchable on demand.
 *
 * Feature Map (mirrors backend plan_entitlements.py — do NOT guess):
 *   Starter:  basic autopilot + 2-stage cart recovery + abandoned_cart_basic_coupon
 *             + templates + campaigns (monthly cap) + Salla/WA
 *   Growth:   + full autopilot + stage 3 + advanced coupons + meta_catalog_sync
 *             + growth engine + offers + smart_discount_popup + AI analytics
 *   Scale:    + store_brain_advanced + advanced AI + Zid + team + future integrations
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { API_BASE } from '../api/client'

// ── Types ─────────────────────────────────────────────────────────────────────

export type PlanSlug = 'starter' | 'growth' | 'scale' | 'none' | 'failed'
export type BillingStatus = 'active' | 'trial' | 'failed' | 'cancelled' | 'none'

export interface PlanFeatures {
  // Templates
  nahla_template_library:        boolean
  meta_template_sync:            boolean

  // Autopilot — basic (Starter+)
  autopilot_order_confirmation:  boolean
  autopilot_order_notifications: boolean
  autopilot_shipping_tracking:   boolean

  // Autopilot — full (Growth+)
  autopilot_full:                boolean
  autopilot_customer_recovery:   boolean
  autopilot_cod_confirmation:    boolean

  // Cart recovery
  cart_recovery_stage_2:         boolean  // Starter+
  cart_recovery_stage_3:         boolean  // Growth+
  cart_recovery_advanced_coupon: boolean  // Growth+

  // Coupons
  abandoned_cart_basic_coupon:   boolean  // Starter+
  advanced_coupon_types:         boolean  // Growth+: VIP + inactive + levels

  // Campaigns
  campaign_customer_segments:    boolean  // Starter+
  campaign_ai_optimization:      boolean  // Growth+

  // Growth engine (Growth+)
  predictive_reorder:            boolean
  vip_rewards:                   boolean
  back_in_stock_alerts:          boolean
  new_products_alerts:           boolean

  // Offers (Growth+)
  seasonal_smart_offers:         boolean
  salary_offers:                 boolean
  seasonal_calendar:             boolean

  // Conversion tools
  smart_discount_popup:          boolean  // Growth+

  // Integrations
  meta_catalog_sync:             boolean  // Growth+
  zid_integration:               boolean  // Scale+
  future_integrations:           boolean  // Scale+

  // Analytics — Growth dashboard (Growth+)
  ai_performance_dashboard:      boolean
  conversion_funnel:             boolean

  // Analytics — advanced (Scale+)
  advanced_ai_analytics:         boolean
  revenue_breakdown:             boolean
  top_products_analytics:        boolean
  order_sources_analytics:       boolean

  // AI advanced (Scale+)
  store_brain_advanced:          boolean
  full_ai_customization:         boolean
  advanced_discount_rules:       boolean
  escalation_rules:              boolean

  // Team (Scale+)
  team_handoff_queue:            boolean
}

export interface PlanLimits {
  monthly_conversations: number | null  // null = unlimited
  campaigns_per_month:   number | null
}

export interface PlanUsage {
  monthly_conversations: number
  campaigns_per_month:   number
}

export interface EntitlementsData {
  plan:           PlanSlug
  plan_name_ar:   string
  billing_status: BillingStatus
  is_active:      boolean
  is_blocked:     boolean
  features:       PlanFeatures
  limits:         PlanLimits
  usage:          PlanUsage
}

// ── Feature → minimum plan (mirrors _FEATURE_MIN_PLAN in backend) ─────────────

export const FEATURE_REQUIRED_PLAN: Record<keyof PlanFeatures, PlanSlug> = {
  // Starter+
  nahla_template_library:        'starter',
  meta_template_sync:            'starter',
  autopilot_order_confirmation:  'starter',
  autopilot_order_notifications: 'starter',
  autopilot_shipping_tracking:   'starter',
  cart_recovery_stage_2:         'starter',
  abandoned_cart_basic_coupon:   'starter',
  campaign_customer_segments:    'starter',

  // Growth+
  autopilot_full:                'growth',
  autopilot_customer_recovery:   'growth',
  autopilot_cod_confirmation:    'growth',
  cart_recovery_stage_3:         'growth',
  cart_recovery_advanced_coupon: 'growth',
  advanced_coupon_types:         'growth',
  campaign_ai_optimization:      'growth',
  predictive_reorder:            'growth',
  vip_rewards:                   'growth',
  back_in_stock_alerts:          'growth',
  new_products_alerts:           'growth',
  seasonal_smart_offers:         'growth',
  salary_offers:                 'growth',
  seasonal_calendar:             'growth',
  smart_discount_popup:          'growth',
  meta_catalog_sync:             'growth',
  ai_performance_dashboard:      'growth',
  conversion_funnel:             'growth',

  // Scale+
  zid_integration:               'scale',
  future_integrations:           'scale',
  advanced_ai_analytics:         'scale',
  revenue_breakdown:             'scale',
  top_products_analytics:        'scale',
  order_sources_analytics:       'scale',
  store_brain_advanced:          'scale',
  full_ai_customization:         'scale',
  advanced_discount_rules:       'scale',
  escalation_rules:              'scale',
  team_handoff_queue:            'scale',
}

export const PLAN_LABELS_AR: Record<PlanSlug, string> = {
  starter: 'الأساسية',
  growth:  'النمو',
  scale:   'التوسع',
  none:    'بدون اشتراك',
  failed:  'فشل الدفع',
}

export const FEATURE_LABELS_AR: Record<keyof PlanFeatures, string> = {
  // Templates
  nahla_template_library:        'مكتبة قوالب نحلة',
  meta_template_sync:            'مزامنة قوالب Meta',

  // Autopilot basic
  autopilot_order_confirmation:  'تأكيد الطلب التلقائي',
  autopilot_order_notifications: 'إشعارات الطلب التلقائية',
  autopilot_shipping_tracking:   'تتبع الشحن التلقائي',

  // Autopilot full
  autopilot_full:                'الطيار الآلي الكامل',
  autopilot_customer_recovery:   'استرجاع العملاء التلقائي',
  autopilot_cod_confirmation:    'تأكيد الدفع عند الاستلام (COD)',

  // Cart recovery
  cart_recovery_stage_2:         'السلة المتروكة — المرحلة الثانية',
  cart_recovery_stage_3:         'السلة المتروكة — المرحلة الثالثة',
  cart_recovery_advanced_coupon: 'كوبون متقدم في استرجاع السلة',

  // Coupons
  abandoned_cart_basic_coupon:   'كوبون استرجاع السلة الأساسي',
  advanced_coupon_types:         'كوبونات VIP والاسترجاع المتقدمة',

  // Campaigns
  campaign_customer_segments:    'شرائح العملاء في الحملات',
  campaign_ai_optimization:      'تحسين الحملات بالذكاء الاصطناعي',

  // Growth engine
  predictive_reorder:            'إعادة الطلب التنبؤية',
  vip_rewards:                   'مكافآت العملاء VIP',
  back_in_stock_alerts:          'تنبيهات عودة المنتج للمخزن',
  new_products_alerts:           'تنبيهات المنتجات الجديدة',

  // Offers
  seasonal_smart_offers:         'العروض الموسمية الذكية',
  salary_offers:                 'عروض يوم الراتب',
  seasonal_calendar:             'تقويم المناسبات الذكي',

  // Conversion
  smart_discount_popup:          'نافذة الخصم الذكية',

  // Integrations
  meta_catalog_sync:             'مزامنة كاتالوج ميتا (Facebook / Instagram)',
  zid_integration:               'تكامل Zid',
  future_integrations:           'تكاملات مستقبلية (وصول مبكر)',

  // Analytics Growth
  ai_performance_dashboard:      'لوحة أداء الذكاء الاصطناعي',
  conversion_funnel:             'مسار التحويل',

  // Analytics Scale
  advanced_ai_analytics:         'تحليلات الذكاء المتقدمة',
  revenue_breakdown:             'تفصيل الإيرادات',
  top_products_analytics:        'تحليل أفضل المنتجات',
  order_sources_analytics:       'مصادر الطلبات',

  // AI advanced
  store_brain_advanced:          'ذكاء المتجر المتقدم',
  full_ai_customization:         'تخصيص الذكاء الكامل',
  advanced_discount_rules:       'قواعد الخصم المتقدمة',
  escalation_rules:              'قواعد التصعيد',

  // Team
  team_handoff_queue:            'طابور تحويل للفريق',
}

// ── Default fallback (no plan / unauthenticated) ──────────────────────────────

const _FALSE_FEATURES = Object.fromEntries(
  Object.keys(FEATURE_REQUIRED_PLAN).map(k => [k, false])
) as PlanFeatures

const DEFAULT_ENTITLEMENTS: EntitlementsData = {
  plan:           'none',
  plan_name_ar:   'بدون اشتراك',
  billing_status: 'none',
  is_active:      false,
  is_blocked:     false,
  features: _FALSE_FEATURES,
  limits:   { monthly_conversations: 0, campaigns_per_month: 0 },
  usage:    { monthly_conversations: 0, campaigns_per_month: 0 },
}

// ── Cache ─────────────────────────────────────────────────────────────────────

const CACHE_TTL_MS = 2 * 60 * 1000

interface Cache { data: EntitlementsData; fetchedAt: number }
let _cache: Cache | null = null

function isCacheValid(): boolean {
  return !!_cache && Date.now() - _cache.fetchedAt < CACHE_TTL_MS
}

export function invalidateEntitlementsCache(): void {
  _cache = null
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export interface UseEntitlementsResult {
  ent:             EntitlementsData
  loading:         boolean
  error:           string | null
  refetch:         () => void
  hasFeature:      (key: keyof PlanFeatures) => boolean
  getLimit:        (key: keyof PlanLimits)   => number
  isLimitExceeded: (key: keyof PlanLimits)   => boolean
  requiredPlan:    (key: keyof PlanFeatures) => PlanSlug
}

export function useEntitlements(): UseEntitlementsResult {
  const [ent,     setEnt]     = useState<EntitlementsData>(_cache?.data ?? DEFAULT_ENTITLEMENTS)
  const [loading, setLoading] = useState(!isCacheValid())
  const [error,   setError]   = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const fetchEntitlements = useCallback(async () => {
    if (isCacheValid()) {
      setEnt(_cache!.data)
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const token = localStorage.getItem('nahla_token') || ''
      const res = await fetch(`${API_BASE}/billing/entitlements`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal:  AbortSignal.timeout(8000),
      })
      if (!res.ok) {
        if (res.status === 401 || res.status === 403) {
          setEnt(DEFAULT_ENTITLEMENTS)
          setLoading(false)
          return
        }
        throw new Error(`HTTP ${res.status}`)
      }
      const json = await res.json()
      const data: EntitlementsData = {
        plan:           json.plan           ?? 'none',
        plan_name_ar:   json.plan_name_ar   ?? 'بدون اشتراك',
        billing_status: json.billing_status ?? 'none',
        is_active:      json.is_active      ?? false,
        is_blocked:     json.is_blocked     ?? false,
        features:       json.features       ?? _FALSE_FEATURES,
        limits:         json.limits         ?? DEFAULT_ENTITLEMENTS.limits,
        usage:          json.usage          ?? DEFAULT_ENTITLEMENTS.usage,
      }
      _cache = { data, fetchedAt: Date.now() }
      setEnt(data)
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'AbortError') return
      setError('تعذّر تحميل بيانات الاشتراك')
      setEnt(DEFAULT_ENTITLEMENTS)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchEntitlements()
    return () => { abortRef.current?.abort() }
  }, [fetchEntitlements])

  const hasFeature = useCallback(
    (key: keyof PlanFeatures) => !!ent.features?.[key],
    [ent]
  )

  const getLimit = useCallback(
    (key: keyof PlanLimits): number => {
      const v = ent.limits?.[key]
      return v === null || v === undefined ? Infinity : v
    },
    [ent]
  )

  const isLimitExceeded = useCallback(
    (key: keyof PlanLimits): boolean => {
      const limit = getLimit(key)
      if (limit === Infinity) return false
      const used = ent.usage?.[key as keyof PlanUsage] ?? 0
      return used >= limit
    },
    [ent, getLimit]
  )

  const requiredPlan = useCallback(
    (key: keyof PlanFeatures): PlanSlug => FEATURE_REQUIRED_PLAN[key] ?? 'growth',
    []
  )

  return { ent, loading, error, refetch: fetchEntitlements, hasFeature, getLimit, isLimitExceeded, requiredPlan }
}
