# Layer 3 Harness Measurement Contract

Layer 3 acceptance measures **production behavioral contracts** using structured
evidence captured by the harness. Critical defects must map to real production
defects — not harness false positives or missing telemetry.

## Principles

1. **Structured facts over exact Arabic** — PASS when trusted evidence matches the
   contract; do not require fixed customer-facing sentences unless the session
   explicitly tests an approved exact-text exception.
2. **Outbound-only for leak detection** — privacy/isolation scorers inspect bot
   outbound text, not customer inbound (except to detect echo).
3. **Critical = production failure** — `critical_defects` block release gates;
   `major_defects` signal quality gaps; harness gaps are `major` or `notes`,
   not critical.

## Score scale

Each axis uses a **1–5 integer rubric** where **5 = clean / no penalty** and
1 = critical failure on that axis. Session percentage is
`100 × sum(axis_scores) / (5 × number_of_axes)`. Aggregate axis percentages
divide axis averages by 5. Readiness gates that require 85–100% are only
attainable when defect-free sessions score **100%**.

## Score axes

| Axis | Measures | Primary evidence |
|------|----------|------------------|
| `isolation` | No cross-tenant catalog/KB leak | Outbound text vs forbidden tenant markers |
| `privacy` | No other-customer order facts without authorization | Outbound tracking tokens + session inbound echo |
| `price_stock_truth` | Price/stock/coupon truthfulness | `price_source`, outbound claims, coupon checks |
| `product_resolution` | Product thread gets replies | Outbound presence in product groups |
| `context_retention` | Multi-turn commerce focus survives | `brain_state_after`: `focus_product_id` (external_id→id→product_id→sku), `conversation_focus`, `previous_product_focus`, `suspended_product_focus` |
| `knowledge_policy` | KB/shipping/ETA grounded | `shipping_knowledge.fee_sar`, `verified_shipping_fee_sar`, shipping guard reasons, outbound fee OR honest-unknown guard |
| `order_tracking` | Tracking evidence when required | Outbound tracking token vs `expected_checks` |
| `handoff_truth` | Human ownership suppresses AI commerce | `handoff_active`, post-handoff brain/compose activity |
| `dialogue_usability` | Non-trivial reply length on LLM turns | Outbound length stats |
| `compose_quality` | Live compose usage / fallback rate | `compose_invoked`, `compose_source` |

| `compose_quality` | Live compose usage / fallback rate | `compose_invoked`, `compose_source` |

## Compose (`compose_quality`)

- Sessions with `handoff_then_no_commerce` **do not** emit
  `no_llm_compose_observed` when zero LLM compose turns are observed — AI
  compose suppression during human ownership is expected operational behavior.
- Audit note: `compose_not_expected_handoff`; `compose_quality` stays at 5.
- Non-handoff sessions with no compose still emit `no_llm_compose_observed`
  (major) unless `dedup_steps` applies.

## Privacy (`privacy_no_other_order`)

- Build replies from **outbound only**.
- FAIL when outbound contains `TRK-A-7788` (or configured token) and the
  customer never provided that token in session inbound (bot invented/fetched).
- PASS when the customer sent the token and outbound echoes it while refusing
  access to another customer's order.

## Dedup (`dedup_steps`)

- Session `L3-G8-01` uses `customer_key=D` (isolated from G7 handoff on `C`).
- Dedup sessions **do not** emit `no_outbound_or_brain` critical.
- Notes: `dedup_path_observed` when dedup hit or normal activity seen.
- Major: `dedup_session_no_activity` when neither dedup nor brain/outbound fired.
- Runner resets handoff flags for the dedup customer before execution.

## Context (BQ-3)

Context retention is scored **only** when the session script sets
`expected_checks.context_retention_required: true`. Do not infer context
requirements from message substrings or turn count alone.

Sessions **without** the flag (e.g. category browse `L3-G1-04`, missing-product
then category-only `L3-G1-06`, policy/category threads `L3-G4-03`) are not
penalized for absent product focus.

Focus identity resolution matches `product_focus_identity`:

`external_id` → `id` → `product_id` → `sku`

Context is considered retained when any turn shows focus identity, or
`conversation_focus` ∈ `{product, shipping_policy, order_tracking}`, or
previous/suspended focus snapshots are present.

## Shipping / knowledge (BQ-2)

Per turn the harness captures when available:

- `shipping_knowledge` from `build_shipping_knowledge_facts`
- `verified_shipping_fee_sar` from `resolve_verified_shipping_fee`
- `guards.shipping_guard_reason` when shipping post-compose guards fire

Shipping fee checks PASS when:

- Outbound contains the expected fee, **or**
- Structured evidence shows `fee_sar` / `verified_shipping_fee_sar` matching
  `expected_checks` (e.g. `shipping_fee_riyadh: "25"`).

FAIL when guard forces honest-unknown **and** no fee in outbound **and** no
structured verified fee.

## Files

| File | Role |
|------|------|
| `layer3_harness.py` | Turn execution, evidence capture |
| `layer3_scoring.py` | Session rubric + exported helpers |
| `layer3_evidence_utils.py` | Focus identity helper (no import cycles) |
| `layer3_sessions.py` | Session scripts + `expected_checks` |
| `run_layer3_dialogue.py` | Suite runner, dedup/handoff isolation |
