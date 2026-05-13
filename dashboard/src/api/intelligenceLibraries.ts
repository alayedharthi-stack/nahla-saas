// ── Intelligence Libraries API client ─────────────────────────────────────
// Wraps the manual-coupons and AI-media-library CRUD endpoints exposed by
// `backend/routers/intelligence_libraries.py`. Both libraries are tenant-scoped
// and used by the merchant brain when the customer asks for a discount or when
// the brain decides to attach an image / video / document to its reply.
//
// File uploads bypass `apiCall` because `apiCall` always sets
// `Content-Type: application/json`, which would corrupt the multipart body.

import { getToken, getTenantId, logout } from '../auth'
import { apiCall, API_BASE } from './client'

export type AIMediaType = 'image' | 'video' | 'pdf' | 'document' | 'audio'

export interface ManualCoupon {
  id: number
  tenant_id: number
  code: string
  title: string | null
  description: string | null
  discount_text: string | null
  usage_context: string | null
  is_active: boolean
  priority: number
  starts_at: string | null
  expires_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ManualCouponInput {
  code: string
  title?: string | null
  description?: string | null
  discount_text?: string | null
  usage_context?: string | null
  is_active?: boolean
  priority?: number
  starts_at?: string | null
  expires_at?: string | null
}

export interface AIMediaItem {
  id: number
  tenant_id: number
  title: string
  description: string | null
  media_type: AIMediaType
  file_url: string
  thumbnail_url: string | null
  usage_context: string | null
  tags: string[]
  is_active: boolean
  priority: number
  storage_kind: 'external' | 'local'
  mime_type: string | null
  file_size_bytes: number | null
  // Stable namespaced key (e.g. `payment_rajhi_barcode`). When set,
  // the AI emits `[MEDIA_KEY:<slug>]` markers that resolve to this
  // row deterministically — independent of relevance scoring.
  media_key: string | null
  created_at: string | null
  updated_at: string | null
}

export interface AIMediaInput {
  title: string
  description?: string | null
  media_type: AIMediaType
  file_url: string
  thumbnail_url?: string | null
  usage_context?: string | null
  tags?: string[]
  is_active?: boolean
  priority?: number
  media_key?: string | null
}

export interface AIMediaUploadInput {
  file: File
  title: string
  media_type: AIMediaType
  description?: string
  usage_context?: string
  tags?: string[]
  is_active?: boolean
  priority?: number
  media_key?: string | null
}

// Registry entry returned by GET /intelligence/ai-media/keys.
// One per well-known slug the AI knows how to emit.
export interface MediaKeyOption {
  key: string
  label_ar: string
  description_ar: string
  intent: 'payment' | 'shipping' | 'store' | 'product_meta' | 'legal' | string
  expected_media_type: 'image' | 'video' | 'document' | string
}

async function uploadAIMedia(payload: AIMediaUploadInput): Promise<AIMediaItem> {
  const token = getToken()
  const tenantId = getTenantId()
  const form = new FormData()
  form.append('file', payload.file)
  form.append('title', payload.title)
  form.append('media_type', payload.media_type)
  if (payload.description !== undefined) form.append('description', payload.description)
  if (payload.usage_context !== undefined) form.append('usage_context', payload.usage_context)
  if (payload.tags && payload.tags.length) form.append('tags', payload.tags.join(','))
  if (payload.priority !== undefined) form.append('priority', String(payload.priority))
  if (payload.is_active !== undefined) form.append('is_active', String(payload.is_active))
  if (payload.media_key !== undefined && payload.media_key !== null) {
    form.append('media_key', payload.media_key)
  }

  let res: Response
  try {
    res = await fetch(`${API_BASE}/intelligence/ai-media/upload`, {
      method: 'POST',
      cache: 'no-store',
      mode: 'cors',
      headers: {
        ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: form,
    })
  } catch {
    throw new Error('تعذر رفع الملف — تحقق من الاتصال بالخادم.')
  }

  if (res.status === 401) {
    let code = ''
    try { code = (await res.clone().json())?.code ?? '' } catch { /* noop */ }
    if (['missing_token', 'invalid_token', 'no_tenant_claim'].includes(code)) {
      logout()
      window.location.href = '/login'
      throw new Error('انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً')
    }
  }

  if (!res.ok) {
    let detail = `API error ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* noop */ }
    throw new Error(detail)
  }

  return (await res.json()) as AIMediaItem
}

export const intelligenceLibrariesApi = {
  // ── Manual coupons ────────────────────────────────────────────────────
  listManualCoupons(onlyActive = false) {
    const params = onlyActive ? '?only_active=true' : ''
    return apiCall<{ items: ManualCoupon[] }>(`/intelligence/manual-coupons${params}`)
  },

  createManualCoupon(payload: ManualCouponInput) {
    return apiCall<ManualCoupon>('/intelligence/manual-coupons', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  updateManualCoupon(id: number, payload: Partial<ManualCouponInput>) {
    return apiCall<ManualCoupon>(`/intelligence/manual-coupons/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },

  toggleManualCoupon(id: number) {
    return apiCall<ManualCoupon>(`/intelligence/manual-coupons/${id}/toggle`, {
      method: 'POST',
    })
  },

  deleteManualCoupon(id: number) {
    return apiCall<{ deleted: boolean; id: number }>(`/intelligence/manual-coupons/${id}`, {
      method: 'DELETE',
    })
  },

  // ── AI media library ──────────────────────────────────────────────────
  // List of well-known media keys the AI knows how to emit. The
  // dashboard renders this as a dropdown grouped by intent so the
  // merchant doesn't have to memorise slugs.
  listMediaKeys() {
    return apiCall<{ items: MediaKeyOption[] }>('/intelligence/ai-media/keys')
  },

  listAIMedia(onlyActive = false, mediaType?: AIMediaType) {
    const params = new URLSearchParams()
    if (onlyActive) params.set('only_active', 'true')
    if (mediaType) params.set('media_type', mediaType)
    const qs = params.toString()
    return apiCall<{ items: AIMediaItem[] }>(`/intelligence/ai-media${qs ? `?${qs}` : ''}`)
  },

  createAIMedia(payload: AIMediaInput) {
    return apiCall<AIMediaItem>('/intelligence/ai-media', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  uploadAIMedia,

  updateAIMedia(id: number, payload: Partial<AIMediaInput>) {
    return apiCall<AIMediaItem>(`/intelligence/ai-media/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },

  toggleAIMedia(id: number) {
    return apiCall<AIMediaItem>(`/intelligence/ai-media/${id}/toggle`, {
      method: 'POST',
    })
  },

  deleteAIMedia(id: number) {
    return apiCall<{ deleted: boolean; id: number }>(`/intelligence/ai-media/${id}`, {
      method: 'DELETE',
    })
  },
}
