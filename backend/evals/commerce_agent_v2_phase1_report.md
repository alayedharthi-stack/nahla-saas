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
guardrail behavior was changed before this rerun.

## Aggregate before/after

| Metric | Contaminated run | Clean-room run | Change |
|---|---:|---:|---:|
| Cases | 10 | 10 | — |
| Completed | 3 | 3 | 0 |
| Failed | 7 | 7 | 0 |
| Exact expected tool plans | 6 | 7 | +1 |
| Unsupported claims | 9 | 9 | 0 |
| Input + output tokens | 46,814 | 41,948 | -4,866 (-10.4%) |
| Total latency | 141,981 ms | 123,628 ms | -18,353 ms (-12.9%) |
| Estimated cost | $0.024615750 | $0.021360000 | -$0.003255750 (-13.2%) |
| Provider-call records | 0 | 26 | fixed |
| Eval end marker | Missing (assertion crash) | Present | fixed |

Contaminated deployment:
`7d8774e8-85b4-4948-8b3a-12cd52bbe09f`

Clean-room deployment:
`bece6a66-ddbb-4d8a-984f-6e1352b89b89`

Clean-room commit:
`625dc24fe783fd56d052a385b5950d96348c7abc`

The clean run reported `provider_observability_passed=true` and
`quality_gate_passed=false`. All 26 model responses had a provider response ID,
request ID, per-call latency, and non-cumulative token usage.

## Case-by-case comparison

| Case | Contaminated run | Clean-room run | Isolation finding |
|---|---|---|---|
| `catalog-browse-ar` | `search_products`; failed on `stock` | Same tools and same failure | Grounding failure is real, not caused by shared history. |
| `catalog-specific-ar` | `search_products`; failed on `stock` | Same tools and same failure | Grounding failure is real. |
| `price-followup-ar` | Searched using leaked price wording; omitted details; failed on `stock` | Used declared `عسل طلح` history; searched `عسل طلح`; still omitted details and failed on `stock` | Isolation fixed product-query focus, but the missing `get_product_details` and grounding failure remain real. |
| `product-provenance-ar` | Expected tools; completed | Expected tools; failed on `product_knowledge` claim mapping | Tool routing is sound; claim/evidence validation remains unstable and needs contract-level investigation. |
| `gift-recommendation-ar` | Three catalog searches; failed on `stock` | Two catalog searches plus details; failed on `stock` | The stale first search disappeared, but unnecessary repeated search remains real. |
| `budget-recommendation-ar` | Leaked product query returned one candidate; failed on price/stock | Empty browse query returned both candidates; still failed on price/stock | Isolation fixed candidate coverage; catalog claim mapping remains broken. |
| `global-kb-ar` | Unnecessary catalog search, then merchant KB; failed on `stock` | Merchant KB only; completed | This failure and extra catalog call were caused by contaminated history. |
| `linked-kb-ar` | Expected tools; completed | Expected tools; failed on `product_knowledge` claim mapping | Routing is sound; claim/evidence validation is the remaining issue. |
| `missing-product-ar` | Leaked catalog search plus two target/broad searches; failed on `stock` | Two target/broad searches; completed with safe not-found response | Contamination caused the unrelated search and unsupported stock claim; one avoidable re-search remains. |
| `missing-fact-ar` | Expected tools; completed safely | Expected tools; completed safely | Stable across both runs. |

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

## Local verification

- Phase 1 suite: `42 passed, 1 skipped` (the skip is the opt-in live eval).
- Offline replay: `10/10` exact scripted tool plans.
- Related V1/governance selection: `66 passed`.
- `pip check`: no broken requirements.
- `git diff --check`: clean.

## Evidence-based next work

Do not treat Phase 1 as passing. Clean-room evidence supports investigating:

1. catalog and product-knowledge claim/evidence canonicalization in the output
   guardrail contract;
2. the missing details call in the explicit price follow-up;
3. repeated catalog searches in gift and missing-product cases.

The direct merchant-policy route is already correct in clean-room conditions,
so no Agent routing change should be made for that case based on the
contaminated run.
