# Commerce Agent V2 — Phase 1 Eval Report

Date: 2026-09-12

Provider baseline: `gpt-5.6-sol`, reasoning effort `high`

Mode: read-only shadow evaluation through the official Agents SDK runner

## Current conclusion

The first live result was methodologically contaminated: all ten cases reused
the same persisted `Conversation`, whose fixture already contained an inbound
message (`كم سعر الطلح؟`). Each case used a different `inbound_trace_id`, so the
Session adapter correctly treated that row as prior history instead of the
current turn.

The leak was in the eval harness, not in the Session adapter's tenant or
conversation scoping. The harness now creates a distinct `Conversation` for
every case. Single-turn cases have no prior messages. Multi-turn cases receive
only the history declared inside that case's fixture.

The clean-room rerun completed and emitted its end marker. Isolation corrected
some routing and removed unnecessary work, but it did not eliminate the main
grounding failures. No Agent instructions, tool selection policy, tool set, or
guardrail behavior was changed before that rerun.

Phase 1.1 then replaced literal claim/evidence comparison with a typed canonical
grounding contract. The final Sol High run completed all ten cases, produced ten
correct outcomes, and recorded zero unsupported claims. The eval runner ended
normally and all 26 provider-call records were present. The overall quality gate
remains false because the missing-product case made three distinct catalog
searches; the evaluator correctly treats that as an unnecessary search loop.
Grounding stabilization is therefore verified, but Phase 1 as a whole is not
declared complete.

## Aggregate before/after

| Metric | Contaminated | Clean-room baseline | Phase 1.1 final | Clean → final |
|---|---:|---:|---:|---:|
| Cases | 10 | 10 | 10 | — |
| Completed | 3 | 3 | 10 | +7 |
| Failed | 7 | 7 | 0 | -7 |
| Tool behavior matches | 6 | 7 | 9 | +2 |
| Correct outcomes | not separately scored | not separately scored | 10 | — |
| Unsupported claims | 9 | 9 | 0 | -9 |
| Input + output tokens | 46,814 | 41,948 | 49,668 | +7,720 |
| Total latency | 141,981 ms | 123,628 ms | 151,767 ms | +28,139 ms |
| Estimated cost | $0.024615750 | $0.021360000 | $0.026470125 | +$0.005110125 |
| Provider-call records | 0 | 26 | 26 | unchanged |
| Eval end marker | Missing | Present | Present (`status=0`) | unchanged |

Contaminated deployment:
`7d8774e8-85b4-4948-8b3a-12cd52bbe09f`

Clean-room deployment:
`bece6a66-ddbb-4d8a-984f-6e1352b89b89`

Clean-room commit:
`625dc24fe783fd56d052a385b5950d96348c7abc`

Phase 1.1 final deployment:
`05430d20-95b7-4859-b513-bc8f8b2d099e`

Phase 1.1 final GitHub commit:
`22efa4e1ade0d692846d5476089269c0362e3372`

The clean-room baseline and Phase 1.1 final runs both reported
`provider_observability_passed=true`. The final run reported
`quality_gate_passed=false` only because tool behavior was 9/10. All 26 final
model responses had a provider response ID, request ID, per-call latency, and
non-cumulative token usage.

## Case-by-case comparison

| Case | Contaminated | Clean-room baseline | Phase 1.1 final | Final finding |
|---|---|---|---|---|
| `catalog-browse-ar` | `search_products`; failed on stock | Same tool; failed on stock | `search_products`; completed; no unsupported claims | Typed availability/quantity and numeric price claims fixed the real grounding failure. |
| `catalog-specific-ar` | `search_products`; failed on stock | Same tool; failed on stock | `search_products`; completed; no unsupported claims | Catalog facts and Arabic rendering are grounded. |
| `price-followup-ar` | Leaked price wording; failed on stock | Declared product history; failed on stock | `search_products`; completed; no unsupported claims | Follow-up uses only declared history; search evidence is sufficient for the exact price. |
| `product-provenance-ar` | Expected tools; completed | Expected tools; product-knowledge rejection | `search_products → search_product_knowledge`; completed | Canonical body plus deterministic paraphrase verification is stable for this case. |
| `gift-recommendation-ar` | Three searches; failed on stock | Two searches plus details; failed on stock | Two distinct searches; completed | No identical loop and all product facts are grounded. |
| `budget-recommendation-ar` | Leaked query; price/stock rejection | Correct browse; price/stock rejection | One browse; completed | Numeric price and the derived `150 < 200` statement both verify deterministically. |
| `global-kb-ar` | Catalog plus merchant KB; failed | Merchant KB only; completed | `search_merchant_knowledge`; completed | Direct merchant-policy routing remains correct; paraphrase is accepted. |
| `linked-kb-ar` | Expected tools; completed | Expected tools; product-knowledge rejection | `search_products → search_product_knowledge`; completed | Knowledge is grounded; informational «المتوفر» is not misread as stock availability. |
| `missing-product-ar` | Leaked search plus target searches; failed | Two searches; completed safely | Three distinct searches; completed safely | Outcome is correct, but an unnecessary third reformulation remains and fails the tool contract. |
| `missing-fact-ar` | Expected tools; completed safely | Expected tools; completed safely | `search_products → search_product_knowledge`; completed safely | Stable safe fallback with no invented harvest year. |

