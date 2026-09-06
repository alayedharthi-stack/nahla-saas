import { apiCall } from './client'

// ── Service keys (lifecycle slice A scope) ───────────────────────────────────

export type OrderUpdateServiceKey =
  | 'order_confirmation'
  | 'cod_confirmation'
  | 'payment_pending'
  | 'payment_confirmed'
  | 'order_preparing'
  | 'order_ready'
  | 'shipping_tracking'
  | 'out_for_delivery'
  | 'order_delivered'
  | 'order_cancelled'
  | 'order_refunded'

export const ORDER_UPDATE_SERVICE_KEYS: readonly OrderUpdateServiceKey[] = [
  'order_confirmation',
  'cod_confirmation',
  'payment_pending',
  'payment_confirmed',
  'order_preparing',
  'order_ready',
  'shipping_tracking',
  'out_for_delivery',
  'order_delivered',
  'order_cancelled',
  'order_refunded',
] as const

export function isOrderUpdateServiceKey(key: string): key is OrderUpdateServiceKey {
  return (ORDER_UPDATE_SERVICE_KEYS as readonly string[]).includes(key)
}

// ── Types (resilient to partial / evolving backend payloads) ─────────────────

export type MetaRevisionStatus =
  | 'DRAFT'
  | 'PENDING'
  | 'APPROVED'
  | 'REJECTED'
  | 'PAUSED'
  | 'DISABLED'
  | string

export interface OrderUpdateRevision {
  id?: string | number | null
  template_id?: string | number | null
  body_text?: string | null
  text?: string | null
  status?: MetaRevisionStatus | null
  label?: string | null
  approved_at?: string | null
  submitted_at?: string | null
  meta_template_name?: string | null
  meta_status?: MetaRevisionStatus | null
}

export interface OrderUpdateServiceToggle {
  enabled?: boolean
}

export interface OrderUpdatesSettings {
  enabled?: boolean
  services?: Partial<Record<OrderUpdateServiceKey, OrderUpdateServiceToggle>>
  flags?: Partial<Record<OrderUpdateServiceKey, boolean>>
  effective?: Partial<Record<OrderUpdateServiceKey, boolean>>
}

export const LEGACY_DEFAULT_ON_KEYS: readonly OrderUpdateServiceKey[] = [
  'order_confirmation',
  'shipping_tracking',
  'cod_confirmation',
] as const

export function persistedIndividualEnabled(
  settings: OrderUpdatesSettings | null | undefined,
  serviceKey: OrderUpdateServiceKey,
): boolean {
  if (!settings) return LEGACY_DEFAULT_ON_KEYS.includes(serviceKey)
  if (settings.flags && typeof settings.flags[serviceKey] === 'boolean') {
    return settings.flags[serviceKey] as boolean
  }
  const nested = settings.services?.[serviceKey]?.enabled
  if (typeof nested === 'boolean') return nested
  return LEGACY_DEFAULT_ON_KEYS.includes(serviceKey)
}

export function effectiveEnabledAfterMaster(
  settings: OrderUpdatesSettings | null | undefined,
  serviceKey: OrderUpdateServiceKey,
): boolean {
  if (settings?.effective && typeof settings.effective[serviceKey] === 'boolean') {
    return settings.effective[serviceKey] as boolean
  }
  return isMasterEnabled(settings) && persistedIndividualEnabled(settings, serviceKey)
}

export interface OrderUpdateServiceDetail {
  service_key?: OrderUpdateServiceKey | string
  enabled?: boolean
  body_text?: string | null
  message_text?: string | null
  variables?: string[] | Record<string, string> | null
  available_variables?: string[] | null
  meta_status?: MetaRevisionStatus | null
  live_revision?: OrderUpdateRevision | null
  approved_revision?: OrderUpdateRevision | null
  pending_revision?: OrderUpdateRevision | null
  last_approved_revision?: OrderUpdateRevision | null
  preview_body?: string | null
  preview_footer?: string | null
}

export interface CreateOrderUpdateRevisionPayload {
  body_text: string
  text?: string
  submit_to_meta: boolean
}

export interface CreateOrderUpdateRevisionResponse {
  revision?: OrderUpdateRevision | null
  service?: OrderUpdateServiceDetail | null
  detail?: OrderUpdateServiceDetail | null
  ok?: boolean
}

// ── Normalizers ─────────────────────────────────────────────────────────────

export function revisionBodyText(rev: OrderUpdateRevision | null | undefined): string {
  if (!rev) return ''
  return (rev.body_text ?? rev.text ?? '').trim()
}

export function serviceBodyText(detail: OrderUpdateServiceDetail | null | undefined): string {
  if (!detail) return ''
  const pending = revisionBodyText(detail.pending_revision)
  const direct = (detail.body_text ?? detail.message_text ?? '').trim()
  if (pending) return pending
  if (direct) return direct
  return revisionBodyText(detail.live_revision ?? detail.approved_revision ?? detail.last_approved_revision)
}

export function serviceVariables(detail: OrderUpdateServiceDetail | null | undefined): string[] {
  if (!detail) return []
  const raw = detail.available_variables ?? detail.variables
  if (Array.isArray(raw)) return raw.filter(v => typeof v === 'string' && v.trim())
  if (raw && typeof raw === 'object') return Object.keys(raw)
  return []
}

export function approvedRevision(detail: OrderUpdateServiceDetail | null | undefined): OrderUpdateRevision | null {
  if (!detail) return null
  return (
    detail.live_revision
    ?? detail.approved_revision
    ?? detail.last_approved_revision
    ?? null
  )
}

export function isMasterEnabled(settings: OrderUpdatesSettings | null | undefined): boolean {
  if (!settings) return true
  if (typeof settings.enabled === 'boolean') return settings.enabled
  return true
}

export function isServiceEnabled(
  settings: OrderUpdatesSettings | null | undefined,
  serviceKey: OrderUpdateServiceKey,
): boolean {
  return persistedIndividualEnabled(settings, serviceKey)
}

export function patchPayloadForIndividual(
  serviceKey: OrderUpdateServiceKey,
  enabled: boolean,
): OrderUpdatesSettings {
  return { services: { [serviceKey]: { enabled } } }
}

export function patchPayloadForMaster(enabled: boolean): OrderUpdatesSettings {
  return { enabled }
}

export function isNotFoundError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const status = (err as { status?: number }).status
  return status === 404
}

// ── API ───────────────────────────────────────────────────────────────────────

export const orderUpdatesApi = {
  getSettings: () => apiCall<OrderUpdatesSettings>('/order-updates/settings'),

  putSettings: (payload: OrderUpdatesSettings) =>
    apiCall<OrderUpdatesSettings>('/order-updates/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  patchSettings: (payload: OrderUpdatesSettings) =>
    apiCall<OrderUpdatesSettings>('/order-updates/settings', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  getService: (serviceKey: OrderUpdateServiceKey) =>
    apiCall<OrderUpdateServiceDetail>(`/order-updates/${serviceKey}`),

  createRevision: (
    serviceKey: OrderUpdateServiceKey,
    payload: CreateOrderUpdateRevisionPayload,
  ) =>
    apiCall<CreateOrderUpdateRevisionResponse>(`/order-updates/${serviceKey}/revisions`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  submitRevision: (
    serviceKey: OrderUpdateServiceKey,
    templateId: string | number,
  ) =>
    apiCall<CreateOrderUpdateRevisionResponse>(
      `/order-updates/${serviceKey}/revisions/${templateId}/submit`,
      { method: 'POST' },
    ),
}
