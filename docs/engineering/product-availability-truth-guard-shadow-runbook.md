# Product availability truth guard — shadow observation runbook (ARCH-001)

**Experimental staging-only.** Enables `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=shadow`
for **customer-invisible** observation. Shadow mode produces no reply rewrites, no extra
LLM calls, and no outbound provider dispatches.

## Preprod vs post-approval paths

| Stage | Prerequisite | Traffic claim | Unlocks |
|-------|--------------|---------------|---------|
| **Preprod synthetic signoff v2** | `ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2` HMAC bundle | `synthetic_probes_only` | Real-channel acceptance prep gates only |
| **Post-approval canonical shadow** | v2 signoff + explicit approval | Limited allowlisted canary (organic allowed) | Shadow telemetry review toward enforce |
| **Enforce** | Real conflict telemetry / accuracy review | Production-grade evidence | `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=enforce` (not approved by default) |

The retired **48-hour zero-traffic slot** is superseded by v2 lifecycle signoff. Legacy v1
artifacts remain readable but cannot unlock preprod gates.

See: `docs/engineering/product-availability-preprod-synthetic-signoff-v2-evidence-schema.md`

## Guard invocation sites (no double-count)

| Path | When | `invocation_site` |
|------|------|-------------------|
| Brain pipeline | `MERCHANT_BRAIN` active tenants | `pipeline` |
| WhatsApp webhook | Legacy path only (`not _brain_active`) | `webhook` |

Brain and legacy paths are **mutually exclusive** per turn — do not sum pipeline and
webhook counts for the same conversation turn.

## Preconditions

1. Staging identity: `RAILWAY_PROJECT_NAME=desirable-growth`,
   `RAILWAY_ENVIRONMENT_NAME=staging`.
2. Pinned deploy revision recorded **before** enabling shadow mode (post-approval path only).
3. Conditional-coupon compose/canary flags remain **off**.
4. `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE` is `off` (default-off verified).

## Operator commands — shadow observation (post-approval)

```bash
# Default-off gate (no shadow)
python -m scripts.operators.product_availability_truth_guard_shadow_observation default-off

# Synthetic matrix (process-scoped shadow; sets env only inside probe)
python -m scripts.operators.product_availability_truth_guard_shadow_observation matrix

# Closed hash manifest for the exact target checkout
python -m scripts.operators.product_availability_truth_guard_shadow_observation artifact-manifest

# Runtime-bound matrix (must run inside deployed /app with persistent shadow)
python -m scripts.operators.product_availability_truth_guard_shadow_observation \
  runtime-matrix <PINNED_SHA> <EXPECTED_MANIFEST_DIGEST>

# Teardown helper (prints Railway rollback command)
python -m scripts.operators.product_availability_truth_guard_shadow_observation teardown
```

## Operator commands — preprod synthetic signoff v2

Production signoff ingests **externally generated** runtime-bound phase artifacts.
CI `contract-self-test` validates contract logic only and produces ineligible bundles.

```bash
# After each real lifecycle action inside isolated nahla-arch001-shadow /app:
#   save phase artifact JSON (baseline.json, container_restart.json, ...)

# Assemble production bundle from phase artifacts + teardown + negative controls
export NAHLA_ARCH001_PREPROD_PINNED_REVISION=<SHA>
export NAHLA_ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST=<digest>
export NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_NAME=nahla-arch001-shadow
export NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_ID=<uuid>
export NAHLA_ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID=<post-redeploy-uuid>
export NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY=<min-32-byte-secret>

python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  assemble-bundle \
  --phase-dir ./phase-artifacts \
  --teardown ./teardown-proof.json \
  --negative-controls-dir ./negative-controls \
  --output docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2-<date>.json

# CI contract self-test only (eligible_for_signoff=false)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 contract-self-test
```

Archive signed production output to
`docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2-<date>.json`.

**Do not use** removed `full-probe` / in-process multi-phase labeling for production signoff.

## Enable shadow on staging (post-approval only)

```bash
railway variables --environment staging \
  --set "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=shadow" \
  --service nahla-saas
```

Record deployment ID and pinned SHA in post-approval shadow evidence — not in the v2
preprod bundle (which claims synthetic probes only).

## Recurring runtime-bound poll (post-approval parent agent loop)

```bash
EXPECTED_MANIFEST_DIGEST="$(
  python -m scripts.operators.product_availability_truth_guard_shadow_observation \
    artifact-manifest |
  python -c 'import json,sys; print(json.load(sys.stdin)["manifest_digest"])'
)"

railway ssh --environment staging --service nahla-saas \
  python -m scripts.operators.product_availability_truth_guard_shadow_observation \
  runtime-matrix <PINNED_SHA> "$EXPECTED_MANIFEST_DIGEST"
```

Acceptance per poll:
- `ok=true`
- `guards.customer_text_changed_count=0`
- `guards.additional_llm_calls=0`
- `guards.outbound_provider_calls=0`
- `guards.duplicate_invocation_count=0`

## Immediate rollback

```bash
railway variables --environment staging \
  --set "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=off" \
  --service nahla-saas
```

If any poll reports `customer_text_changed_count > 0` or outbound/LLM calls, set mode
to `off` immediately and mark **BLOCK**.

## Closed telemetry schema

Runtime shadow records use `product_availability_shadow_v1` (log prefix
`[PRODUCT_AVAILABILITY_SHADOW_OBSERVATION]`). Fields: `tenant_id`, `turn_fingerprint`,
`invocation_site`, `evidence_state`, `guard_action`, `would_rewrite`, `reason_code`,
`customer_text_changed`, `additional_llm_calls`, `guard_duration_ms`,
`duplicate_invocation`. **No customer text, phones, or names.**

## CI

- `backend/tests/test_product_availability_truth_guard_shadow_observation_probe.py`
- `backend/tests/test_product_availability_preprod_synthetic_signoff_v2_probe.py`
