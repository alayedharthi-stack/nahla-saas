# Conditional-coupon compose canary — enablement & teardown runbook

P0 gate slice before conditional-coupon compose activation. **This runbook does not
enable production flags by itself** — it documents preflight, staging verification,
and immediate teardown for a single test-mode tenant.

## Scope

- Early compose canary gate only (tenant allowlist + test mode + phone allowlist).
- Shadow observation remains independent (`NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED`).
- Compose master flag alone does **not** authorize Layer 0 I/O or compose routing.

## Configuration (closed names)

| Name | Kind | Purpose |
|------|------|---------|
| `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED` | env master | Global compose master (default **off**) |
| `customer_conditional_coupon_compose_allowlist_tenants` | tenant `ai_settings` list | Explicit tenant allowlist (no hardcoded merchants) |
| `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ALLOWLIST_TENANTS` | env fallback | Comma-separated tenant IDs when settings key absent |
| `store_ai_mode` | tenant `ai_settings` | Must be `test` for compose canary |
| `ai_test_allowed_numbers` | tenant `ai_settings` | Normalized phone allowlist (existing platform canary pattern) |

## Truth table (compose path)

| Master | Shadow | Allowlist | Store mode | Relevant turn | Phone allowlist | Layer 0 I/O | Compose |
|--------|--------|-----------|------------|---------------|-----------------|-------------|---------|
| off | * | * | * | * | * | **no** | **no** |
| on | on | * | * | yes | * | **yes** (shadow) | **no** unless canary passes |
| on | off | missing/empty | * | * | * | **no** | **no** |
| on | off | malformed | * | * | * | **no (global)** | **no** |
| on | off | tenant ∉ list | * | * | * | **no** | **no** |
| on | off | tenant ∈ list | not `test` | * | * | **no** | **no** |
| on | off | tenant ∈ list | `test` | no | * | **no** | **no** |
| on | off | tenant ∈ list | `test` | yes | missing / ∉ list | **no** | **no** |
| on | off | tenant ∈ list | `test` | yes | ∈ list | **yes** | **yes** (max one dedicated compose) |

Telemetry keys (no PII / no allowlist contents):

- `conditional_coupon_compose_canary_allowed`
- `conditional_coupon_compose_canary_reason`
- `conditional_coupon_compose_master_enabled`
- `conditional_coupon_compose_relevance_required`
- `conditional_coupon_compose_relevance_satisfied`

## Preflight gates (all required before staging canary)

1. **Live shadow evidence** — archived sign-off:
   `docs/engineering/staging-evidence/conditional-coupon-shadow-signoff-2026-07-18.json`
2. **Exact revision attestation** — consumer verifier pin (current: `8ea344fc` short;
   do **not** bump pin until this slice is the selected staging runtime).
3. **A1 validated** — dual-head `0088`/`0089`, capability `validated` @ `0088`.
4. **Tenant test mode** — `store_ai_mode=test` on the canary tenant only.
5. **Tenant allowlist** — `customer_conditional_coupon_compose_allowlist_tenants: [<tenant_id>]`.
6. **Phone allowlist** — probe customer in `ai_test_allowed_numbers`.
7. **No real outbound** — staging verification uses consumer verifier + suppressed providers.
8. **Telemetry acceptance** — denied tenant shows `tenant_not_allowlisted`; allowed tenant
   shows `allowed` without compose provenance when compose did not run.

## Staging verification (repeatable)

```bash
# Default-off + canary denied/allowed preflight (no DB)
python -m scripts.operators.customer_conditional_coupon_consumer_verify verify

# Full sign-off (requires fixture DB)
python -m scripts.operators.customer_conditional_coupon_consumer_verify verify --target-app-root .
```

Expected new gate phases:

- `compose_canary_denied` — master on, tenant not allowlisted → zero I/O
- `compose_canary_allowed_preflight` — closed settings tuple → `allowed`

## Enablement order (operations slice — not this PR)

1. Deploy runtime containing compose canary gate module.
2. Bump consumer-verify target pin to deployed revision (follow-up PR).
3. Set tenant `store_ai_mode=test`, allowlist, and phone list.
4. Set `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED=true` **only**
   after steps 1–3 pass in staging.
5. Run consumer verifier end-to-end on staging with **no outbound provider calls**.

## Immediate teardown

1. `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED=false` (unset).
2. Remove tenant from `customer_conditional_coupon_compose_allowlist_tenants`.
3. Clear env `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ALLOWLIST_TENANTS`.
4. Re-run consumer verifier `teardown_flags` phase — both shadow and compose env flags unset.
5. Confirm denied-path telemetry on non-canary tenants.

## Follow-up pin/deploy

This PR adds the gate and verifier phases but **does not** bump
`PINNED_TARGET_RUNTIME_REVISION` in `customer_conditional_coupon_consumer_verify_contract.py`.
After merge and deploy, open a dedicated pin PR targeting the deployed SHA before enabling
compose master in staging.
