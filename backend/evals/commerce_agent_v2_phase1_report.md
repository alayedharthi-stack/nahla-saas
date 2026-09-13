# Commerce Agent V2 — Phase 1 Eval Report

Date: 2026-09-13

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
grounding contract. Phase 1.2 added a run-local, deterministic exploration
budget: an initial empty catalog lookup may be followed by one reformulation;
after two consecutive misses, all Phase-1 read tools are hidden for the next
model turn so it must conclude from the empty results. Any successful catalog
search resets the budget. This changes neither the Agent instructions nor the
four tool contracts.

Phase 1.2's search-loop correction is verified. A narrow follow-up also fixed
the remaining Phase 1.1 false positive: a repeated availability word inside a
same-clause quantity rendering is accepted only when verified availability and
quantity claims have the same evidence ref, product subject, value, and state.
Wrong quantities, opposite availability states, and cross-product bindings
remain rejected.

The post-fix Sol High run completed all ten cases with ten correct outcomes and
zero unsupported claims. The false-positive case passed. Provider observability
passed with 27 provider-call records, and the eval end marker was emitted. The
aggregate quality gate nevertheless remained false at 8/10 tool behavior
because Sol added an unnecessary merchant-knowledge lookup in `linked-kb-ar`
and `missing-fact-ar`. Both the targeted `missing-product-ar` and the valid
two-search `gift-recommendation-ar` passed their tool contracts. No Agent,
persona, tool, session, tenant, or evaluator change was made for the new routing
variance.

## Historical aggregate before narrow false-positive fix

| Metric | Contaminated | Clean-room baseline | Phase 1.1 final | Phase 1.2 prior run | Enforced confirmation |
|---|---:|---:|---:|---:|---:|
| Cases | 10 | 10 | 10 | 10 | — |
| Completed | 3 | 3 | 10 | 10 | 9 |
| Failed | 7 | 7 | 0 | 0 | 1 |
| Tool behavior matches | 6 | 7 | 9 | 9 | 10 |
| Correct outcomes | not separately scored | not separately scored | 10 | 10 | 9 |
| Unsupported claims | 9 | 9 | 0 | 0 | 1 |
| Input + output tokens | 46,814 | 41,948 | 49,668 | 47,226 | 49,252 |
| Total latency | 141,981 ms | 123,628 ms | 151,767 ms | 121,424 ms | 159,054 ms |
| Estimated cost | $0.024615750 | $0.021360000 | $0.026470125 | $0.024039000 | $0.026818125 |
| Provider-call records | 0 | 26 | 26 | 26 | complete / passed |
| Eval end marker | Missing | Present | Present (`status=0`) | Present (`status=0`) | Present (`status=1`, enforced assertion) |

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

Phase 1.2 final deployment:
`eb4eab4e-53b3-465d-850c-2fd3dc9876a6`

Phase 1.2 final GitHub commit:
`43e6ade7f6260343b9791436e8dad209c7c1529b`

Phase 1.2 gate-enforced confirmation deployment:
`45bb78f8-3bd6-4b0d-9b48-a34e06427a68`

Confirmation GitHub commit (same file tree):
`44f066e47a36f691687c2dd44d5db62be0d43c99`

Narrow false-positive fix GitHub commit:
`737954708b5413d10996756ac730fed87d4baa6c`

Post-fix full-eval deployment:
`74ae6fbf-f675-493c-8548-8920e5b9ac62`

Post-fix eval source commit (same code tree):
`324427ae5f5b70430050664206aa16fac66597ed`

## Post-fix full-eval result

| Metric | Result |
|---|---:|
| Cases | 10 |
| Completed | 10 |
| Correct outcomes | 10 |
| Unsupported claims | 0 |
| Tool behavior matches | 8 |
| Provider observability | Passed |
| Provider-call records | 27 |
| Input + output tokens | 50,612 |
| Total latency | 136,543 ms |
| Estimated cost | $0.026460750 |
| Eval end marker | Present (`status=1`, enforced quality-gate assertion) |
| Quality gate | False |

The clean-room baseline and Phase 1.1 final runs both reported
`provider_observability_passed=true`. The final run reported
`quality_gate_passed=false` in both final runs because tool behavior was 9/10,
although the failing case changed after the targeted search loop was fixed. All
26 Phase 1.2 final model responses had a provider response ID, request ID,
per-call latency, and non-cumulative token usage.

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

## Phase 1.2 case comparison

| Case | Phase 1.1 final | Phase 1.2 final | Finding |
|---|---|---|---|
| `catalog-browse-ar` | One catalog search; pass | One catalog search; pass | No regression. |
| `catalog-specific-ar` | One catalog search; pass | One catalog search; pass | No regression. |
| `price-followup-ar` | One catalog search; pass | One catalog search; pass | No regression. |
| `product-provenance-ar` | Catalog + product knowledge; pass | Same tools; safe answer, but required product-knowledge evidence was not retrieved | Model-generated retrieval query varied; unrelated to catalog-miss budget. |
| `gift-recommendation-ar` | Two catalog searches; pass | Two catalog searches; pass | Successful second search resets the miss budget. |
| `budget-recommendation-ar` | One catalog browse; pass | One reformulation then catalog browse; pass | Successful browse resets the miss budget. |
| `global-kb-ar` | Merchant knowledge only; pass | Merchant knowledge only; pass | Direct policy lookup remains available before any catalog exhaustion. |
| `linked-kb-ar` | Catalog + product knowledge; pass | Same tools; pass | No regression. |
| `missing-product-ar` | Three catalog searches; fail | Two catalog searches; pass | Third low-value search and cross-tool continuation are blocked generically. |
| `missing-fact-ar` | Catalog + product knowledge; pass | Same tools; pass | No regression in the final run. |

