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

// Canonical product-source strings the backend stamps on every row.
// Mirrors `KNOWN_SOURCES` in backend/core/catalog.py. Extend together.
//
// Hub architecture: each value names an INPUT side that feeds the
// central Nahla Catalog. ``meta`` is for products imported FROM the
// merchant's Meta Commerce Manager catalog — even if they will later
// be pushed BACK to Meta as an output channel, the input-side tag is
// permanent.
export type ProductSource = 'salla' | 'zid' | 'meta' | 'manual' | 'unknown'
export type DominantSource = ProductSource | 'mixed'

// Output channels the catalog can publish to. ``google_merchant`` and
// ``checkout`` are planning-only today.
export type CatalogChannel =
  | 'whatsapp'
  | 'meta_catalog'
  | 'ai'
  | 'campaigns'
  | 'google_merchant'
  | 'checkout'

// ── Product Studio types (May 2026 #15) ──────────────────────────────
//
// Mirrors backend/services/product_readiness.* and the new Studio
// endpoints in routers/catalog.py.

export type ReadinessState = 'ok' | 'warn' | 'error' | 'missing'
export type ReadinessLevel = 'green' | 'amber' | 'red' | 'slate'

export interface ReadinessFieldStatus {
  field:     string
  label_ar:  string
  state:     ReadinessState
  count:     number | null
  limit:     number | null
  soft_at:   number | null
  message:   string
  rationale: string
  required:  boolean
}

export interface ChannelReadiness {
  channel:        string
  label_ar:       string
  icon_key:       string
  enabled:        boolean
  ready:          boolean
  score_pct:      number
  blocking_count: number
  warnings_count: number
  fields:         ReadinessFieldStatus[]
}

export interface ProductBadge {
  enabled_total:  number
  ready_count:    number
  warn_count:     number
  blocking_count: number
  score_pct:      number
  level:          ReadinessLevel
}

export interface CatalogProductDiagRow {
  id:                    number
  title:                 string
  external_id:           string | null
  sku:                   string | null
  meta_retailer_id:      string | null
  effective_retailer_id: string | null
  publish_status:        'published' | 'ready' | 'needs_mapping'
  in_stock:              boolean
  stock_quantity:        number | null
  price:                 string | null
  image_url:             string
  product_url:           string
  source:                ProductSource
  readiness_badge:       ProductBadge | null
}

export interface CatalogProductDiagResponse {
  rows:          CatalogProductDiagRow[]
  total:         number   // post-filter count (drives pagination)
  tenant_total?: number   // unfiltered count (tenant-wide stat)
  limit:         number
  offset:        number
  coverage: {
    with_rid:    number
    missing_rid: number
    published:   number
    unpublished: number
    total:       number
  }
  filters_applied?: {
    q:               string | null
    source:          string | null
    has_image:       boolean | null
    has_retailer_id: boolean | null
    in_stock:        boolean | null
  }
}

// Studio filters — typed query-string for the products list.
export interface StudioFilters {
  q?:               string
  source?:          ProductSource
  has_image?:       boolean
  has_retailer_id?: boolean
  in_stock?:        boolean
}

// Full product detail returned by GET /products/{id}.
export interface StudioProduct {
  id:                          number
  tenant_id:                   number
  title:                       string
  description:                 string | null
  price:                       string | null
  sku:                         string | null
  external_id:                 string | null
  meta_retailer_id:            string | null
  effective_retailer_id:       string
  in_stock:                    boolean
  stock_quantity:              number | null
  source:                      ProductSource
  image_url:                   string
  product_url:                 string
  additional_images:           string[]
  sale_price:                  string
  currency:                    string
  availability:                string
  brand:                       string
  category:                    string
  condition:                   string
  gtin:                        string
  mpn:                         string
  variants:                    Array<Record<string, unknown>>
  meta_catalog_published_at:   string | null
}

export interface ProductDetailResponse {
  product:     StudioProduct
  per_channel: ChannelReadiness[]
}

// Body for the readiness preview endpoint — mirrors
// `_ReadinessPreviewBody`. Every field optional so the live counter
// works from the first keystroke.
export interface ReadinessPreviewBody {
  title?:             string
  description?:       string
  price?:             string
  sale_price?:        string
  currency?:          string
  sku?:               string
  external_id?:       string
  meta_retailer_id?:  string
  image_url?:         string
  product_url?:       string
  additional_images?: string[]
  availability?:      string
  brand?:             string
  category?:          string
  condition?:         string
  gtin?:              string
  mpn?:               string
  in_stock?:          boolean
  stock_quantity?:    number
}

export interface ReadinessPreviewResponse {
  per_channel: ChannelReadiness[]
}

// Channel registry snapshot — drives the live-counter labels +
// tooltips so the dashboard never has to hard-code "Meta title is
// 200 chars".
export interface ChannelFieldSpec {
  field:            string
  label_ar:         string
  required:         boolean
  min_length:       number | null
  max_length:       number | null
  allowed_values:   string[] | null
  regex:            string | null
  soft_warn_at_pct: number
  rationale_ar:     string
}

