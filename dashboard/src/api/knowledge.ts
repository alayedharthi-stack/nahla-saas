// ── Smart Store Knowledge Hub API client ─────────────────────────────────
// Wraps the merchant-facing CRUD endpoints exposed by
// `backend/routers/knowledge.py`. The Knowledge Hub is the structured
// successor to the legacy single-textarea `manual_knowledge_base` field
// — sections are grouped into six dashboard buckets and can attach
// rows from the existing `AIMediaItem` library through a many-to-many
// link table.
//
// Source-of-truth precedence is enforced server-side in the prompt
// overlay: e-commerce platform data (Salla / Zid / Shopify) wins on
// price, inventory, product names, links and primary images; sections
// here cover everything else (story, tone, hours, branches, payment +
// shipping policies, return rules, product usage tips, FAQ, …).

import { apiCall } from './client'
import type { AIMediaItem, AIMediaType } from './intelligenceLibraries'

// ── Section kinds registry (mirrors services/knowledge_section_kinds.py) ──

export interface SectionKindMeta {
  kind: string
  group: number
  label_ar: string
  placeholder_ar: string
  is_product_bound: boolean
}

export interface KnowledgeGroup {
  id: number
  label_ar: string
}

export interface SectionKindsResponse {
  groups: KnowledgeGroup[]
  kinds: SectionKindMeta[]
  link_roles: string[]
}

// ── Sections + media link rows ────────────────────────────────────────────

export type LinkRole =
  | 'primary'
  | 'evidence'
  | 'barcode'
  | 'tutorial_video'
  | 'recipe_video'
  | 'policy_pdf'
  | 'certificate'
  | 'map'

export interface MediaLinkRow {
  id: number
  section_id: number
  media_id: number
  link_role: LinkRole
  created_at: string | null
  // Inlined for thumbnail rendering — backend serializes the bare
  // minimum from the linked AIMediaItem so the dashboard avoids a
  // second round-trip per card.
  media: Pick<
    AIMediaItem,
    'id' | 'title' | 'media_type' | 'file_url' | 'thumbnail_url' | 'media_key' | 'is_active'
  > | null
}

export interface KnowledgeSection {
  id: number
  tenant_id: number
  kind: string
  group: number
  title: string | null
  body: string
  metadata_json: Record<string, unknown> | null
  priority: number
  is_active: boolean
  source: 'manual' | 'ai_classified' | 'imported'
  ai_status: 'approved' | 'pending' | 'rejected'
  classification_confidence: number | null
  conflicts_json: Record<string, unknown> | null
  created_at: string | null
  updated_at: string | null
  media_links: MediaLinkRow[]
  product_links?: ProductLinkRow[]
}

export interface SectionInput {
  kind: string
  title?: string | null
  body?: string
  metadata_json?: Record<string, unknown> | null
  priority?: number
  is_active?: boolean
}

export interface MediaLinkInput {
  media_id: number
  link_role?: LinkRole
}

// ── Legacy import (one-shot migration) ────────────────────────────────────

export interface LegacyPreviewBlock {
  kind: string
  title: string
  body: string
}

export interface LegacyKnowledgeBaseResponse {
  text: string
  char_count: number
  preview: LegacyPreviewBlock[]
}

export interface MigrateResponse {
  created: number
  blocks: { id?: number; kind: string }[]
  cleared_legacy: boolean
  dry_run?: boolean
}

// ── Phase 2 — AI classifier ("تنسيق ودمج بالذكاء") ──────────────────────

export type ProposedOpType = 'create' | 'update' | 'merge' | 'link_media' | 'link_product'

export interface ProposedOp {
  op_id: string
  op: ProposedOpType
  kind: string
  title: string | null
  body: string
  metadata: Record<string, unknown>
  target_section_id: number | string | null
  link_role: LinkRole | null
  media_id: number | null
  // Phase 3.2 — auto product-match fields (only present on link_product ops)
  product_id?: number | null
  confidence?: number | null
  rationale: string
}

export type ConflictKind =
  | 'platform_price'
  | 'platform_stock'
  | 'platform_name'
  | 'platform_url'
  | 'existing_section'

export interface DraftConflict {
  with_section_id: number | null
  with_field: string
  kind: ConflictKind | string
  explanation: string
}

export interface DraftProposal {
  proposed_ops: ProposedOp[]
  confidence: number
  model?: string | null
  fallback_used?: boolean
  fallback_reason?: string | null
}

export interface KnowledgeDraft {
  id: number
  tenant_id: number
  raw_text: string
  attached_media_ids: number[]
  status: 'pending' | 'approved' | 'rejected' | 'failed'
  proposal: DraftProposal
  conflicts: DraftConflict[]
  created_at: string | null
  decided_at: string | null
  applied_op_ids: string[]
}

