# Knowledge Base Flow Audit

> **Status:** PR-1 — Audit & Inventory (read-only reference)  
> **Last updated:** 2026-06-13  
> **Doctrine alignment:** [AGENTS.md](../../AGENTS.md) — operational facts are deterministic; personality is non-deterministic; platform-wide solutions only.

This document is the canonical map of how merchant knowledge is stored, edited, classified, and consumed by the Brain runtime. **No production behavior should change based on this doc alone.**

---

## 1. Where knowledge is stored

### 1.1 Legacy layer

| Location | Field | Format |
|----------|-------|--------|
| `tenant_settings.ai_settings` (JSONB) | `manual_knowledge_base` | Single free-form text blob |

- Defaults: `backend/core/tenant.py` → `DEFAULT_AI["manual_knowledge_base"] = ""`
- Still read at runtime when **no** structured sections exist, or for platform-intent excerpt paths
- One-shot migration: `POST /knowledge/sections/migrate-from-legacy` (`backend/routers/knowledge.py`)

### 1.2 Structured layer (Smart Store Knowledge Hub)

| Table | Purpose |
|-------|---------|
| `merchant_knowledge_sections` | One row per fact / policy / note |
| `merchant_knowledge_media` | M2M link to `ai_media_library` with `link_role` |
| `merchant_knowledge_section_products` | Product-scoped sections (Phase 3) |
| `merchant_knowledge_drafts` | GPT classifier proposals before merchant approval |

**Migrations:** `0067_merchant_knowledge.py`, `0068_merchant_knowledge_drafts.py`, `0069_section_products.py`, `0073_kb_section_deleted_at.py`

**ORM:** `database/models.py` → `MerchantKnowledgeSection`, `MerchantKnowledgeMedia`, `MerchantKnowledgeSectionProduct`, `MerchantKnowledgeDraft`

### 1.3 Related but separate

| Store | Used for |
|-------|----------|
| `knowledge_policies` | Legacy ai-engine / orchestrator policy guard — **not** the dashboard KB hub |
| `ai_settings.owner_instructions`, `coupon_rules`, `escalation_rules` | High-priority style/policy overlay — separate from structured facts |

---

## 2. Dashboard UI surfaces

| Surface | Path / file | Role |
|---------|-------------|------|
| **Knowledge Hub (primary)** | `/knowledge-base` → `dashboard/src/pages/KnowledgeBase.tsx` | Section CRUD, quick update, drafts, search, improvement suggestions |
| **API client** | `dashboard/src/api/knowledge.ts` | Wraps `backend/routers/knowledge.py` |
| **Nahla Intelligence settings** | `/intelligence` → `AISettingsPanel` in `dashboard/src/pages/Intelligence.tsx` | Personality, owner instructions, coupon/escalation text fields |
| **Intelligence Libraries** | `/intelligence/libraries` | Media pool reused by KB media linking |

### 2.1 UI gaps (pre-redesign baseline)

| Gap | Detail |
|-----|--------|
| **Group 7 not visible** | Behavioral kinds (`forbidden_phrases`, `escalation_rules`, …) exist in registry but main page only rendered groups 1–5 + media summary |
| **`metadata_json` hidden** | Column exists; API accepts it; `SectionEditor` did not expose structured fields |
| **Repair preview unwired** | `GET /knowledge/repair/preview` implemented (`repair_advisor.py`) but not called from dashboard |
| **Quick update UX** | Large card; "حفظ كملاحظة" created `quick_update` rows without clear review path |
| **Search / suggestions layout** | Functional but visually crowded; limited filter set |
| **Mixed instructions in Intelligence** | `owner_instructions`, `coupon_rules`, `escalation_rules` can contain operational facts that belong in KB or catalog |

---

## 3. How the Brain reads knowledge

### 3.1 Pipeline (no embeddings)

