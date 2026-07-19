# Merchant store identity contract

## Ownership boundary

Bilingual merchant store identity lives in `TenantSettings.store_settings` (JSONB). This PR owns **persistence, merge rules, settings API exposure, OAuth prefill, and dashboard display**. It does **not** modify AI runtime, Trusted Context, conversation, routing, or payment code.

AI engineering should consume these exact fields when wiring Merchant Profile / Trusted Context in a follow-up PR.

## Canonical JSON fields

| Field | Purpose |
|-------|---------|
| `store_name_ar` | Arabic display name |
| `store_name_en` | English display name |
| `store_name_ar_source` | Provenance for Arabic slot (output-only in API) |
| `store_name_en_source` | Provenance for English slot (output-only in API) |
| `store_name` | Legacy single-language mirror (Arabic then English) |
| `store_name_source` | Optional legacy provenance mirror |

### Source values (persisted)

- `merchant_override` — merchant edited in dashboard; protected from external overwrite
- `external:<provider>` — e.g. `external:salla`, `external:zid`
- empty / unset — slot available for external sync

Resolver-only fallbacks (`tenant_name`, safe generic label) are **never** written as sources.

## Language detection

`detect_store_name_language` assigns a name to **one** slot deterministically:

- Any Arabic Unicode letter → `ar`
- Otherwise → `en`

**No translation.** External sync fills only the detected language. Missing language stays empty until merchant or another external name supplies it.

## Merge rules

### External import (`merge_external_store_name`)

- Normalize: trim, collapse whitespace; ignore empty external names
- Update a language slot only when empty **or** current source is `external:*`
- Never overwrite non-empty `merchant_override` or non-empty unknown-source values
- Update legacy `store_name` only when empty or legacy source is external

### Merchant dashboard (`merge_merchant_store_name_updates`)

- Only keys explicitly present in the PATCH/PUT payload apply
- Unchanged values keep their source
- Changed non-empty value → `merchant_override`
- Cleared value → clear source (external may fill later)
- Recompute legacy `store_name` from Arabic then English when a bilingual field changes

## Display resolution (`resolve_store_name`)

Priority for requested language (`ar` | `en`):

1. Requested-language value with `merchant_override`
2. Requested-language external / available value
3. Other approved bilingual language
4. Legacy `store_name`
5. `tenant_name` (generic tenant record label)
6. Safe generic fallback (`متجر` / `Store` by default)

**Never** use owner email, account username, or integration contact email as store name.

## Integration handoff

| Surface | Behavior |
|---------|----------|
| Salla OAuth | `persist_external_store_name(..., provider="salla")` before production commits |
| Zid OAuth | Centralized in `_save_zid_tokens` with `provider="zid"` |
| Settings API | `store_name_ar` / `store_name_en` writable; `*_source` read-only |
| Dashboard sidebar | Language-aware resolution + `localStorage.nahla_store_name` sync |

## AI team next steps

Read `backend/core/store_identity.py` and pass resolved bilingual names into Trusted Context / Merchant Profile using the same priority rules. Do not duplicate merge logic in prompts.
