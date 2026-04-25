// ── Customer Import API client ────────────────────────────────────────────────
// Wraps the four-step wizard endpoints under /customers/import.
//
// File upload uses raw fetch + FormData because `apiCall` always sets
// `Content-Type: application/json`, which would corrupt the multipart body.
// Auth headers are attached manually here so the upload still hits the
// tenant-scoped endpoint correctly.

import { getToken, getTenantId, logout } from '../auth'
import { apiCall, API_BASE } from './client'

export type ImportClassification = 'new' | 'exact' | 'suspect' | 'invalid'

export type ImportBatchStatus =
  | 'parsed'
  | 'previewed'
  | 'committed'
  | 'failed'

export interface ImportBatch {
  id: number
  filename: string | null
  file_kind: string | null
  status: ImportBatchStatus
  column_mapping: Record<string, string>
  total_rows: number
  summary: {
    new: number
    matched: number
    suspects: number
    invalid: number
  }
  result: {
    created: number
    updated: number
    skipped: number
    errors: number
  }
  created_at: string | null
  committed_at: string | null
  error_message: string | null
}

export interface UploadResponse {
  batch: ImportBatch
  headers: string[]
  supported_fields: string[]
  suggested_mapping: Record<string, string>
  sample_rows: Record<string, string>[]
}

export interface ClassifiedNormalized {
  row_index: number
  raw: Record<string, string>
  name: string
  phone_raw: string
  normalized_phone: string
  email: string
  city: string
  notes: string
  source: string
  invalid_reasons: string[]
}

export interface SuspectCandidate {
  customer_id: number | null
  name: string
  email: string
  normalized_phone: string
  acquisition_channel: string
  reason: string
}

export interface ClassifiedRow {
  row_index: number
  classification: ImportClassification
  normalized: ClassifiedNormalized
  match_customer_id: number | null
  match_reason: string
  suspect_candidates: SuspectCandidate[]
  /** acquisition_channel of the matched existing customer (e.g. "salla_sync") */
  match_acquisition_channel: string
  /** Current name of the matched existing customer */
  match_customer_name: string
}

export interface MappingResponse {
  batch: ImportBatch
  sample: Record<ImportClassification, ClassifiedRow[]>
}

export interface RowsResponse {
  items: ClassifiedRow[]
  page: number
  page_size: number
  total: number
}

export interface CommitOptions {
  apply_new: boolean
  update_existing: boolean
  /** map row_index → "skip" | "create_new" | "merge_into:<id>" */
  suspect_decisions: Record<number, string>
}

export interface CommitResponse {
  batch: ImportBatch
  result: {
    created: number
    updated: number
    skipped: number
    errors: number
    error_rows: { row_index: number | null; error: string }[]
  }
}

// Multipart upload — bypass apiCall so we can set the right headers.
async function uploadFile(file: File): Promise<UploadResponse> {
  const token = getToken()
  const tenantId = getTenantId()
  const form = new FormData()
  form.append('file', file)

  let res: Response
  try {
    res = await fetch(`${API_BASE}/customers/import/upload`, {
      method: 'POST',
      cache: 'no-store',
      mode: 'cors',
      headers: {
        ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: form,
    })
  } catch (err) {
    throw new Error('تعذر رفع الملف — تحقق من الاتصال بالخادم.')
  }

  if (res.status === 401) {
    let code = ''
    try { code = (await res.clone().json())?.code ?? '' } catch {}
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
    } catch {}
    throw new Error(detail)
  }

  return res.json() as Promise<UploadResponse>
}

export const customerImportApi = {
  upload: uploadFile,

  submitMapping(batchId: number, mapping: Record<string, string>, defaultRegion = 'SA') {
    return apiCall<MappingResponse>(`/customers/import/${batchId}/mapping`, {
      method: 'POST',
      body: JSON.stringify({ mapping, default_region: defaultRegion }),
    })
  },

  getBatch(batchId: number) {
    return apiCall<{ batch: ImportBatch }>(`/customers/import/${batchId}`)
  },

  listRows(batchId: number, opts: {
    status?: ImportClassification
    page?: number
    pageSize?: number
  } = {}) {
    const params = new URLSearchParams()
    if (opts.status)   params.set('status', opts.status)
    if (opts.page)     params.set('page', String(opts.page))
    if (opts.pageSize) params.set('page_size', String(opts.pageSize))
    const qs = params.toString()
    return apiCall<RowsResponse>(
      `/customers/import/${batchId}/rows${qs ? `?${qs}` : ''}`,
    )
  },

  commit(batchId: number, options: CommitOptions) {
    return apiCall<CommitResponse>(`/customers/import/${batchId}/commit`, {
      method: 'POST',
      body: JSON.stringify(options),
    })
  },

  list(limit = 20) {
    return apiCall<{ items: ImportBatch[] }>(`/customers/import?limit=${limit}`)
  },

  delete(batchId: number) {
    return apiCall<{ ok: boolean }>(`/customers/import/${batchId}`, {
      method: 'DELETE',
    })
  },
}