```
WhatsApp webhook
  → backend/modules/ai/brain/pipeline.py
      build_structured_facts_block(db, tenant_id, active_product_ids)
      build_behavioral_overlay_block(db, tenant_id)
  → merchant_context.structured_facts_block
  → merchant_context.structured_behavior_block
  → backend/modules/ai/brain/compose/prompt_builder.py
      Block 2: high_priority_layer (behavior)
      Block 3: facts (structured or legacy manual_knowledge_base)
```

**Overlay builder:** `backend/modules/ai/prompts/tenant_overlay.py`

### 3.2 Query filters (AI-visible)

From `backend/core/knowledge.py`:

```text
AI retrieval: deleted_at IS NULL AND is_active = true
```

Additional runtime filters in `build_structured_facts_block`:

- Drop **behavioral kinds** (group 7) from facts — routed to `build_behavioral_overlay_block`
- Skip group 6 (linked media bucket) in facts text
- Product-scoped sections filtered by `active_product_ids` from conversation context
- Catalog-inactive product links dropped via `section_has_catalog_active_product`

### 3.3 What is injected per section

`_render_section_block()` emits:

- `title`, `body`
- `[MEDIA_KEY:slug]` for linked active media with `media_key` set
- Product scope tag `(منتجات: …)` when product links exist

**`metadata_json` is not rendered in the facts block** except via dedicated consumers (goal retrieval, staff contact, arrival policy, payment beneficiary extraction).

### 3.4 Precedence

Platform e-commerce data (Salla / Zid / Shopify) wins on price, stock, variants, product URLs, primary images. KB carries policies, story, usage tips, FAQ, etc. Enforced in overlay precedence note + guards — not in this doc's scope.

### 3.5 Special retrieval paths (not vector)

| Path | Module | Trigger |
|------|--------|---------|
| Goal-based recommendations | `goal_retrieval.py` | `kind=goal_based_recommendation` + `metadata_json.goal_tags` |
| Platform KB excerpt | `knowledge_platform_slice.py` | Platform-intent turns; reads raw `manual_knowledge_base` |
| Staff / arrival contact | `staff_contact_fallback_v0.py`, `arrival_contact_policy.py` | Heuristics on section body + metadata |

---

## 4. GPT usage in KB surfaces

| Surface | GPT? | Module | Model env |
|---------|------|--------|-----------|
| Quick update classification | **Yes** | `backend/modules/ai/knowledge/classifier.py` | `NAHLA_KB_CLASSIFIER_MODEL` → default `gpt-4.1`, fallback `OPENAI_MODEL` |
| Draft preview | **Yes** (via classifier) | `POST /knowledge/quick-update/format` | Same |
| Improvement advisor — audit | **No** | `improvement_advisor.audit()` | Pure Python |
| Improvement advisor — polish | **Optional Yes** | `improvement_advisor.polish_with_gpt()` | Skipped if `OPENAI_API_KEY` unset |
| Repair preview | **No** | `repair_advisor.py` | Heuristic only |
| Product matcher (draft approval) | **No** | `product_matcher.py` | Token overlap |
| Runtime Brain reply | Claude (compose) | `responder.py` | Reads injected KB — does not re-classify |

**Classifier fallback:** When API key missing → single `quick_update` op (deterministic).

---

## 5. Embeddings / vector retrieval

**Confirmed: none for merchant KB today.**

- Full section load + filter + prompt injection
- `improvement_advisor.py` explicitly: "No embeddings, no vector store, no retrieval rewrite"
- `whatsapp_webhook.py` dedup uses lexical overlap, not embeddings

---

## 6. Field reference

### `merchant_knowledge_sections`

| Column | Notes |
|--------|-------|
| `kind` | Registry in `services/knowledge_section_kinds.py` |
| `title`, `body` | Primary merchant-facing content |
| `metadata_json` | JSONB — goal tags, staff roles, beneficiary, arrival flags, … |
| `priority`, `is_active` | Ordering + AI visibility |
| `source` | `manual` \| `ai_classified` \| `imported` |
| `ai_status`, `classification_confidence`, `conflicts_json` | Draft lifecycle |
| `deleted_at` | Soft delete |

