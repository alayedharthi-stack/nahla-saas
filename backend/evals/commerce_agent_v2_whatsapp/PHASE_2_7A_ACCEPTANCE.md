# Phase 2.7A — canonical A1–C4 acceptance matrix

Artifact: `phase_2_7a_acceptance_v1.json` (contract `commerce_v2_phase_2_7a_acceptance_v1`).
Loader/runner: `backend/services/commerce_v2_phase_2_7a_acceptance.py`.
Tests: `tests/test_phase_2_7a_acceptance_matrix.py` (collected by the default root suite).

## Why these twelve turns

Phase 2.7A is the deterministic pre-review gate for the Commerce Agent: three
synthetic Tenant 1 customers, four turns each, twelve turns in total, no D
scenario. The owner selected each turn **verbatim** from the approved Phase 2.6
corpus so that the gate exercises the three journeys a Salla reviewer will look
at first, with no randomness:

| Customer | Journey |
|---|---|
| A | social entry, then grounded catalog discovery (browse → first product → price and availability) |
| B | multi-turn reference resolution inside one conversation (browse → "pick one" → "its price" → "more about it") |
| C | order and shipment truth for the customer's own synthetic order (status → items → shipment → tracking) |

## Verbatim source in `corpus_v1.json`

Every turn's `source` pointer is `(alias, segment_index, category, step_index, variant_index)`.
The loader re-reads `corpus_v1.json` and refuses to load if the text, expected
tools or expected outcome at that pointer differ from the artifact.

| Turn | Input | Source (alias / segment / category / step / variant) | Expected tools | Outcome |
|---|---|---|---|---|
| A1 | السلام عليكم | A / 0 / social / 0 / 0 | — | social_reply |
| A2 | وش المنتجات المتوفرة عندكم؟ | A / 1 / catalog_browse / 0 / 0 | search_products | grounded_reply |
| A3 | أبغى تفاصيل أول منتج عندكم | A / 2 / specific_product / 0 / 0 | search_products, get_product_details | grounded_reply |
| A4 | كم سعر أول منتج وهل هو متوفر؟ | A / 3 / price_availability / 0 / 0 | search_products | grounded_reply |
| B1 | السلام عليكم، أبغى أتصفح المنتجات | B / 6 / long_multiturn / 0 / 0 | search_products | grounded_reply |
| B2 | اختر لي واحداً منها | B / 6 / long_multiturn / 1 / 0 | search_products | grounded_reply |
| B3 | كم سعره وهل هو متوفر؟ | B / 6 / long_multiturn / 2 / 0 | search_products | grounded_reply |
| B4 | طيب هل عندكم معلومات إضافية عنه؟ | B / 6 / long_multiturn / 3 / 0 | search_products, search_product_knowledge | grounded_reply |
| C1 | وش حالة آخر طلب لي؟ | C / 0 / latest_order / 0 / 0 | resolve_customer_order | grounded_reply |
| C2 | وش محتويات آخر طلب؟ | C / 2 / order_items / 0 / 0 | resolve_customer_order, get_order_details | grounded_reply |
| C3 | وش حالة شحنة طلبي؟ | C / 4 / shipment_status / 0 / 0 | resolve_customer_order, get_order_shipment | grounded_reply |
| C4 | عطني رقم التتبع | C / 6 / tracking / 0 / 0 | resolve_customer_order, get_order_shipment | grounded_reply |

## Three tiers: machine gate, human reply review, final acceptance

| Tier | Who | What it proves | Output field |
|---|---|---|---|
| 1. Machine gate | runner | every turn executed in canonical order with tools, outcome, ownership, safety proofs, isolation, prior context and B continuity verified by code | `machine_passed`, `machine_summary` (`N/12 machine checks`), per-turn `machine_passed` / `machine_status` |
| 2. Human-visible reply review | named reviewer | every `required_assertion` of every executed turn judged against the customer-visible text and evidence, with reviewer, timestamp, verdict and short evidence | per-turn `review.assertions[]`, run `review_status` (`pending` / `approved` / `rejected`) |
| 3. Final Salla-readiness acceptance | both | tier 1 passed **and** tier 2 approved every assertion | `acceptance_passed`, `classification` |

`classification` is exactly one of `MACHINE_GATE_FAILED`,
`MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING`,
`MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED`, or `ACCEPTANCE_PASSED`. A run whose
twelve turns all pass the machine checks is reported as
**MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING**, never as "12/12 PASS", until a
reviewer has approved every required assertion through
`POST …/acceptance/phase-2-7a/runs/{run_id}/reviews` (or the offline
`acceptance-review` CLI subcommand). Any rejected assertion makes the run
`MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED`; a failed machine check makes it
`MACHINE_GATE_FAILED` regardless of reviews.

Continuity assertions on B2–B4 that the machine verified are annotated
`machine_check: b_continuity_verified` in the ledger but still need the
reviewer's verdict; when product identity could not be extracted
(`continuity.status = unverifiable`) the turn is `passed_unverified_continuity`
and those assertions remain strictly human-mandatory.

## Machine-gate semantics

A turn is **not** a pass because an HTTP request returned 200. For each turn the
runner records the full INTERNAL_E2E artifact evidence and scores it with the
existing `score_turn` contract plus matrix-specific checks:

* expected tools are a subset of the tools actually called; every called tool is
  one of the seven read-only tools;
* the expected outcome holds (`social_reply`/`grounded_reply` must not fall back;
  `safe_missing_fact` must use the expected safe fallback);
