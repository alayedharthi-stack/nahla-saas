import { apiCall } from './client'

export type SupportAccessTargetStatus =
  | 'NONE'
  | 'PENDING'
  | 'APPROVED'
  | 'REVOKED'
  | 'EXPIRED'
  | 'REJECTED'

export interface SupportAccessTarget {
  tenant_id: number
  tenant_name: string
  status: SupportAccessTargetStatus
  can_request: boolean
  approval_recipient_available: boolean
  request_reference: string | null
  requested_at: string | null
  duration_hours: number | null
}

export interface CreateSupportAccessRequest {
  tenant_id: number
  purpose: string
  duration_hours: number
}

export const supportAccessApi = {
  targets: () =>
    apiCall<{ count: number; targets: SupportAccessTarget[] }>(
      '/admin/support-access/targets',
    ),

  request: (body: CreateSupportAccessRequest) =>
    apiCall<{
      request_id: string
      status: 'pending'
      tenant_id: number
      duration_hours: number
      message: string
    }>('/admin/support-access/requests', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}
