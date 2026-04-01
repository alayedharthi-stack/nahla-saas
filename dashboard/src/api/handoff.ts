export interface HandoffSession {
  id: number
  customer_phone: string
  customer_name: string
  status: 'active' | 'resolved'
  handoff_reason: string | null
  last_message: string | null
  notification_sent: boolean
  resolved_by: string | null
  resolved_at: string | null
  created_at: string | null
}

export interface HandoffSettings {
  notification_method: 'webhook' | 'whatsapp' | 'both' | 'none'
  webhook_url: string
  staff_whatsapp: string
  auto_pause_ai: boolean
}

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

export const handoffApi = {
  getSettings: () =>
    apiCall<{ settings: HandoffSettings }>('/handoff/settings'),

  saveSettings: (settings: HandoffSettings) =>
    apiCall<{ settings: HandoffSettings }>('/handoff/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),

  getSessions: (params?: { status?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.status) qs.set('status', params.status)
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs}` : ''
    return apiCall<{ sessions: HandoffSession[]; total: number; offset: number; limit: number }>(
      `/handoff/sessions${query}`
    )
  },

  resolveSession: (sessionId: number, resolvedBy = 'staff') =>
    apiCall<{ session_id: number; status: string; resolved_by: string; resolved_at: string }>(
      `/handoff/sessions/${sessionId}/resolve`,
      { method: 'PUT', body: JSON.stringify({ resolved_by: resolvedBy }) }
    ),
}