Two intermediate Phase 1.2 runs were retained rather than cherry-picked away.
The first limited catalog search itself and fixed the third catalog call, but
one run continued through merchant knowledge after the search tool disappeared.
The strengthened version ends all Phase-1 tool exploration after the second
consecutive catalog miss. The final run verifies that behavior. Independent
Sol variability was also observed in `missing-fact-ar`, budget claim rendering,
and product-knowledge retrieval; no evaluator or Phase 1.1 guardrail semantics
were changed to conceal those observations.

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

## Phase 1.2 tool-efficiency stabilization

The Phase 1.1 trace showed these catalog query hashes for
`missing-product-ar`: the original `عسل مانوكا نادر`, a useful `مانوكا`
reformulation, then the overly broad `عسل` query. The first two returned no
products; the third discarded the distinguishing term and returned an unrelated
generic product. No run-local state told the SDK that sufficient negative
evidence had already been collected.

Phase 1.2 records consecutive empty catalog searches only inside the trusted
run context. The tool-availability predicate allows the initial lookup and one
reformulation. After the second miss it hides the four existing read tools for
the next model turn, preventing both a third broad catalog query and migration
of the same failed lookup to merchant knowledge. A successful search clears the
counter immediately, so valid multi-step discovery is not capped globally.
Nothing is persisted to Conversation or Session state.

The implementation is platform-general: it contains no merchant, product,
language, or `مانوكا` special case. It adds no Agent, classifier, tool, customer
text template, or instruction. The canonical evidence and guardrail contracts
are unchanged.

## Narrow Phase 1.1 false-positive correction

The failing `catalog-specific-ar` reply contained a verified
`availability=true` claim rendered as `متوفر لدينا` and a verified
`stock_quantity=8` claim rendered as `8 عبوات`. Its text also naturally said
`المتاح حاليًا 8 عبوات`. The final text-coverage pass treated that second word
as a new unclaimed availability assertion even though the typed facts jointly
supported the phrase.

The correction applies only to this joint rendering shape. An unmatched
availability mention may be supported by a quantity phrase in the same clause
only when both already-verified claims share the exact evidence ref and product
subject, the quantity matches, and the availability state matches. Tests cover
the accepted natural phrasing plus wrong quantity, opposite availability, and
cross-product evidence rejection. The canonical evidence and FactClaim schemas
are unchanged.

## Local verification after Phase 1.2

- Phase 1 suite: `57 passed, 1 skipped` (the skip is the opt-in live eval).
- Offline replay: `10/10` canonical scripted plans, each satisfying its tool/evidence contract.
- V1 provider-boundary replay: `2 passed`.
- Intelligence non-interference checks: `52 passed`.
- Constitution compliance: `57 passed`.
- `pip check`: no broken requirements.
- `git diff --check`: clean.

The separately known V1 test
`test_layer2_webhook_three_turn_replay_reaches_provider_boundary` still fails on
the pre-existing phone/SQLite fixture collision; the failure signature is
unchanged and the test does not exercise the V2 tool-budget path.

## Final live verification and disposition

The post-fix live run used `gpt-5.6-sol` with reasoning effort `high`, a
fresh Conversation for every case, and only explicitly declared history for the
three multi-turn cases. It emitted all summary, case, and provider-call records.
With live-gate enforcement enabled, it ended with
`COMMERCE_V2_EVAL_END status=1` because the aggregate gate was false.

Grounding-contract acceptance criteria passed: 10/10 completed, 10/10 outcomes,
zero unsupported commercial claims, zero cross-session contamination, zero
cross-tenant leakage observed, and no literal-copy requirement for valid price,
availability, quantity, or knowledge paraphrases. Negative tests still reject
changed facts, false bounds, wrong evidence refs, cross-product subjects, and
unclaimed commercial values.

The narrow grounding fix passed its target: 10/10 completed, 10/10 correct
outcomes, and zero unsupported claims. The missing-product plan remained two
bounded catalog calls with a safe fallback, and the gift recommendation retained
its acceptable two-search plan.

The overall Phase 1 quality gate nevertheless remains false at 8/10 tool
behavior. `linked-kb-ar` called merchant knowledge before the required catalog
and product-knowledge tools; `missing-fact-ar` called merchant knowledge after
product knowledge returned no evidence. Both final outcomes were correct and
safe, but the unnecessary calls violate their frozen tool contracts. No rerun
was used to select a luckier sample, and those routing changes were not folded
into the separately authorized false-positive correction.

Accordingly, Phase 1 is **not** marked final under the requested Definition of
Done. No evaluator expectation was weakened, no Agent instruction or persona
was changed, and no merge or production deployment was performed. Further work
would require a separately authorized tool-routing stabilization rather than a
grounding change.
