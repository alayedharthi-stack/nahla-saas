# Trusted Context Layer 2 — Intent Evidence + DecisionPlan Shadow

**Status:** Proposed — design only; pending architecture review  
**Date:** 2026-07-14  
**Package:** `backend/modules/ai/brain/truth_surface/layer2/`  
**Depends on:** Layer 1 on `main` (#574, #580, #582, #583)

This document describes **PROPOSED / SHADOW CONTRACT** types only. Layer 2 is **not implemented at runtime** and is **not wired** to webhook, Brain, prompt, Compose, loaders, telemetry, or lifecycle execution.

---

## Design-only constraints (frozen)

- Layer 2 remains **pure contract/design only** — no runtime telemetry wiring in this phase.
- **No loader selection migration** — Layer 1 `coupon_offer_loader` gate stays independent.
- **`عروض` parity drift** — Layer 1 matches `عروض` (#582); Layer 2 builder patterns do not yet include the plural form. Documented prerequisite for any future Layer 2 telemetry PR. Do **not** modify runtime `intent_evidence` or loader gates in this PR.
- **No lifecycle DTO** — lifecycle boundary is owned by `core/commerce_lifecycle` (PR #575). This package defines AI-side shadow contracts only; no `LifecycleHandoffRef` or competing handoff type.
- **No direct imports** from `core.commerce_lifecycle`, `trusted_context.py`, webhook, Brain, Compose, or loaders.

---

## Contracts (schema_version = `"1"`)

### IntentEvidence

Structured evidence only:

- `schema_version`, `confidence`, `entities`, `required_domains`, `evidence_refs`
- `ambiguity_state`, `trigger_ids`, `source_turn_ref`, `shadow_only`

No DB, network, customer prose, coupon codes, phone numbers, or fact payloads.

### DecisionPlanShadow

Coverage comparison metadata only:

- `schema_version`, `proposed_action`, `required_facts`, `missing_facts`, `loaded_coverage`
- `constraints`, `safety_flags`, `reason_codes`, `snapshot_ref`, `shadow_only` (must be `true`)

Enforcement is impossible by default — no execute/enforce API.

### `clarify_missing` — telemetry label only

`proposed_action=clarify_missing` records missing required coverage for drift telemetry. It must **never**:

- cause customer clarification or customer-facing text
- change routing, webhook handling, or loader selection
- alter Brain, Decision, or Compose behavior
- trigger reload, enforcement, or lifecycle execution

### DomainDefinition registry

Static metadata only — `loader_id` is a **string reference**, never a callable. Nine domains registered with `read_only=True` for AI architecture-owned loaders.

---

## Prohibited in this phase

| Zone | Prohibited |
|------|------------|
| Webhook / pipeline | Any Layer 2 hook |
| Brain / Compose | Any kwargs or projection from shadow |
| Loaders | Selection changes based on Layer 2 output |
| Telemetry | Production log wiring |
| Lifecycle | Handoff DTOs, imports, dispatch |
| Enforcement | `shadow_only=false`, execute paths |

---

## Rollout gates

### Gate 0 — This PR (contracts only)

- [ ] Pure `layer2/` package on `main`
- [ ] Focused contract tests green
- [ ] `constitution-compliance` green
- [ ] Design record accepted after architecture review

### Gate 1 — Future telemetry-only PR (explicit approval)

Metadata-only compare after Layer 1 snapshot. Not authorized by Gate 0.

---

## Verification

```bash
pytest backend/tests/test_layer2_shadow_contracts.py -q
pytest backend/tests/test_constitution_compliance.py -q
```

---

## References

- Layer 1 mass validation: `backend/tests/trusted_context_layer1_scenarios.py`
- Lifecycle contracts (other agent): `backend/core/commerce_lifecycle/`
- Nahla Constitution: `AGENTS.md`
