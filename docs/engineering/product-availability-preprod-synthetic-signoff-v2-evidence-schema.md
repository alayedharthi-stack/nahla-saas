# ARCH-001 preprod synthetic signoff v2 — evidence schema

Initiative: `ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2`  
Bundle schema: `product_availability_preprod_synthetic_signoff_v2`

## Purpose

Replace the pre-production **48-hour zero-traffic observation window** prerequisite
with a **phase/lifecycle-based synthetic matrix signoff**. This artifact proves
deterministic guard behavior under controlled probes only.

| Claim | Allowed in v2 preprod signoff? |
|-------|-------------------------------|
| Synthetic 7/7 matrix PASS | Yes |
| Zero customer text mutation | Yes |
| Zero added LLM/provider calls | Yes |
| Organic / real-channel traffic observed | **No** (`traffic_claim=synthetic_probes_only`) |
| Post-approval canonical shadow canary | **No** — remains `pending` |
| Enforce eligibility | **No** — remains `pending` |

Legacy v1 bundles (`product_availability_shadow_staging_signoff_v1`) remain
**historically readable** but are **not sufficient** to unlock preprod gates.

## Artifact location

- Operator output: `docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2-<date>.json`
- Gate env ref: `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT`
- HMAC key env ref: `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY`

## Bundle shape (unsigned fields before `signature`)

| Field | Type | Description |
|-------|------|-------------|
| `bundle_schema_version` | string | `product_availability_preprod_synthetic_signoff_v2` |
| `initiative_id` | string | `ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2` |
| `traffic_claim` | string | Must be exactly `synthetic_probes_only` |
| `identity_binding` | object | Pinned SHA, manifest digest, service/deployment/image binding |
| `lifecycle_phases` | array[6] | baseline → container_restart → fresh_pinned_redeploy → repeat_matrix_1..3 |
| `negative_controls` | object | Four BLOCK controls with expected failure codes |
| `stable_counter_reference` | object | Baseline counters reused across lifecycle phases |
| `post_approval` | object | `canonical_shadow_canary=pending`, `enforce_eligibility=pending` |
| `superseded_invalid_windows` | array | Retired 48h windows marked inactive |
| `teardown_proof` | object | Shadow teardown metadata (preprod does not require live shadow) |
| `signed_at_utc` | string | ISO-8601 UTC |
| `signature` | string | `hmac-sha256:<digest>` over canonical JSON without signature |

### `identity_binding`

| Key | Requirement |
|-----|-------------|
| `pinned_target_revision` | 7–40 char git SHA |
| `manifest_digest` | 64-char SHA-256 over closed runtime artifact manifest |
| `service_name` | `nahla-saas` |
| `service_id` | `686b36c5-a926-4e58-912a-5e9d13fbc2e7` |
| `deployment_id` | Railway deployment UUID |
| `image_digest` | 64-char SHA-256 or `absent` |

### Lifecycle phase row

| Key | Requirement |
|-----|-------------|
| `phase` | One of six closed lifecycle phases |
| `ok` | `true` |
| `matrix` | 7/7 synthetic matrix with zero safety violations |
| `stable_counters` | Must match baseline reference |
| `dependency_fault` | Optional; if present `status=skipped_not_supported` + `residual_risk` |

### Negative controls (must BLOCK)

| Control ID | Expected code |
|------------|---------------|
| `wrong_manifest` | `artifact_manifest_mismatch` |
| `wrong_revision` | `runtime_revision_mismatch` |
| `outside_app` | `runtime_execution_required` |
| `enforce_enabled` | `enforce_mode_enabled` |

## Operator commands

```bash
# Single lifecycle phase matrix (process-scoped shadow)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  lifecycle-phase baseline

# Negative controls only
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  negative-controls

# CI-safe full probe (build + sign + verify)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 full-probe

# Verify archived artifact
NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY=<secret> \
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  verify-bundle docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2.json

# Read legacy v1 (historical only)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  verify-legacy-v1 docs/engineering/staging-evidence/product-availability-shadow-baseline-2026-07-18.json
```

## Rollback / invalidation

1. **Supersede bundle:** add the window/bundle ID to `superseded_invalid_windows` with
   `active=false`, reason, and `superseded_at_utc`. Re-sign a replacement bundle.
2. **Invalidate gate consumption:** unset `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT`
   from any downstream operator env. Real-channel acceptance and staging consolidation
   gates fail closed immediately.
3. **Do not rewrite history:** leave v1 JSON artifacts in `staging-evidence/`; mark them
   superseded in the replacement v2 bundle only.
4. **Post-approval path unchanged:** canonical shadow during limited allowlisted canary and
   enforce eligibility review remain separate operator workflows after v2 preprod signoff.

## Gate consumption

| Consumer | Behavior |
|----------|----------|
| Real-channel acceptance | `gate_arch001_shadow_signoff` verifies v2 HMAC artifact only |
| Staging config consolidation | `gate_arch001_teardown_proof` requires v2 artifact + teardown proof ref |
| Enforce rollout | **Not unlocked** by v2; requires real conflict telemetry review |

`NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM=true` alone is **not sufficient**.

## CI

`backend/tests/test_product_availability_preprod_synthetic_signoff_v2_probe.py`
