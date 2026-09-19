# Commerce runtime — live reasoning provider and real read tools (contract)

Status: merged behind no flag and reachable from no runtime path yet. This
document covers the first half of the owner-authorised pilot integration:
items 1, 2 and 4 of *What a real reasoning adapter still needs* in
`commerce-runtime-agent-loop.md`. Delivery, routing, the pilot allowlist and
activation are a separate change and are **not** described here.

- Loop contract: `docs/architecture/commerce-runtime-agent-loop.md`
- Ledger contract: `docs/architecture/commerce-runtime-effect-and-delivery-ledgers.md`

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO       (the system prompt is reused verbatim; see §1)
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
MIGRATION_ADDED=NO
```

---

## 1. Ownership: what this change does and does not own

Nahla keeps the loop. `core.commerce_runtime.agent_provider` owns exactly one
translation — one `ProviderRequest` to one model request, and that request's
answer to exactly one member of the closed `ProviderResult` union. It holds no
budget, reserves nothing, retries nothing and decides nothing about what
happens next. **No second autonomous loop exists**: the Agents SDK is not used
by this path, `max_retries` is zero on the client, and each call is a single
`messages.create`.

The **system prompt is not authored here**. It is imported verbatim from
`modules.ai.commerce_agent_v2.agent.COMMERCE_AGENT_INSTRUCTIONS`, which is
where the repository already keeps the commerce agent's instructions. The
adapter refuses to be constructed without instructions, so it can never fall
back to prose of its own. It composes no greeting, apology, clarification or
any other customer-facing sentence.

One piece of model-facing text *is* added: the declaration of the reply
channel described in §3 (its name, one operational description and its input
schema). That is a capability declaration — how a finished answer is handed to
the platform — not tone, wording or persona. It is recorded here because the
distinction matters under GOV-001 and must not be hidden inside a `NO` flag.

The V2 read tools were split mechanically so both callers share one
implementation: each `X_impl(context, ...)` holds the unchanged body, and the
Agents-SDK `X(run_context, ...)` wrapper keeps the same name, signature and
description and simply forwards. The model-visible tool surface of the V2 agent
— every name, description and JSON schema — is asserted byte-identical to the
pre-split surface.

---

## 2. One inference step

`AnthropicProvider.call_single_step` is additive next to the existing
`call` / `call_messages` / `call_with_tools`, which are untouched. It exists
because those deliberately flatten away three things a loop owner needs:

| Needed | Why the flattened path cannot give it |
| --- | --- |
| tool-use block **ids** | `actions` keeps only `{type, payload}`; the id is the model's correlation handle |
| **stop reason** | truncation and refusal are indistinguishable from a complete answer without it |
| **usage** | spend per step cannot be attributed to a turn without it |

It reuses the existing model resolution (`resolve_model_for_provider` over
`resolve_anthropic_model`), the existing cost audit (`emit_llm_cost_audit`) and
the existing usage ledger (`record_ai_usage_from_anthropic`). It never raises;
its `status` is closed:

`ok`, `no_api_key`, `sdk_unavailable`, `auth_error`, `rate_limited`,
`overloaded`, `timeout`, `connection_error`, `api_error`, `sdk_error`.

### 2.1 Reconciling SDK retries with durable attempt accounting

The loop debits one reasoning attempt durably **before** the provider is
called. If the SDK silently retried inside that call, one durable attempt could
stand for several real requests, and the recorded budget would understate what
was spent. The client is therefore constructed with `max_retries=0` and an
explicit per-request `timeout` equal to the loop's own remaining wait: **one
durable debit is one request to the model**. Retrying at all is the loop's
decision, taken from its persisted budget, never the library's.

### 2.2 The closed translation

| What the step returned | Result | Reason |
| --- | --- | --- |
| tool-use blocks, within the declared maximum | `ProviderToolRequests` | model block ids become `call_id`, for correlation only |
| the reply channel alone | `ProviderReply` | text, evidence references and the commerce flag as the model set them |
| the reply channel **with** other tool calls | `ProviderInvalid` | `reply_mixed_with_tool_requests`; never guessed either way |
| `stop_reason = refusal` | `ProviderBlocked` | `model_refusal`; not retried by the loop |
| `stop_reason = max_tokens` | `ProviderInvalid` | `truncated_output`, even when a complete-looking reply block is present |
| text with no tool call | `ProviderInvalid` | `no_tool_use_block`; a plain sentence is never delivered |
| malformed reply arguments | `ProviderInvalid` | one named reason per shape (missing text, refs not a list, flag missing, arguments not an object) |
| more requests than declared | `ProviderInvalid` | `tool_requests_exceed_declared_maximum:<n>><max>` |
| any non-`ok` status | `ProviderFailure` | the status itself is the reason; an unknown status becomes `unexpected_status:<status>` |

Every one of these is asserted to pass `validate_provider_result` — the
adapter cannot return something the loop must reject as malformed.

---

## 3. The reply channel

The model answers through one declared tool, `submit_reply`, rather than free
text. The loop's verifier needs two things prose cannot carry: the evidence
references the answer rests on, and whether the answer asserts commerce facts
at all. `tool_choice` is therefore `any`, so every step is either read requests
or the reply.

`claims_commerce_facts` stays **provider-declared**, exactly as the loop
contract already states. The platform does not infer it from the text and does
not infer references the model did not choose: a draft that cites nothing is
verified as citing nothing, and a commerce draft that cites nothing is refused.
Fabricating citations to make verification pass would defeat the verification.

---

## 4. The transcript

The adapter is stateless with respect to authority but remembers, for the
invocation it is alive in, the assistant blocks it received. That lets the next
step present native `tool_use` / `tool_result` pairs:

- A step whose observations are **all** present is replayed as
  `assistant(blocks)` + `user(tool_result…)`. A partial set is never sent as a
  partial pair; it falls through to the data block below.
- A draft **verification refused** is replayed with its own `tool_use`, and the
  verifier's problems as that call's `is_error` result. The model sees exactly
  why its draft was not accepted, and the platform rewrites nothing.
- Observations **restored** from an earlier invocation's checkpoint have no
  transcript to belong to. They are presented as a labelled
  `earlier_tool_observations` data block, marked `restored_from_earlier_attempt`
  and, where the checkpoint bound dropped a body,
  `result_body_dropped_by_checkpoint_bound`. No pair is fabricated for them.

Tool results are **data**, never instructions: each is a JSON document of the
observation, including its error code when the tool refused or failed.

---

## 5. Real read tools

`core.commerce_runtime.agent_live_tools` registers six tools, each backed by
the same implementation the Commerce Agent V2 read tools call:

| Tool | Implementation | Result kind |
| --- | --- | --- |
| `catalog_search` | `tools.catalog.search_products_impl` | `product_list` |
| `product_lookup` | `tools.catalog.get_product_details_impl` | `product` |
| `merchant_knowledge_lookup` | `tools.knowledge.search_merchant_knowledge_impl` | `knowledge_entry` |
| `order_lookup` | `tools.orders.resolve_customer_order_impl` | `order_summary` |
| `order_details` | `tools.orders.get_order_details_impl` | `order_details` |
| `shipment_lookup` | `tools.orders.get_order_shipment_impl` | `shipment` |

**No commerce write exists on this surface.** No order is created, updated or
cancelled, no payment is taken, no coupon is applied and no message is sent.
The registry refuses to register anything not declared read-only, and the six
declared schemas between them offer the model exactly six argument names:
`query`, `limit`, `product_id`, `order_number`, `purpose`, `order_id`.

### 5.1 Scope

Scope comes from the trusted runtime context and nothing else. The runtime
builds one `CommerceAgentContext` from the verified tenant, WhatsApp
connection, conversation and customer, and binds it to the tenant and
conversation the loop is running for. Every call re-checks that binding and
refuses a mismatch with `scope_override_refused`, before the implementation
runs. `tenant_id`, `conversation_id`, `store_id`, `owner_id` and the rest of
the reserved set are refused as arguments by the registry itself.

### 5.2 Provenance and uncertainty

Each observation carries the evidence references the underlying tool produced,
taken from the records themselves. A lookup that found nothing, was denied or
could not run reports `found: false` with the implementation's own reason and
**no** evidence references — an empty answer is never dressed as a fact. The
projections are bounded (5 products, 400 description characters, 4 knowledge
sections of 700 characters, 12 line items) so a full result stays inside the
loop's observation bound rather than being refused as too large.

### 5.3 The database session, and abandonment

The tools share one session, because the V2 read contract is stateful within a
turn: `product_lookup` and `order_details` are authorised only for ids an
earlier lookup in the same context returned. A session is not thread-safe, and
the loop abandons a tool call that overruns its wait without being able to stop
it. The binding therefore closes permanently the moment a second call finds the
first still running: every later read is refused rather than sharing a session
with a thread nobody is waiting for.

This is a **bounded refusal, not a guarantee that the abandoned work stopped**.
The abandoned call may still complete against that session; the binding stays
closed afterwards regardless, and the runtime closes the session only after the
turn.

---

## 6. Alignment with the public Anthropic agent guidance

The two references — *Building effective agents* and *Building agents with the
Claude Agent SDK* — guide this work; the merged Nahla contracts remain
authoritative. No SDK-owned lifecycle is introduced. This is not a claim of
literal recipe completion. The rows below extend the loop contract's table with
what this change adds.

| Principle | Implementation | Test evidence | Remaining limitation |
| --- | --- | --- | --- |
| The model does the reasoning; the harness keeps control | `agent_provider.AnthropicReasoningProvider.step` returns one result and decides nothing | `test_one_step_asks_for_one_attempt_with_the_loop_s_own_wait_and_prompt` | The loop's budget is fixed per turn; there is no adaptive effort |
| Tools are the model's only way to reach data | six read-only tools over the existing V2 reads | `test_the_registry_exposes_exactly_the_six_read_tools`, `test_no_declared_schema_offers_a_write_a_price_or_a_quantity_the_model_could_set` | Read-only by construction; no action tool exists to evaluate yet |
| Give the model its own results back | native `tool_use`/`tool_result` replay | `test_this_invocation_s_observations_are_replayed_as_native_tool_result_pairs` | Restored observations lose the native pairing and are shown as data |
| Fail loudly rather than plausibly | closed translation table, refusal on truncation and on mixed results | `test_a_truncated_step_is_invalid_even_when_it_carries_a_complete_looking_reply`, `test_plain_text_without_the_reply_channel_is_never_delivered` | A well-formed but wrong answer is still only structurally checked |
| Tool results are context, not authority | JSON observation documents, error codes preserved | `test_a_failed_observation_is_replayed_as_an_error_result_not_hidden` | Result bodies are projected and bounded, so the model sees less than the source row |
| Spend is measurable | per-step `StepUsage`, existing cost audit and usage ledger reused | `test_usage_is_recorded_per_step_when_the_api_reports_it`, `test_unavailable_usage_stays_absent_and_is_never_reported_as_zero` | Usage absent from a response stays absent; it is never estimated as zero |

---

## 7. Out of scope here

No delivery, transport, dispatch or reconciliation; no routing decision, no
webhook, worker or scheduler wiring; no activation flag or tenant allowlist; no
migration and no bootstrap-target change; no commerce write of any kind; no
model, prompt or persona change. The Salla address work stays separate and this
change makes no address claim. PR #1085 and unrelated V1 work stay frozen.
UC-01 and UC-02 remain open in the active legacy paths, and commerce
reliability acceptance remains **NOT ACCEPTED**.
