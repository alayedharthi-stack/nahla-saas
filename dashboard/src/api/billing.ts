import { apiCall } from './client'

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

export interface BillingStatus {
  has_subscription:        boolean
  plan:                    BillingPlan | null
  status:                  'active' | 'none' | 'cancelled' | 'trial' | 'pending_payment' | 'payment_failed'
  is_trial:                boolean
  trial_days_remaining:    number
  trial_expired:           boolean
  conversations_used:      number
  conversations_limit:     number     // -1 = unlimited
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
    return apiCall<CheckoutResult>('/billing/checkout', {
      method:  'POST',
      body:    JSON.stringify({
        plan_slug,
        success_url: `${origin}/billing/payment-result`,
        error_url:   `${origin}/billing/payment-result`,
      }),
    })
  },

  /** Poll this after Moyasar redirect to confirm activation. */
  getPaymentResult: (sub_id: number) =>
    apiCall<PaymentResult>(`/billing/payment-result?sub_id=${sub_id}`),
}