* Commerce Agent V2 owned the turn, V1 was bypassed, the guardrail passed, the
  trace/internal message ids correlate, `external_egress_count == 0`, and all
  seven safety proofs are proven with zero value (no unsupported commercial
  claim, no cross-tenant or cross-customer leakage, no duplicate reply, no
  silent V1 fallback, no write or Salla mutation);
* the reply was produced in the alias's own fixture conversation
  (`alias_conversation_mismatch` otherwise), with no real `customer_id`
  (`real_customer_fallback_forbidden` otherwise), and turns flagged
  `requires_prior_context` ran after every earlier turn of the same alias
  completed (`prior_context_incomplete` otherwise);
* **B continuity** from `structured_reply.product_refs`: B1 records the products
  shown (`continuity.products_shown`); B2 must select exactly one of them
  (`b_continuity_selection_not_from_b1`, `b_continuity_selection_ambiguous`);
  B3 and B4 must reference exactly that product (`b_continuity_product_switched`).
  When the stray product is one of the two catalog products named in B's seed
  history, the blocker is `b_continuity_seed_history_fallback`: the seeded
  history must never stand in for B1's grounded results, even when the expected
  tools were called. If no product identity is present the turn is marked
  `unverifiable`, not passed, and the continuity assertions stay human-mandatory.

`required_assertions` are the owner's per-turn review criteria; they are carried
verbatim into every result and into the review ledger so a human reviewer can
judge the customer-visible text against them alongside the machine checks.

Ordering is fixed: A1→A4, then B1→B4, then C1→C4, sequentially, one turn at a
time. A, B and C use three separate fixture conversations; continuity within an
alias comes from that alias's own conversation session. A halting failure
(status other than `completed`, any external egress, any safety blocker, a
conversation/customer mismatch, or an exception) stops the sequence; the
remaining turns are reported as `not_executed`, so the final summary can never
read 12/12 unless all twelve turns were executed and passed.

## Fixture requirements

The runner never provisions, resets, or heals fixtures. Before a run:

1. `POST /admin/internal-e2e/fixtures/provision` (optionally with
   `reset_aliases`) so that A, B and C exist as `internal_e2e:t1:customer:<alias>`
   conversations with no `customer_id`, B's approved seed history is present,
   and C owns the synthetic order `IE2E-C-001` (`source=internal_e2e`) with its
   synthetic shipment and tracking number.
2. The runner then verifies all of that and fails closed with
   `phase_2_7a_fixture_missing:<alias>`, `phase_2_7a_c_order_fixture_missing`,
   `phase_2_7a_c_shipment_fixture_missing`, or
   `phase_2_7a_real_customer_fallback_forbidden` before executing any turn.
3. B's seed history is verified row by row: exactly 32
   `internal_e2e_seed_history` rows (24 pagination + 8 reference), ids
   `internal_e2e:t1:b:seed:01…32` in order, alternating inbound/outbound,
   B-only identity metadata, and no such row in any other conversation.
   Failures: `phase_2_7a_b_seed_history_missing`, `…_count_invalid`,
   `…_order_invalid`, `…_metadata_mismatch`, `…_row_invalid`,
   `…_contaminated`.

## Support-access gate (production run path)

Creating or running an acceptance run requires an ACTIVE Tenant 1 support-access
grant whose merchant-approved purpose names Phase 2.7A and which has at least 30
minutes remaining. The gate reads the grant exactly as the support-access router
stores it and runs **after** the INTERNAL_E2E scope check and **before** any
fixture is read or a control row is created. Failures:
`phase_2_7a_support_grant_missing`, `…_expired`, `…_expiry_missing`,
`…_insufficient_remaining`, `…_purpose_mismatch`, `…_wrong_tenant`. The runner
never creates, requests or approves a grant. `GET …/matrix` is read-only and
does not require a grant.

## Safety gates and cleanup

* Execution requires the existing INTERNAL_E2E gates
  (`NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED`, tenant allowlist `1`); when
  disabled the runner raises `internal_e2e_disabled` before reading fixtures.
* All routes live under the admin-only `/admin/internal-e2e/acceptance/phase-2-7a/*`
  prefix (`GET …/matrix`, `POST …/runs`, `GET …/runs/{run_id}`,
  `POST …/runs/{run_id}/reviews`); the CLI equivalents are
  `scripts/operators/commerce_v2_internal_e2e.py acceptance` and
  `acceptance-review`.
* Every turn is persisted through the normal INTERNAL_E2E inbound/outbound
  rows with `case_id=P27A:<turn_id>` and `batch_id=p27a:<run_id>`; full
  artifacts remain retrievable via `GET /admin/internal-e2e/results?trace_id=…`.
* After a run, disable INTERNAL_E2E and reset only the synthetic A/B/C fixtures
  (`POST /admin/internal-e2e/fixtures/{alias}/reset`). Real customers,
  conversations, orders and merchant data are never touched by this gate.

## Deterministic gate vs the randomized 180-turn batch

| | Phase 2.7A gate | Phase 2.6 batch |
|---|---|---|
| Artifact | `phase_2_7a_acceptance_v1.json` (12 fixed turns) | `corpus_v1.json` (180 seeded turns) |
| Variant selection | none, verbatim text | seeded random variant per step |
| Ordering | fixed A1→C4, sequential | scheduled, optional A/B/C concurrency waves |
| Purpose | pre-review acceptance, one deterministic pass/fail | regression coverage and latency/tier metrics |
| Output | per-turn results, machine summary, review ledger, classification | batch score report (`score_batch`) |

Both run through the same INTERNAL_E2E turn path, the same safety proofs, and
the same zero-egress hard gate.
