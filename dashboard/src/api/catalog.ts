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
import { getToken, getTenantId, logout } from '../auth'
import { apiCall, API_BASE } from './client'

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

export interface WabaLinkedCatalog {
  id:   string
  name: string | null
}

/** Mirrors GET /merchant/catalog/waba-link-status (PR #509). */
export interface WabaCatalogLinkStatus {
  ok:                      boolean
  connected:               boolean
  waba_id:                 string | null
  expected_catalog_id:     string | null
  linked_catalogs:         WabaLinkedCatalog[]
  linked_catalog_ids:      string[]
  expected_catalog_linked: boolean | null
  token_source:            string
  http_status:             number | null
  missing:                 string[]
  error:                   string | null
  error_code?:             number | null
  error_type?:             string | null
  error_message?:          string | null
  error_category?:         string | null
  link_status?:            'linked' | 'mismatch' | 'not_linked' | 'unknown' | null
  catalog_exists?:         boolean | null
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
export type ProductSource = 'salla' | 'zid' | 'meta' | 'manual' | 'nahla_native' | 'unknown'
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

// Per-variant payload returned by the diagnostics row (migration
// 0064 — Phase 4). When the parent product has no real variants
// the array contains a single ``is_default=true`` synthetic row;
// when it does, callers can expand the row in ProductStudio and
// see one entry per sellable SKU.
export interface CatalogProductVariantRow {
  id:               number
  salla_variant_id: string | null
  sku:              string | null
  retailer_id:      string | null
  price:            string | null
  currency:         string | null
  stock_quantity:   number | null
  in_stock:         boolean
  is_default:       boolean
  options:          Record<string, unknown>
  option_summary:   string
  image_url:        string
}

export type CatalogVisibility = 'active' | 'hidden' | 'removed' | 'archived' | 'all'

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
  currency:              string | null
  sale_price?:           string | null
  regular_price?:        string | null
  is_on_sale?:           boolean
  image_url:             string
  product_url:           string
  source:                ProductSource
  catalog_status?:       string
  merchant_hidden_at?:   string | null
  meta_removed_at?:      string | null
  readiness_badge:       ProductBadge | null
  // Parent / variant intelligence layer (migration 0064).
  has_variants?:               boolean
  default_variant_id?:         number | null
  variants?:                   CatalogProductVariantRow[]
  variants_count?:             number
  sellable_variants_count?:    number
}

// Five-counter summary surfaced in the ProductStudio header.
// `products` = parent count, `variants` = real (non-default)
// sellable rows, the remaining four are channel-readiness pills.
export interface CatalogVariantsSummary {
  products:          number
  variants:          number
  variants_in_stock: number
  whatsapp_ready:    number
  meta_ready:        number
  google_ready:      number
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
  variants_summary?: CatalogVariantsSummary
  filters_applied?: {
    q:               string | null
    source:          string | null
    has_image:       boolean | null
    has_retailer_id: boolean | null
    in_stock:        boolean | null
    catalog_visibility?: string | null
  }
}

// Studio filters — typed query-string for the products list.
export interface StudioFilters {
  q?:               string
  source?:          ProductSource
  has_image?:       boolean
  has_retailer_id?: boolean
  in_stock?:        boolean
  catalog_visibility?: CatalogVisibility
}

// Full product detail returned by GET /products/{id}.
export interface ProductPublicationStatus {
  data_ready_for_whatsapp: boolean
  meta_catalog_synced:     boolean
  waba_catalog_linked:     boolean | null
  visible_in_whatsapp:     boolean
}

export type WhatsappCatalogSyncPhase =
  | 'idle'
  | 'queued'
  | 'syncing'
  | 'published'
  | 'pending_verification'
  | 'needs_attention'
  | 'blocked'
  | 'retrying'

export interface WhatsappCatalogSyncCounts {
  eligible: number
  pending: number
  syncing: number
  synced: number
  failed: number
  blocked: number
  pending_verification?: number
  skipped_ineligible: number
}

export interface WhatsappCatalogSyncFailure {
  product_id: number
  title: string
  sync_status: string
  error_summary: string
}

export interface WhatsappCatalogSyncStatus {
  ok: boolean
  tenant_id: number
  ready: boolean
  blocker_code: string | null
  message_ar: string | null
  action_ar: string | null
  phase: WhatsappCatalogSyncPhase
  counts: WhatsappCatalogSyncCounts
  last_success_at: string | null
  failures: WhatsappCatalogSyncFailure[]
  auto_sync_enabled?: boolean
  auto_sync_flag?: string
  verification?: {
    lookup_fields: string[]
    identity_fields?: string[]
    content_fields?: string[]
    not_verified_fields: string[]
    note_ar?: string
  }
}

export interface WhatsappCatalogSyncEnqueueResponse {
  ok: boolean
  queued: boolean
  phase: WhatsappCatalogSyncPhase | 'blocked' | 'retrying'
  trigger: string
  enqueued: number
  eligible: number
  blocker_code?: string | null
  message_ar?: string | null
  action_ar?: string | null
}

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
  catalog_status?:            string
  merchant_hidden_at?:        string | null
  meta_removed_at?:           string | null
  sync_status?:               string | null
  sync_error_summary?:        string | null
  meta_item_id?:              string | null
  last_sync_attempt_at?:      string | null
  last_synced_at?:            string | null
  retry_allowed?:             boolean
}