The aggregate `3/10 completed` hides a material composition change. The
contaminated run completed provenance, linked knowledge, and missing fact. The
clean run completed merchant policy, missing product, and missing fact.

## Isolation contract

- Every case creates a new tenant-bound `Conversation` and therefore a unique
  SDK session key: `commerce-v2:{tenant_id}:{conversation_id}`.
- `turn_mode=single` forbids fixture history and asserts an empty Session.
- `turn_mode=multi` requires explicit inbound/outbound history in that case.
- The current user input is passed to `Runner.run`; it is not persisted as a
  prior message in the case conversation.
- The Session adapter continues to read only `MessageEvent` rows matching both
  the trusted tenant and the exact conversation.

The live output recorded distinct session IDs `commerce-v2:1:4` through
`commerce-v2:1:13`; all single-turn cases recorded
`declared_history_count=0`, while the three multi-turn cases recorded exactly
two declared history messages.

## Experimental conversation reset utility

An internal admin/test utility is available at:

`POST /admin/debug/conversation-context-reset`

It is protected by normal admin authentication, `ENABLE_ADMIN_DEBUG`, the
optional admin-debug secret, tenant scoping, and an explicit confirmation
string. Preview is the default (`apply=false`). Applying requires:

```json
{
  "tenant_id": 1,
  "conversation_id": 123,
  "apply": true,
  "confirmation": "RESET_CONVERSATION_CONTEXT"
}
```

The reset creates a new latest Conversation for the same tenant/customer with:

- no `MessageEvent` history, producing a new V2 Session key;
- empty `brain_state` and no inherited conversation metadata;
- AI pause, human takeover, handoff, and urgency flags cleared;
- customer-level `ConversationHistorySummary` rows removed;
- active phone-bound `HandoffSession` rows resolved.

It preserves the customer, old conversations and messages for audit, catalog,
merchant/product knowledge, orders, payments, and other business records.

## Phase 1.1 grounding-contract stabilization

The stabilization replaces flattened string comparison with a typed canonical
fact contract while preserving the single Agent, four read-only tools, trusted
tenant context, and Conversation-backed Session architecture. Agent persona and
base instructions are unchanged.

Catalog evidence now publishes product-bound canonical facts for product name,
description, numeric price/sale/regular price, currency when the synchronized
catalog supplies it, boolean availability, integer stock quantity, product URL,
and image URL. Knowledge evidence publishes one source-bound canonical body fact
and the linked product id where applicable.

Each `FactClaim` carries the canonical typed value, the exact evidence ref, the
trusted product subject when product-bound, and the exact natural-language span
inside `CommerceReply.text`. The guardrail independently verifies:

- kind, source, evidence reference, and product subject;
- numeric price equivalence independent of display formatting;
- boolean availability and integer quantity semantics;
- currency and URL canonical equality;
- deterministic knowledge paraphrase support using normalized content overlap,
  polarity, numbers, and scope qualifiers;
- deterministic distinction between product availability and words such as
  «المتوفر»/«المتاح» when they qualify information rather than stock;
- deterministic verification of a rendered upper price bound against the
  canonical product price, for example `150 < 200`;
- coverage of price, quantity, availability, and URLs appearing in final text by
  a successfully verified claim.

The guardrail remains fail-closed. A correct paraphrase may pass, but an altered
fact, invalid evidence ref, cross-product claim, missing typed evidence, or
sensitive final-text value without a verified claim is rejected.

Tool evaluation is now contract-based rather than requiring one arbitrary exact
sequence. Every case declares required tools, acceptable efficient plans,
forbidden unnecessary tools, required evidence sources, and expected outcome.
The price follow-up accepts either catalog search alone or search plus details,
because the search result already contains the exact price. A missing fact may
also use one product-details check after product knowledge returns no evidence.
The missing-product case permits one evidence-seeking reformulation, rejects an
identical duplicate, and does not accept three searches merely to improve a
score.

## Local verification after Phase 1.1

- Phase 1 suite: `54 passed, 1 skipped` (the skip is the opt-in live eval).
- Offline replay: `10/10` canonical scripted plans, each satisfying its tool/evidence contract.
- V1 provider-boundary replay: `2 passed`.
- Intelligence non-interference checks: `52 passed`.
- Constitution compliance: `57 passed`.
- `pip check`: no broken requirements.
- `git diff --check`: clean.

## Final live verification and disposition

The final live run used `gpt-5.6-sol` with reasoning effort `high`, a fresh
Conversation for every case, and only explicitly declared history for the three
multi-turn cases. It emitted all summary, case, and provider-call records and
ended with `COMMERCE_V2_EVAL_END status=0`.

Grounding-contract acceptance criteria passed: 10/10 completed, 10/10 outcomes,
zero unsupported commercial claims, zero cross-session contamination, zero
cross-tenant leakage observed, and no literal-copy requirement for valid price,
availability, quantity, or knowledge paraphrases. Negative tests still reject
changed facts, false bounds, wrong evidence refs, cross-product subjects, and
unclaimed commercial values.

The overall Phase 1 quality gate remains false at 9/10 tool behavior because of
the three-search missing-product loop. That residual requires a separately
reviewed Agent/tool-routing stabilization decision; it was not hidden by
weakening the evaluator and no persona or base-instruction change was made in
Phase 1.1.