### Dashboard groups (registry)

| Group | Label (AR) | Prompt channel |
|-------|------------|----------------|
| 1 | التحديثات السريعة | Facts |
| 2 | معلومات المتجر | Facts |
| 3 | سياسات البيع | Facts |
| 4 | سياسات الشحن | Facts |
| 5 | معلومات المنتجات | Facts |
| 6 | مكتبة الوسائط | Navigational (skipped in facts block) |
| 7 | سلوك المساعد | High-priority behavior overlay |

---

## 7. API endpoints (knowledge router)

| Method | Path | Mutates? |
|--------|------|----------|
| GET | `/knowledge/section-kinds` | No |
| GET | `/knowledge/sections` | No |
| GET | `/knowledge/sections/search` | No |
| POST/PATCH/DELETE | `/knowledge/sections…` | Yes |
| POST | `/knowledge/quick-update/format` | Yes (creates draft) |
| GET | `/knowledge/drafts` | No |
| POST | `/knowledge/drafts/{id}/approve` | Yes |
| GET | `/knowledge/repair/preview` | No |
| GET | `/knowledge/improvement-suggestions` | No |
| POST | `/knowledge/improvement-suggestions/promote` | Yes (creates draft) |

---

## 8. Pre / post checklist (UI redesign PRs)

Use before merging any KB UI PR that must **not** change runtime AI behavior.

### Before merge

- [ ] Record baseline: tenant with structured sections → log or test `build_structured_facts_block` output hash / section count
- [ ] Confirm no edits to `tenant_overlay.py`, `prompt_builder.py`, `pipeline.py`, `classifier.py` prompts
- [ ] Confirm no DB migrations in UI-only PR
- [ ] Dashboard `/knowledge-base` loads without console errors

### After merge

- [ ] Page opens; CRUD uses same API payloads (no new required fields)
- [ ] Search returns results; active/inactive toggle works
- [ ] Quick update → draft path still preview-only (no auto-apply)
- [ ] Improvement / repair suggestions require explicit approve
- [ ] **Facts block byte-identical** for same tenant DB state (automated test: `tests/test_tenant_overlay_knowledge_base.py`, `backend/tests/test_kb_search_and_visibility.py`)
- [ ] Group 7 sections visible in UI; injection path unchanged (behavior still via `build_behavioral_overlay_block`)
- [ ] No new hardcoded tenant / merchant / product names in dashboard code

### Regression tests to run

```bash
# From repo root — adjust for your test runner
pytest tests/test_tenant_overlay_knowledge_base.py backend/tests/test_kb_search_and_visibility.py backend/tests/test_knowledge_phase1.py -q
```

---

## 9. Redesign PR sequence (planned)

| PR | Branch | Scope | Runtime touch |
|----|--------|-------|---------------|
| PR-1 | `audit/knowledge-base-flow-map` | This document | None |
| PR-2 | `feat/kb-admin-ui-organization` | Dashboard layout, buckets, preview UX | None |
| PR-3 | Metadata editors (future) | Per-kind `metadata_json` UI | None until consumers wired |
| PR-4 | Preview & warnings polish | Repair/improvement UX | None |
| PR-5 | Structured escalation contacts | `merchant_escalation_contacts` (future) | New consumers only with evidence guards |

---

## 10. Doctrine reminders for redesign

**Operational (deterministic):** prices, stock, shipping, payment, location, contact numbers, escalation delivery evidence.

**Personality (non-deterministic):** tone, greetings, warmth — persona compose, not template pools.

**Forbidden in KB redesign:** canned replies, prompt patches to hide operational gaps, auto-apply AI suggestions, tenant-specific hardcoding, deleting legacy data without migration.
