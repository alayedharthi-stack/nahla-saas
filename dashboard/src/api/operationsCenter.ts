/**
 * Operations Center API — structured branches, contacts, escalation (PR-B).
 */
import { apiCall } from './client'

export interface MerchantBranch {
  id: number
  tenant_id: number
  name: string
  city: string
  district: string
  address: string
  maps_url: string
  hours_json: Record<string, unknown> | null
  is_active: boolean
  sort_order: number
  contact_count: number
  created_at?: string | null
  updated_at?: string | null
}

export interface BranchContact {
  id: number
  branch_id: number
  display_name: string
  role: string
  phone_e164: string
  whatsapp_e164: string
  is_active: boolean
  is_default_reception: boolean
  sort_order: number
}

export interface BranchEscalationStep {
  id: number
  branch_id: number
  escalation_level: number
  display_name: string
  role: string
  phone_e164: string
  is_active: boolean
  sort_order: number
}

export type BranchInput = {
  name: string
  city?: string
  district?: string
  address?: string
  maps_url?: string
  hours_json?: Record<string, unknown> | null
  is_active?: boolean
  sort_order?: number
}

export type ContactInput = {
  display_name: string
  role?: string
  phone_e164: string
  whatsapp_e164?: string
  is_active?: boolean
  is_default_reception?: boolean
  sort_order?: number
}

export type EscalationStepInput = {
  escalation_level: number
  display_name: string
  role?: string
  phone_e164: string
  is_active?: boolean
  sort_order?: number
}

export const operationsCenterApi = {
  listBranches: () =>
    apiCall<{ branches: MerchantBranch[] }>('/operations-center/branches'),

  getBranch: (id: number) =>
    apiCall<MerchantBranch>(`/operations-center/branches/${id}`),

  createBranch: (body: BranchInput) =>
    apiCall<MerchantBranch>('/operations-center/branches', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateBranch: (id: number, body: Partial<BranchInput>) =>
    apiCall<MerchantBranch>(`/operations-center/branches/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  deleteBranch: (id: number) =>
    apiCall<void>(`/operations-center/branches/${id}`, { method: 'DELETE' }),

  activateBranch: (id: number) =>
    apiCall<MerchantBranch>(`/operations-center/branches/${id}/activate`, {
      method: 'POST',
    }),

  deactivateBranch: (id: number) =>
    apiCall<MerchantBranch>(`/operations-center/branches/${id}/deactivate`, {
      method: 'POST',
    }),

  listContacts: (branchId: number) =>
    apiCall<{ contacts: BranchContact[] }>(
      `/operations-center/branches/${branchId}/contacts`,
    ),

  createContact: (branchId: number, body: ContactInput) =>
    apiCall<BranchContact>(`/operations-center/branches/${branchId}/contacts`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateContact: (branchId: number, contactId: number, body: Partial<ContactInput>) =>
    apiCall<BranchContact>(
      `/operations-center/branches/${branchId}/contacts/${contactId}`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  deleteContact: (branchId: number, contactId: number) =>
    apiCall<void>(
      `/operations-center/branches/${branchId}/contacts/${contactId}`,
      { method: 'DELETE' },
    ),

  setDefaultReception: (branchId: number, contactId: number) =>
    apiCall<BranchContact>(
      `/operations-center/branches/${branchId}/contacts/${contactId}/set-default-reception`,
      { method: 'POST' },
    ),

  listEscalationSteps: (branchId: number) =>
    apiCall<{ steps: BranchEscalationStep[] }>(
      `/operations-center/branches/${branchId}/escalation-steps`,
    ),

  createEscalationStep: (branchId: number, body: EscalationStepInput) =>
    apiCall<BranchEscalationStep>(
      `/operations-center/branches/${branchId}/escalation-steps`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  updateEscalationStep: (
    branchId: number,
    stepId: number,
    body: Partial<EscalationStepInput>,
  ) =>
    apiCall<BranchEscalationStep>(
      `/operations-center/branches/${branchId}/escalation-steps/${stepId}`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  deleteEscalationStep: (branchId: number, stepId: number) =>
    apiCall<void>(
      `/operations-center/branches/${branchId}/escalation-steps/${stepId}`,
      { method: 'DELETE' },
    ),

  reorderEscalationSteps: (branchId: number, stepIds: number[]) =>
    apiCall<{ steps: BranchEscalationStep[] }>(
      `/operations-center/branches/${branchId}/escalation-steps/reorder`,
      { method: 'POST', body: JSON.stringify({ step_ids: stepIds }) },
    ),
}
