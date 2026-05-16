/**
 * catalog.ts
 * ──────────
 * Typed client for the WhatsApp Catalog settings surface. Backs the
 * merchant "كتالوج واتساب" section in WhatsApp integrations and the
 * admin "Catalog Audit" page.
 *
 * Mirrors backend/routers/catalog.py 1:1 — every field listed in
 * each interface MUST exist on the server response. The dashboard
 * relies on this shape staying stable; a server-side rename should
 * land here in the same commit.
 */
import { apiCall } from './client'

// ── Shared shapes ────────────────────────────────────────────────────

export interface CatalogConnectionBlock {
  found:           boolean
  phone_id_tail:   string | null
  status:          string | null
  catalog_enabled: boolean
  meta_catalog_id: string | null
}

export interface CatalogEligibilityBlock {
  ok:     boolean
  reason: string
}

export interface CatalogProductRow {
  id:                    number
  title:                 string
  external_id:           string | null
  meta_retailer_id:      string | null
  effective_retailer_id: string | null
}

export interface CatalogCoverage {
  with_retailer_id:    number
  without_retailer_id: number
  sample_size:         number
}

export interface CatalogStatus {
  tenant_id:        number
  connection:       CatalogConnectionBlock
  eligibility:      CatalogEligibilityBlock
  products_sample:  CatalogProductRow[]
  coverage:         CatalogCoverage
  advice:           string
}

export interface CatalogConfigPatch {
  meta_catalog_id?: string | null
  catalog_enabled?: boolean
}

export interface CatalogTestSendBody {
  to:             string
  product_id?:    number
  product_title?: string
  mode?:          'auto' | 'catalog' | 'image' | 'cta'
}

export interface CatalogTestSendResult {
  ok:             boolean
  tenant_id:      number
  to_masked:      string
  mode_requested: string
  product: {
    id:           number
    title:        string
    external_id:  string | null
    retailer_id:  string | null
    image_url:    boolean
    product_url:  boolean
  }
  catalog: {
    eligible:  boolean
    reason:    string
    attempted: boolean
    succeeded: boolean
    raw_error: string | null
  }
  image_cta: {
    attempted:  boolean
    image_ok:   boolean
    cta_ok:     boolean
    raw_error?: string | null
  }
  cta_only: {
    attempted:  boolean
    ok:         boolean
    raw_error?: string | null
  }
  final_mode: string
}

export interface CatalogPatchResponse {
  ok: boolean
  applied_changes: Record<string, { before: unknown; after: unknown }>
  status: CatalogStatus
}

// ── Product diagnostic + resync ──────────────────────────────────────

export interface CatalogProductDiagRow {
  id:                    number
  title:                 string
  external_id:           string | null
  meta_retailer_id:      string | null
  effective_retailer_id: string | null
  publish_status:        'published' | 'ready' | 'needs_mapping'
  in_stock:              boolean
}

export interface CatalogProductDiagResponse {
  rows:   CatalogProductDiagRow[]
  total:  number
  limit:  number
  offset: number
  coverage: {
    with_rid:    number
    missing_rid: number
    published:   number
    unpublished: number
    total:       number
  }
}

export interface CatalogResyncReport {
  scanned:            number
  retailer_id_set:    number
  already_set:        number
  synthetic_assigned: number
  published_stamped:  number
  errors:             number
}

export interface CatalogResyncResponse {
  ok:     boolean
  report: CatalogResyncReport
}

// ── Merchant surface ─────────────────────────────────────────────────

export const catalogApi = {
  status(): Promise<CatalogStatus> {
    return apiCall<CatalogStatus>('/merchant/catalog/status')
  },
  patch(body: CatalogConfigPatch): Promise<CatalogPatchResponse> {
    return apiCall<CatalogPatchResponse>('/merchant/catalog/config', {
      method: 'PATCH',
      body:   JSON.stringify(body),
    })
  },
  testSend(body: CatalogTestSendBody): Promise<CatalogTestSendResult> {
    return apiCall<CatalogTestSendResult>('/merchant/catalog/test-send', {
      method: 'POST',
      body:   JSON.stringify(body),
    })
  },
  products(limit: number = 50, offset: number = 0): Promise<CatalogProductDiagResponse> {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    return apiCall<CatalogProductDiagResponse>(`/merchant/catalog/products?${qs.toString()}`)
  },
  resync(): Promise<CatalogResyncResponse> {
    return apiCall<CatalogResyncResponse>('/merchant/catalog/resync', { method: 'POST' })
  },
}

// ── Admin surface ────────────────────────────────────────────────────

export interface AdminCatalogAuditRow {
  tenant_id:             number
  merchant_name:         string | null
  whatsapp_connected:    boolean
  catalog_enabled:       boolean
  meta_catalog_id_set:   boolean
  eligibility_ok:        boolean
  eligibility_reason:    string
  products_total:        number
  products_with_rid:     number
  products_with_rid_pct: number
}

export interface AdminCatalogAuditResponse {
  rows:  AdminCatalogAuditRow[]
  count: number
}

export interface AdminCatalogConfigPatch extends CatalogConfigPatch {
  tenant_id: number
}

export interface AdminCatalogTestSendBody extends CatalogTestSendBody {
  tenant_id: number
}

export const adminCatalogApi = {
  status(tenantId: number, sample: number = 5): Promise<CatalogStatus> {
    const qs = new URLSearchParams({
      tenant_id: String(tenantId),
      sample:    String(sample),
    })
    return apiCall<CatalogStatus>(`/admin/catalog/status?${qs.toString()}`)
  },
  audit(only_connected: boolean = true, limit: number = 200):
      Promise<AdminCatalogAuditResponse> {
    const qs = new URLSearchParams({
      only_connected: String(only_connected),
      limit:          String(limit),
    })
    return apiCall<AdminCatalogAuditResponse>(
      `/admin/catalog/audit?${qs.toString()}`,
    )
  },
  patch(body: AdminCatalogConfigPatch): Promise<CatalogPatchResponse> {
    return apiCall<CatalogPatchResponse>('/admin/catalog/config', {
      method: 'PATCH',
      body:   JSON.stringify(body),
    })
  },
  testSend(body: AdminCatalogTestSendBody): Promise<CatalogTestSendResult> {
    return apiCall<CatalogTestSendResult>('/admin/catalog/test-send', {
      method: 'POST',
      body:   JSON.stringify(body),
    })
  },
  products(tenantId: number, limit: number = 50, offset: number = 0): Promise<CatalogProductDiagResponse> {
    const qs = new URLSearchParams({
      tenant_id: String(tenantId), limit: String(limit), offset: String(offset),
    })
    return apiCall<CatalogProductDiagResponse>(`/admin/catalog/products?${qs.toString()}`)
  },
  resync(tenantId: number): Promise<CatalogResyncResponse> {
    return apiCall<CatalogResyncResponse>('/admin/catalog/resync', {
      method: 'POST',
      body:   JSON.stringify({ tenant_id: tenantId }),
    })
  },
}
