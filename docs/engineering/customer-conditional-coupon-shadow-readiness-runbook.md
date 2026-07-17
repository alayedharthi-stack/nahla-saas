# Conditional coupon Layer 0 manual shadow-review checklist

Operator guide for encoding a **manual shadow-review checklist** for Layer 0 conditional-coupon facts. This artifact summarizes operator-supplied, schema-validated observations and manual attestations. It is **not** CI proof, database attestation, or an automated rollout gate.

## What this is

| Property | Value |
|----------|-------|
| Module | `backend/modules/ai/brain/truth_surface/customer_conditional_coupon_shadow_readiness.py` |
| Schema | `coupon_shadow_manual_checklist_v2` |
| `artifact_kind` | `manual_shadow_checklist` |
| `independent_verification` | `none` (always) |

The encoder may summarize sanitized Layer 0 fact/telemetry observations, but it **never** independently proves:

- CI job success
- A1 capability state in the database
- Subject bridge / proof correctness
- Deployment shadow-flag configuration

Operators must obtain those proofs **outside** this encoder and reference them when completing checklist items.

## What this is not

- Not a canary or compose enabler — `canary_or_compose_forbidden` is always `true` in archived JSON.
- Not a runtime flag activator — do not use this document to enable `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED` or any compose consumer.
- Not an eligibility claim — archived output must not contain `policy_eligibility_ready` or any raw eligibility truth.

## Hard gates (non-negotiable)

| Gate | Rule |
|------|------|
| **Default-off** | Production shadow flag remains unset/false. Default-off must produce zero Layer 0 loader I/O (`gate_skipped_reason=shadow_flag_disabled`, `order_count_query_count=0`). |
| **Manual attestations only** | Checklist items end with `_attested` and record operator sign-off, not automated verification. |
| **No canary / compose** | Customer-facing coupon compose/consumers remain forbidden regardless of checklist outcome. |
| **No operational claims in facts** | Layer 0 v8 fact records must match the closed top-level allowlist; no IDs, phone, message, or metadata. |

## Independent evidence operators must collect separately

Before completing the manual checklist, archive references to:

1. **CI run** — GitHub Actions run URL/ID where coupon Layer 0, bridge consumer, and PG E2E jobs are green on `main`.
2. **DB capability evidence** — A1 capability row read or reconciliation report output showing `validated` (see [a1-reconciliation-operator-runbook.md](./a1-reconciliation-operator-runbook.md)).
3. **Sanitized observation** — Layer 0 `fact_record` + `telemetry` copied from staging shadow observation (schema-valid only).

For staging blocked solely by missing fixture data at `{0088, 0089}` + validated A1,
seed the minimal tuple first via
[customer-conditional-coupon-shadow-fixture-runbook.md](./customer-conditional-coupon-shadow-fixture-runbook.md).
That harness does **not** enable the shadow flag; a **separate, time-boxed** shadow-only
observation window is required afterward (see fixture runbook § Future shadow-only
observation window).

This encoder does not fetch or verify any of the above.

## Manual checklist items (all required)

| Checklist token | Operator attests (unverified by encoder) |
|-----------------|------------------------------------------|
| `ci_layer0_unit_tests_attested` | Reviewed CI evidence for Layer 0 unit tests |
| `ci_bridge_consumer_tests_attested` | Reviewed CI evidence for bridge consumer tests |
| `ci_pg_e2e_a1_chain_tests_attested` | Reviewed CI evidence for PostgreSQL E2E |
| `a1_capability_validated_attested` | Reviewed DB/reconciliation evidence for A1 `validated` |
| `deployment_shadow_flag_default_off_attested` | Reviewed env audit that shadow flag is default-off |

Additional manual gate on evidence input:

| Field | Meaning |
|-------|---------|
| `a1_proof_gate_attested` | Operator attests subject proof gate reviewed (unverified) |
| `shadow_flag_default_off_observed` | Operator observed default-off in target env telemetry |

## Encoding procedure (archival only)

1. Collect independent CI, DB, and sanitized observation evidence (above).
2. Complete all `MANUAL_CHECKLIST_ITEMS` attestations honestly.
3. Prefer `build_evidence_from_layer0_observation(...)` — rejects unknown telemetry keys.
4. Call `evaluate_coupon_shadow_readiness(evidence)` and archive `result.to_dict()`.
5. Label the archive explicitly: **manual checklist artifact — not attestation**.

Direct `CouponShadowReadinessEvidence(...)` construction is allowed for tests/operators; evaluation always re-validates fact schema, forbidden keys, and sanitizer status. Caller-supplied sanitizer booleans are not accepted.

## Closed outcomes

| Outcome | Meaning |
|---------|---------|
| `manual_shadow_checklist_complete` | All manual checklist items + observation gates satisfied for **manual shadow review** only |
| `manual_checklist_incomplete` | One or more checklist items not attested |
| `blocked_by_invalid_input` | Unknown fact/telemetry key, invalid bridge outcome, or schema mismatch |
| `blocked_by_sanitization` | Fact absent or sanitizer/PII rejection |
| `blocked_by_a1_attestation` | Shadow-flag observation contradicts deployment attestation |
| `blocked_by_subject_proof` | `a1_proof_gate_attested` false, bridge unresolved/ambiguous, or proof-related `closed_reason_code` |
| `blocked_by_budget_telemetry` | Target scan overflow, per-turn query budget breach, or gate skipped in observation |

`ready_for_manual_shadow_review: true` means the **manual checklist is complete** — not that shadow, canary, or compose may proceed automatically.

## Stop conditions

Do not advance toward any customer-facing consumer when:

- `ready_for_manual_shadow_review` is false.
- Independent CI/DB evidence was not reviewed outside this encoder.
- `budget_exceeded=true` or `conditional_target_count > 5`.
- Forbidden fact keys or unknown schema fields appear in observations.
- Subject bridge outcome is `ambiguous`.
- Coupon Layer 0 or PG E2E CI fails on `main`.

## Cost monitoring signals

From sanitized Layer 0 telemetry (`build_sanitized_telemetry`):

| Signal | Budget / alert |
|--------|----------------|
| Bridge resolution | 1 Platform read-bridge call per subject resolve when shadow+relevance gates allow |
| Proof | Snapshot-only — no post-bridge proof rebuild in coupon subject module |
| `conditional_target_count` | ≤ 5; `budget_exceeded=true` is a stop |
| `order_count_query_count` | ≤ 1 per turn |
| `usage_evidence_query_count` | ≤ 1 per turn |
| `loader_duration_ms` | Track p95 per tenant scope |
| `gate_skipped_reason` | Must be `null` in active shadow observation samples |

## Output contract summary

**Archived JSON fields:**

- `readiness_schema_version`, `artifact_kind`, `independent_verification`
- `outcome`, `ready_for_manual_shadow_review`, `canary_or_compose_forbidden`
- `checklist_gates` (manual attestation/observation booleans only — no eligibility truth)
- `readiness_blockers`, `missing_checklist_items`

**Forbidden in archived JSON:** `policy_eligibility_ready`, `eligible`, customer IDs, phone, message text.

## Final customer-text provenance

This slice produces **no customer-facing text**. The checklist encoder emits operator JSON only. No Compose, Dispatch, LLM, template, sanitizer, dedup, or guard path is involved. Canary/compose consumption of conditional-coupon facts remains forbidden regardless of checklist outcome.