export interface ProductDetailResponse {
  product:     StudioProduct
  per_channel: ChannelReadiness[]
  publication: ProductPublicationStatus
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
  | 'meta_access_token_missing'
  | 'catalog_not_found'
  | 'catalog_type_unsupported'
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
    whatsapp_commerce_ready: boolean
  }
  import: {
    status:       string | null
    last_at:      string | null
    last_error:   string | null
    last_report:  {
      scanned?:        number
      created?:        number
      updated?:        number
      skipped_manual?: number
      errors?:         number
      discovery_only?: boolean
    } | null
    token_source: string | null
    discovery_only?: boolean
    products_imported?: boolean
  }
  whatsapp_readiness: {
    ready:                boolean
    checks:               Array<{ key: string; ok: boolean; count?: number; token_source?: string | null }>
    missing_requirements: string[]
  }
  graph_import?: {
    provider:                string | null
    connection_type:         string | null
    meta_catalog_id_present: boolean
    meta_catalog_id:         string
    result_code:             string | null
    action_required:         string | null
    permission_category?:    string | null
    token_selection: {
      token_source:              string | null
      provider:                  string | null
      connection_type:           string | null
      token_tail:                string | null
      token_len:                 number | null
      token_present:             boolean
      platform_token_configured: boolean
      considered:                Array<{ source: string; reason: string }>
    } | null
    preflight?: Record<string, unknown> | null
    products_probe?: Record<string, unknown> | null
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

export interface ManualProductImageUploadResult {
  image_url:    string
  media_id:     string
  content_type: string
  size_bytes:   number
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
  sync_status?:           string | null
  sync_error_summary?:    string | null
  retry_allowed?:         boolean
  publication?:           ProductPublicationStatus
}

export interface MetaSyncPreviewIssue {
  code:       string
  message_ar: string
}

export interface MetaSyncPreviewResponse {
  eligible:          boolean
  dry_run?:          boolean
  would_sync?:       boolean
  product_id?:       number
  source?:           ProductSource | string
  ownership_mode?:   string | null
  meta_catalog_id?:  string | null
  retailer_id?:      string | null
  payload?:          Record<string, unknown>
  fatal_errors?:     MetaSyncPreviewIssue[]
  warnings?:         MetaSyncPreviewIssue[]
  error_code?:       string
  message_ar?:       string
}

export interface MetaSyncConfirmResponse {
  eligible?:         boolean
  ok?:               boolean
  confirm?:          boolean
  product_id?:       number
  source?:           ProductSource | string
  ownership_mode?:   string | null
  retailer_id?:      string | null
  sync_status?:      string | null
  sync_error?:       string | null
  last_synced_at?:   string | null
  variant_created?:  boolean
  error_code?:       string
  message_ar?:       string
  fatal_errors?:     MetaSyncPreviewIssue[]
  push?:             Record<string, unknown>
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
    if (filters?.catalog_visibility)             qs.set('catalog_visibility', filters.catalog_visibility)
    return apiCall<CatalogProductDiagResponse>(`/merchant/catalog/products?${qs.toString()}`)
  },
  hideProduct(id: number): Promise<{ ok: boolean; product_id: number; catalog_status: string }> {
    return apiCall(`/merchant/catalog/products/${id}/hide`, { method: 'POST' })
  },
  restoreProduct(id: number): Promise<{ ok: boolean; product_id: number; catalog_status: string }> {
    return apiCall(`/merchant/catalog/products/${id}/restore`, { method: 'POST' })
  },
  productDetail(id: number): Promise<ProductDetailResponse> {
    return apiCall<ProductDetailResponse>(`/merchant/catalog/products/${id}`)
  },
  updateProduct(id: number, draft: ReadinessPreviewBody): Promise<ProductDetailResponse> {
    return apiCall<ProductDetailResponse>(`/merchant/catalog/products/${id}`, {
      method: 'PATCH',
      body:   JSON.stringify(draft),
    })
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
  wabaLinkStatus(): Promise<WabaCatalogLinkStatus> {
    return apiCall<WabaCatalogLinkStatus>('/merchant/catalog/waba-link-status')
  },
  // Manual product CRUD — Path 3 in the new architecture.
  async uploadManualProductImage(file: File): Promise<ManualProductImageUploadResult> {
    const token = getToken()
    const tenantId = getTenantId()
    const form = new FormData()
    form.append('file', file)

    let res: Response
    try {
      res = await fetch(`${API_BASE}/merchant/catalog/products/manual/upload-image`, {
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
      throw new Error('تعذر رفع الصورة — تحقق من الاتصال بالخادم.')
    }

    if (res.status === 401) {
      logout()
      throw new Error('انتهت الجلسة — سجّل الدخول مجدداً.')
    }

    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : 'upload_failed'
      throw new Error(detail)
    }
    return data as ManualProductImageUploadResult
  },
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
  metaSyncPreview(id: number): Promise<MetaSyncPreviewResponse> {
    return apiCall<MetaSyncPreviewResponse>(
      `/merchant/catalog/products/${id}/meta-sync/preview`,
      { method: 'POST' },
    )
  },
  metaSyncConfirm(id: number): Promise<MetaSyncConfirmResponse> {
    return apiCall<MetaSyncConfirmResponse>(
      `/merchant/catalog/products/${id}/meta-sync/confirm`,
      { method: 'POST', body: JSON.stringify({ confirm: true }) },
    )
  },
  metaSyncRetry(id: number): Promise<MetaSyncConfirmResponse> {
    return apiCall<MetaSyncConfirmResponse>(
      `/merchant/catalog/products/${id}/meta-sync/retry`,
      { method: 'POST' },
    )
  },
  whatsappSyncStatus(): Promise<WhatsappCatalogSyncStatus> {
    return apiCall<WhatsappCatalogSyncStatus>('/merchant/catalog/whatsapp-sync/status')
  },
  enqueueWhatsappSync(force = true): Promise<WhatsappCatalogSyncEnqueueResponse> {
    return apiCall<WhatsappCatalogSyncEnqueueResponse>('/merchant/catalog/whatsapp-sync', {
      method: 'POST',
      body: JSON.stringify({ force }),
    })
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
