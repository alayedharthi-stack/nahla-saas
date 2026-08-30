import { apiCall } from './client'
import { checkoutRedirectBases } from '../lib/billingPostPayment'

export interface BillingPlan {
  id:               number
  slug:             string
  name:             string
  name_ar:          string
  description:      string
  price_sar:        number
  launch_price_sar: number
  billing_cycle:    string
  features:         string[]
  limits: {
    conversations_per_month: number  // -1 = unlimited
    automations:             number
    campaigns_per_month:     number
  }
}

export interface BillingPaymentHistoryRow {
  paid_at:    string | null
  plan_name:  string
  amount_sar: number
  status:     string
  gateway:    string
}

export type BillingLifecycleStatus =
  | 'trial_pending_whatsapp'
  | 'trial_active'
  | 'trial_expired'
  | 'gift_active'
  | 'paid_active'
  | 'paid_expired'

export type BillingRenewalMethod = 'direct_checkout' | 'salla_app' | 'manual_contact'
export type BillingChannel = 'direct' | 'salla' | 'moyasar' | 'manual' | 'unknown'

export interface BillingStatus {
  has_subscription:        boolean
  plan:                    BillingPlan | null
  status:                  BillingLifecycleStatus | 'active' | 'none' | 'cancelled' | 'trial' | 'pending_payment' | 'payment_failed' | 'expired'
  lifecycle_status?:       BillingLifecycleStatus
  lifecycle_status_label_ar?: string
  headline_ar?:            string
  plan_name?:              string | null
  days_remaining?:         number
  expired_since_days?:     number
  is_trial:                boolean
  trial_pending_whatsapp?: boolean
  trial_days_remaining:    number
  trial_expired:           boolean
  trial_started_at?:       string | null
  trial_ends_at?:          string | null
  subscription_started_at?: string | null
  subscription_ends_at?:   string | null
  subscription_expired?:   boolean
  status_reason_ar?:       string
  warning_level?:          'none' | '7d' | '3d' | '1d' | 'expired'
  has_paid_subscription_history?: boolean
  last_payment_at?:        string | null
  last_payment_amount?:    number
  payment_provider?:       string
  payment_history?:        BillingPaymentHistoryRow[]
  ai_auto_replies_allowed?: boolean
  partner_testing_override_active?: boolean
  partner_testing_override_headline_ar?: string | null
  partner_testing_override_reason?: string | null
  partner_testing_override_expires_at?: string | null
  partner_testing_override_plan_slug?: string | null
  manual_gift_grant_active?: boolean
  manual_gift_grant_headline_ar?: string | null
  manual_gift_grant_plan_slug?: string | null
  manual_gift_grant_ends_at?: string | null
  manual_gift_grant_permanent?: boolean
  manual_gift_grant_billing_status?: string | null
  manual_replies_allowed?: boolean
  campaigns_automations_allowed?: boolean
  billing_channel?:        BillingChannel
  renewal_method?:         BillingRenewalMethod
  can_renew_directly?:     boolean
  renewal_url?:            string | null
  is_salla_managed?:       boolean
  whatsapp_connected?:     boolean
  conversations_used:      number
  conversations_limit:     number     // -1 = unlimited
  current_period_conversations_used?: number
  current_period_conversations_limit?: number
  today_conversations_count?: number
  remaining_conversations?: number
  lifetime_conversations_used?: number
  period_mode?:            string
  period_started_at?:      string | null
  period_ends_at?:         string | null
  usage_pct?:              number
  conversations_exceeded?: boolean
  launch_discount_active:  boolean
  current_price_sar:       number
  integration_fee_sar:     number
  started_at?:             string
}

export interface CheckoutResult {
  subscription_id:         number
  checkout_url:            string | null
  gateway:                 'moyasar' | 'demo'
  amount_sar:              number
  plan_slug:               string
  demo_mode:               boolean
  already_active?:         boolean
  reused?:                 boolean
  // present only in demo mode
  success?:                boolean
  launch_discount_active?: boolean
  current_price_sar?:      number
}

export interface PaymentResult {
  subscription_id?: number
  status:           string
  activated:        boolean
  plan_slug?:       string | null
  plan_name_ar?:    string
  amount_sar?:      number | null
}

export const billingApi = {
  getPlans: () =>
    apiCall<{ plans: BillingPlan[]; integration_fee_sar: number }>('/billing/plans'),

  getStatus: () =>
    apiCall<BillingStatus>('/billing/status'),

  /** Legacy: direct activation without payment (admin / testing). */
  subscribe: (plan_slug: string) =>
    apiCall<{ success: boolean; subscription_id: number; launch_discount_active: boolean; current_price_sar: number }>(
      '/billing/subscribe',
      { method: 'POST', body: JSON.stringify({ plan_slug }) },
    ),

  /**
   * Create a payment checkout session.
   * - If Moyasar is configured → returns checkout_url for redirect.
   * - If no gateway configured (demo) → activates subscription immediately.
   */
  createCheckout: (plan_slug: string) => {
    const origin = typeof window !== 'undefined' ? window.location.origin : ''
    const { success_url, error_url } = checkoutRedirectBases(origin)
    return apiCall<CheckoutResult>('/billing/checkout', {
      method:  'POST',
      body:    JSON.stringify({ plan_slug, success_url, error_url }),
    })
  },

  /** Poll this after Moyasar redirect to confirm activation. */
  getPaymentResult: (sub_id: number) =>
    apiCall<PaymentResult>(`/billing/payment-result?sub_id=${sub_id}`),
}
