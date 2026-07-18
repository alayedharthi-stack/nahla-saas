# Product availability truth guard — shadow observation runbook (ARCH-001)

**Experimental staging-only.** Enables `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=shadow`
for a **48-hour bounded observation window** with synthetic probes. Shadow mode is
**customer-invisible**: no reply rewrites, no extra LLM calls, no outbound provider
dispatches.

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
2. Pinned deploy revision recorded **before** enabling shadow mode.
3. Conditional-coupon compose/canary flags remain **off**.
4. `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE` is `off` (default-off verified).

## Operator commands

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

# Full baseline (default-off + matrix + 48h window metadata)
python -m scripts.operators.product_availability_truth_guard_shadow_observation full-probe <PINNED_SHA>

# Teardown helper (prints Railway rollback command)
python -m scripts.operators.product_availability_truth_guard_shadow_observation teardown
```

## Enable shadow on staging (persistent window)

```bash
railway variables --environment staging \
  --set "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=shadow" \
  --service nahla-saas
```

Record deployment ID and pinned SHA in the evidence bundle.

## Recurring runtime-bound poll (parent agent loop)

```bash
# 1. From a clean checkout of the exact pinned target:
EXPECTED_MANIFEST_DIGEST="$(
  python -m scripts.operators.product_availability_truth_guard_shadow_observation \
    artifact-manifest |
  python -c 'import json,sys; print(json.load(sys.stdin)["manifest_digest"])'
)"

# 2. Execute the matrix inside the active Railway /app image:
railway ssh --environment staging --service nahla-saas \
  python -m scripts.operators.product_availability_truth_guard_shadow_observation \
  runtime-matrix <PINNED_SHA> "$EXPECTED_MANIFEST_DIGEST"
```

Archive only the sanitized runtime report after independently binding it to the
active Railway deployment ID and image digest. A local `matrix` result is useful
synthetic contract evidence, but **is not staging runtime evidence**.

**Evidence accumulation:** `docs/engineering/staging-evidence/product-availability-shadow-*.json`

Acceptance per poll:
- `ok=true`
- `guards.customer_text_changed_count=0`
- `guards.additional_llm_calls=0`
- `guards.outbound_provider_calls=0`
- `guards.duplicate_invocation_count=0`

## 48-hour observation contract

| Field | Value |
|-------|-------|
| Duration | 48 hours UTC |
| Sample target | ≥1 synthetic matrix PASS per 6 hours |
| Organic traffic | **Not claimed** — synthetic probes only |
| `customer_text_changed` | 0 |
| `additional_llm_calls` | 0 |
| `outbound_provider_calls` | 0 |
| `duplicate_invocation_count` | 0 |

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

`backend/tests/test_product_availability_truth_guard_shadow_observation_probe.py`
