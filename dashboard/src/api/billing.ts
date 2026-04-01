async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': '1',
      ...(options?.headers ?? {}),
    },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

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
  status:                  'active' | 'none' | 'cancelled' | 'trial'
  conversations_used:      number
  conversations_limit:     number   // -1 = unlimited
  launch_discount_active:  boolean
  current_price_sar:       number
  integration_fee_sar:     number
  started_at?:             string
}

export const billingApi = {
  getPlans: () =>
    apiCall<{ plans: BillingPlan[]; integration_fee_sar: number }>('/billing/plans'),

  getStatus: () =>
    apiCall<BillingStatus>('/billing/status'),

  subscribe: (plan_slug: string) =>
    apiCall<{ success: boolean; subscription_id: number; launch_discount_active: boolean; current_price_sar: number }>(
      '/billing/subscribe',
      { method: 'POST', body: JSON.stringify({ plan_slug }) },
    ),
}
