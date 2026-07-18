# Tenant 33 Merchant Clone Runbook

## Purpose

Selective **merchant-plane** clone for Tenant 33 acceptance testing on experimental
staging. Copies public store settings, catalog, KB/goal sections, coupons/offers,
and approved merchant templates — **never** customers, orders, conversations,
payments, credentials, or operational history.

## Hard prohibitions

1. **Full DR restore is forbidden** for acceptance cloning. Use this operator only.
2. **Production execution requires separate owner approval.** The code contains a
   production-source confirmation gate but must not be run against production until
   explicitly authorized in audit evidence.
3. **Never clone WhatsApp bindings or tokens.** Target is forced to
   `store_ai_mode=test` with an empty `ai_test_allowed_numbers` allowlist.
4. **Do not use `alembic upgrade head`.** Schema must match closed heads `0088` + `0089`.

## Preconditions

| Check | Requirement |
|-------|-------------|
| Target environment | `RAILWAY_PROJECT_NAME=desirable-growth`, `RAILWAY_ENVIRONMENT_NAME=staging` |
| Target database host | `postgres-staging.railway.internal` only |
| Target tenant | Tenant ID `33`. An absent shell is bootstrapped transactionally; an existing shell must be empty and test-marked |
| Schema | Both source and target at Alembic heads `{0088, 0089}` |
| Source ≠ target | Runtime database identity digests must differ; preserving tenant ID `33` across databases is required |

## Environment variables

```bash
# Required for apply/cleanup (default off)
export NAHLA_TENANT_MERCHANT_CLONE_ENABLED=1

# Source attestation
export NAHLA_CLONE_SOURCE_RAILWAY_PROJECT=desirable-growth
export NAHLA_CLONE_SOURCE_RAILWAY_ENVIRONMENT=production
export NAHLA_CLONE_SOURCE_DATABASE_URL='postgresql+psycopg2://...'

# Target attestation (staging only)
export RAILWAY_PROJECT_NAME=desirable-growth
export RAILWAY_ENVIRONMENT_NAME=staging
export DATABASE_URL='postgresql+psycopg2://...@postgres-staging.railway.internal/...'

# Apply confirmation
export NAHLA_TENANT_MERCHANT_CLONE_APPLY_CONFIRM=APPLY_TENANT_33_MERCHANT_CLONE

# Production source only (blocked until owner approval)
export NAHLA_TENANT_CLONE_PRODUCTION_SOURCE_CONFIRM=CLONE_PRODUCTION_TENANT_33_TO_STAGING_TENANT_33
```

## Commands

### 1. Dry-run (default-safe)

Emits sanitized counts, dependency order, non-PII checksums, transformations list,
denied-domain source counts, `identity_mode=preserve_tenant_id_cross_database`,
and source/target runtime database identity SHA-256 digests. DSNs are never emitted.
**No writes.**

```bash
python scripts/operators/tenant_merchant_clone.py dry-run \
  --source-tenant-id 33 \
  --target-tenant-id 33
```

Archive the `dry_run_digest` from output.

### 2. Apply

Requires archived digest, exact schema heads, and confirmation token.

```bash
export NAHLA_TENANT_MERCHANT_CLONE_ENABLED=1
export NAHLA_TENANT_MERCHANT_CLONE_APPLY_CONFIRM=APPLY_TENANT_33_MERCHANT_CLONE
export NAHLA_TENANT_MERCHANT_CLONE_DRY_RUN_DIGEST='<digest from dry-run>'

python scripts/operators/tenant_merchant_clone.py apply \
  --source-tenant-id 33 \
  --target-tenant-id 33 \
  --dry-run-digest '<digest from dry-run>' \
  --manifest-path ./artifacts/tenant33-clone-manifest.json
```

### 3. Cleanup

Deletes **only** rows recorded in the clone manifest for that `clone_id`.
If apply bootstrapped the Tenant 33 shell, cleanup deletes that shell only after
all clone-created rows are gone and only when its deterministic acceptance marker
still matches. Cleanup never deletes a pre-existing Tenant 33 shell.

```bash
export NAHLA_TENANT_MERCHANT_CLONE_ENABLED=1
export NAHLA_TENANT_MERCHANT_CLONE_CLEANUP_CONFIRM=CLEANUP_TENANT_33_MERCHANT_CLONE

python scripts/operators/tenant_merchant_clone.py cleanup \
  --source-tenant-id 33 \
  --target-tenant-id 33 \
  --clone-id '<clone_id from manifest>' \
  --manifest-path ./artifacts/tenant33-clone-manifest.json
```

## Allowed tables (closed scope)

`tenant_settings`, `commerce_permissions`, `delivery_zones`, `shipping_fees`,
`products`, `product_variants`, `product_groups`, `product_group_items`,
`product_relations`, `product_rankings`, `ai_media_library`,
`merchant_knowledge_sections`, `merchant_knowledge_media`,
`merchant_knowledge_section_products`, `coupons`, `coupon_rules`, `promotions`,
`manual_coupons`, `whatsapp_templates`, `smart_automations`, `automation_rules`,
`merchant_branches`, `branch_contacts`, `branch_escalation_steps`,
`branch_arrival_keywords`, `knowledge_policies`, `merchant_addons`,
`merchant_widgets`, `widget_settings`, `integrations`, `store_knowledge_snapshots`

Plus scalar `tenants` public columns (branding, coupon policy, pickup flags).

## Denied tables (hard deny)

All customer/order/conversation/payment/billing/webhook/campaign/telemetry tables.
See `DENIED_TABLES` in `scripts/operators/tenant_merchant_clone_contract.py`.

## Post-apply verification

1. Target `store_ai_mode=test`, `ai_test_allowed_numbers=[]`
2. No `whatsapp_connections` or `whatsapp_numbers` rows on target
3. Unrelated tenant checksums unchanged
4. Manifest archived for cleanup

## Status for Tenant 33

| Gate | Status |
|------|--------|
| Operator code + tests + CI | READY (after merge) |
| Production source execution | **BLOCKED** — owner approval required |
| Staging apply execution | **BLOCKED** until dry-run digest archived and acceptance window opened |