export interface ChannelSpecResponse {
  channel:        string
  label_ar:       string
  icon_key:       string
  enabled:        boolean
  description_ar: string
  image_required: boolean
  fields:         ChannelFieldSpec[]
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

// ── Import from Meta (Hub architecture — May 2026 #14) ───────────────
//
// Pull merchant products FROM Meta Commerce Manager INTO the Nahla
// catalog. Mirrors `services/meta_catalog_import.ImportReport`.

export interface MetaImportReport {
  scanned:        number
  created:        number
  updated:        number
  skipped_manual: number
  errors:         number
  pages_fetched:  number
  truncated:      boolean
  error_samples:  Array<{
    id?:          string | null
    retailer_id?: string | null
    reason:       string
  }>
}

export interface MetaImportResponse {
  ok:     boolean
  report: MetaImportReport
}

// Error detail codes the backend returns on preflight failure. The UI
// matches on these to render the right remediation copy.
export type MetaImportErrorCode =
  | 'connection_not_found'
  | 'catalog_id_missing'
  | 'access_token_missing'
  | 'meta_http_error'

// ── Diagnostics (source-agnostic snapshot) ───────────────────────────
//
// Shape mirrors `backend/routers/catalog.py::_diagnostics_payload`.
// Used by the new "Catalog readiness" card on top of /catalog.

export interface CatalogDiagnostics {
  catalog: {
    catalog_id_present:  boolean
    catalog_id:          string
    catalog_enabled:     boolean
    whatsapp_connected:  boolean
  }
  products: {
    total:                          number
    with_effective_retailer_id:     number
    without_effective_retailer_id:  number
    coverage_pct:                   number
    source_breakdown:               Partial<Record<ProductSource, number>>
    dominant_source:                DominantSource
  }
  readiness: {
    catalog_ready:  boolean
  }
}

// ── Manual product CRUD (Path 3 — no-Salla merchants) ────────────────
//
// Mirrors `_ManualProductIn` / `_ManualProductPatch` in
// backend/routers/catalog.py. All fields except ``title`` are optional
// on create; every field is optional on patch.

export interface ManualProductInput {
  title:             string
  description?:      string | null
  price?:            string | null
  sku?:              string | null
  external_id?:      string | null
  meta_retailer_id?: string | null
  image_url?:        string | null
  product_url?:      string | null
  in_stock?:         boolean
  stock_quantity?:   number | null
}

export interface ManualProductRow {
  id:                     number
  tenant_id:              number
  title:                  string
  description:            string | null
  price:                  string | null
  sku:                    string | null
  external_id:            string | null
  meta_retailer_id:       string | null
  effective_retailer_id:  string
  in_stock:               boolean
  stock_quantity:         number | null
  source:                 ProductSource
  image_url:              string
  product_url:            string
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
  products(
    limit: number = 50,
    offset: number = 0,
    filters?: StudioFilters,
  ): Promise<CatalogProductDiagResponse> {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (filters?.q)               qs.set('q', filters.q)
    if (filters?.source)          qs.set('source', filters.source)
    if (filters?.has_image       !== undefined) qs.set('has_image',       String(filters.has_image))
    if (filters?.has_retailer_id !== undefined) qs.set('has_retailer_id', String(filters.has_retailer_id))
    if (filters?.in_stock        !== undefined) qs.set('in_stock',        String(filters.in_stock))
    return apiCall<CatalogProductDiagResponse>(`/merchant/catalog/products?${qs.toString()}`)
  },
  productDetail(id: number): Promise<ProductDetailResponse> {
    return apiCall<ProductDetailResponse>(`/merchant/catalog/products/${id}`)
  },
  readinessPreview(draft: ReadinessPreviewBody): Promise<ReadinessPreviewResponse> {
    return apiCall<ReadinessPreviewResponse>('/merchant/catalog/readiness/preview', {
      method: 'POST',
      body:   JSON.stringify(draft),
    })
  },
  channels(): Promise<{ channels: ChannelSpecResponse[] }> {
    return apiCall<{ channels: ChannelSpecResponse[] }>('/merchant/catalog/channels')
  },
  resync(): Promise<CatalogResyncResponse> {
    return apiCall<CatalogResyncResponse>('/merchant/catalog/resync', { method: 'POST' })
  },
  diagnostics(): Promise<CatalogDiagnostics> {
    return apiCall<CatalogDiagnostics>('/merchant/catalog/diagnostics')
  },
  // Manual product CRUD — Path 3 in the new architecture.
  createManualProduct(body: ManualProductInput): Promise<ManualProductRow> {
    return apiCall<ManualProductRow>('/merchant/catalog/products/manual', {
      method: 'POST',
      body:   JSON.stringify(body),
    })
  },
  updateManualProduct(id: number, body: Partial<ManualProductInput>): Promise<ManualProductRow> {
    return apiCall<ManualProductRow>(`/merchant/catalog/products/manual/${id}`, {
      method: 'PATCH',
      body:   JSON.stringify(body),
    })
  },
  deleteManualProduct(id: number): Promise<{ deleted: boolean; id: number }> {
    return apiCall<{ deleted: boolean; id: number }>(
      `/merchant/catalog/products/manual/${id}`,
      { method: 'DELETE' },
    )
  },
  // Import from Meta — Path 4. Pulls products from Meta Commerce
  // Manager into the Nahla catalog. Idempotent.
  importFromMeta(): Promise<MetaImportResponse> {
    return apiCall<MetaImportResponse>('/merchant/catalog/import/meta', {
      method: 'POST',
    })
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
  diagnostics(tenantId: number): Promise<CatalogDiagnostics> {
    const qs = new URLSearchParams({ tenant_id: String(tenantId) })
    return apiCall<CatalogDiagnostics>(`/admin/catalog/diagnostics?${qs.toString()}`)
  },
}