export interface FormatQuickUpdateRequest {
  raw_text: string
  attached_media_ids?: number[]
}

// ── Phase 3 — section ⇄ product links ──────────────────────────────────

export interface ProductLite {
  id: number
  title: string
  external_id: string | null
  sku: string | null
  in_stock: boolean
}

export interface ProductLinkRow {
  id: number
  product_id: number
  source: 'manual' | 'ai_fuzzy_match' | 'imported'
  confidence: number | null
  created_at: string | null
  product: ProductLite | null
}

export interface ProductLinkInput {
  product_id: number
  source?: 'manual' | 'ai_fuzzy_match' | 'imported'
  confidence?: number | null
}

// ── API surface ───────────────────────────────────────────────────────────

export const knowledgeApi = {
  // Registry — call once on page mount and keep client-side; the
  // server returns a stable closed set so caching is safe for the
  // duration of the SPA session.
  getSectionKinds() {
    return apiCall<SectionKindsResponse>('/knowledge/section-kinds')
  },

  // ── Sections ───────────────────────────────────────────────────────────
  listSections(opts?: { onlyActive?: boolean; kind?: string; group?: number }) {
    const params = new URLSearchParams()
    if (opts?.onlyActive) params.set('only_active', 'true')
    if (opts?.kind) params.set('kind', opts.kind)
    if (typeof opts?.group === 'number') params.set('group', String(opts.group))
    const qs = params.toString()
    return apiCall<{ items: KnowledgeSection[] }>(
      `/knowledge/sections${qs ? `?${qs}` : ''}`,
    )
  },

  createSection(payload: SectionInput) {
    return apiCall<KnowledgeSection>('/knowledge/sections', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  updateSection(id: number, payload: Partial<SectionInput>) {
    return apiCall<KnowledgeSection>(`/knowledge/sections/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },

  toggleSection(id: number) {
    return apiCall<KnowledgeSection>(`/knowledge/sections/${id}/toggle`, {
      method: 'POST',
    })
  },

  deleteSection(id: number) {
    return apiCall<{ deleted: boolean; id: number }>(
      `/knowledge/sections/${id}`,
      { method: 'DELETE' },
    )
  },

  // ── Media linking (idempotent server-side on (section, media, role)) ──
  linkMedia(sectionId: number, payload: MediaLinkInput) {
    return apiCall<MediaLinkRow>(`/knowledge/sections/${sectionId}/media`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  unlinkMedia(sectionId: number, linkId: number) {
    return apiCall<{ deleted: boolean; id: number }>(
      `/knowledge/sections/${sectionId}/media/${linkId}`,
      { method: 'DELETE' },
    )
  },

  // ── Legacy free-form text import ──────────────────────────────────────
  // The dashboard renders a one-time banner offering to lift the old
  // `ai_settings.manual_knowledge_base` text into structured rows. We
  // call /legacy-knowledge-base first to render the diff, then
  // /migrate-from-legacy with `dry_run=false, clear_legacy=true` to
  // commit.
  getLegacyKnowledgeBase() {
    return apiCall<LegacyKnowledgeBaseResponse>('/knowledge/legacy-knowledge-base')
  },

  migrateFromLegacy(opts?: { clearLegacy?: boolean; dryRun?: boolean }) {
    return apiCall<MigrateResponse>(
      '/knowledge/sections/migrate-from-legacy',
      {
        method: 'POST',
        body: JSON.stringify({
          clear_legacy: !!opts?.clearLegacy,
          dry_run: !!opts?.dryRun,
        }),
      },
    )
  },

  // ── Phase 2 — AI classifier ──────────────────────────────────────────
  formatQuickUpdate(payload: FormatQuickUpdateRequest) {
    return apiCall<KnowledgeDraft>('/knowledge/quick-update/format', {
      method: 'POST',
      body: JSON.stringify({
        raw_text: payload.raw_text,
        attached_media_ids: payload.attached_media_ids || [],
      }),
    })
  },

  listDrafts(status?: string) {
    const qs = status ? `?status=${encodeURIComponent(status)}` : ''
    return apiCall<{ items: KnowledgeDraft[] }>(`/knowledge/drafts${qs}`)
  },

  approveDraft(id: number, opIds?: string[]) {
    return apiCall<KnowledgeDraft>(`/knowledge/drafts/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ op_ids: opIds && opIds.length ? opIds : null }),
    })
  },

  rejectDraft(id: number) {
    return apiCall<KnowledgeDraft>(`/knowledge/drafts/${id}/reject`, {
      method: 'POST',
    })
  },

  // ── Phase 3 — section ⇄ product links ────────────────────────────────
  listProductLinks(sectionId: number) {
    return apiCall<{ items: ProductLinkRow[] }>(
      `/knowledge/sections/${sectionId}/products`,
    )
  },

  linkProduct(sectionId: number, payload: ProductLinkInput) {
    return apiCall<ProductLinkRow>(
      `/knowledge/sections/${sectionId}/products`,
      {
        method: 'POST',
        body: JSON.stringify({
          product_id: payload.product_id,
          source: payload.source || 'manual',
          confidence: payload.confidence ?? null,
        }),
      },
    )
  },

  unlinkProduct(sectionId: number, linkId: number) {
    return apiCall<{ status: string }>(
      `/knowledge/sections/${sectionId}/products/${linkId}`,
      { method: 'DELETE' },
    )
  },

  searchProducts(query: string, limit = 20) {
    const qs = new URLSearchParams({ q: query.trim(), limit: String(limit) })
    return apiCall<{ items: ProductLite[] }>(`/knowledge/products/search?${qs}`)
  },

  // ── KB-Improve V1 — proactive improvement suggestions ───────────────
  // The merchant clicks "اقتراحات تحسين من نحلة ✨" → server runs the
  // deterministic auditor (+ optional GPT polish), returns up to 5
  // preview-only suggestions. Approval funnels through
  // ``promoteImprovementSuggestion`` which creates a
  // ``MerchantKnowledgeDraft`` row — the existing draft preview drawer
  // handles per-op approve / reject after that.
  getImprovementSuggestions(opts?: { polish?: boolean; max?: number }) {
    const qs = new URLSearchParams()
    if (opts?.polish === false) qs.set('polish', 'false')
    if (opts?.max) qs.set('max_suggestions', String(opts.max))
    const suffix = qs.toString() ? `?${qs}` : ''
    return apiCall<ImprovementSuggestionsResponse>(
      `/knowledge/improvement-suggestions${suffix}`,
    )
  },

  promoteImprovementSuggestion(suggestion: ImprovementSuggestion, overrideBody?: string) {
    return apiCall<KnowledgeDraft>(
      '/knowledge/improvement-suggestions/promote',
      {
        method: 'POST',
        body: JSON.stringify({
          suggestion_id: suggestion.id,
          type: suggestion.type,
          target_kind: suggestion.target_kind,
          title: suggestion.title,
          reason: suggestion.reason,
          expected_impact: suggestion.expected_impact,
          proposed_body: overrideBody && overrideBody.trim()
            ? overrideBody
            : suggestion.proposed_body,
          severity: suggestion.severity,
          confidence: suggestion.confidence,
          fingerprint: suggestion.fingerprint,
          related_section_ids: suggestion.related_section_ids,
        }),
      },
    )
  },

  // KB-Improve V1.1 — record a rejection. Server stores it inside
  // ``TenantSettings.ai_settings.kb_improvement_state`` with a 7-day
  // TTL so the same fingerprint won't surface again in subsequent
  // ``/improvement-suggestions`` analyses.
  dismissImprovementSuggestion(suggestion: ImprovementSuggestion, ttlDays = 7) {
    return apiCall<{
      fingerprint: string
      ttl_days: number
      active_dismissed_count: number
    }>('/knowledge/improvement-suggestions/dismiss', {
      method: 'POST',
      body: JSON.stringify({
        fingerprint: suggestion.fingerprint,
        type: suggestion.type,
        target_kind: suggestion.target_kind,
        ttl_days: ttlDays,
      }),
    })
  },
}


// ── KB-Improve V1 — types ──────────────────────────────────────────────────


export type ImprovementSuggestionType =
  | 'missing_required_knowledge'
  | 'weak_section'
  | 'semantic_contamination'
  | 'duplicate_merge'
  | 'missing_media'
  | 'product_knowledge_gap'
  | 'behavior_tone'
  | 'compliance'

export type ImprovementSeverity = 'high' | 'medium' | 'low'

export interface ImprovementSuggestion {
  id: string
  type: ImprovementSuggestionType
  severity: ImprovementSeverity
  title: string
  reason: string
  expected_impact: string
  target_kind: string
  proposed_body: string
  requires_media: boolean
  confidence: number
  related_section_ids: number[]
  // KB-Improve V1.1 — stable hash used by reject/approve suppression.
  fingerprint: string
}

export interface ImprovementSuggestionsResponse {
  suggestions: ImprovementSuggestion[]
  platform_connected: boolean
  platform: string | null
  scanned_sections: number
  model: string
}

// Re-export the AIMediaItem-related types from intelligenceLibraries so
// the new KnowledgeBase page can pick from the existing media library
// without two import sites.
export type { AIMediaItem, AIMediaType }

