# Commerce runtime — delivery, routing and the owner pilot (contract)

Second half of the owner-authorised pilot integration. It connects the reserved
delivery intents to the established WhatsApp transport, adds **one** routing
decision so the legacy runtime and the commerce runtime can never both answer
the same inbound message, and gates the whole thing behind a fail-closed
allowlist that is **off** until configured.

- Provider and tools: `docs/architecture/commerce-runtime-live-integration.md`
- Loop contract: `docs/architecture/commerce-runtime-agent-loop.md`
- Ledger contract: `docs/architecture/commerce-runtime-effect-and-delivery-ledgers.md`
- Operating the pilot: `docs/engineering/commerce-runtime-pilot-runbook.md`

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
MIGRATION_ADDED=NO
COMMERCE_WRITE_ADDED=NO
```

---

## 1. One turn, one owner

```text
admit → claim → reason (loop) → dispatch → complete → release
```

`core.commerce_runtime.runtime_entry.run_commerce_runtime_turn` runs that
sequence and owns every step of it. The provider contributes one inference step
at a time, the tools contribute read-only observations, the transport
contributes one send; none of them decides whether the turn continues, whether
the reply is acceptable, or whether the turn is finished.

| Step | Owned by | Guarantee it carries |
| --- | --- | --- |
| `admit_turn` | foundation | the inbound provider message id is the identity: a redelivered webhook resolves to the *same* turn |
| `claim` | foundation | one owner at a time, fenced; a second invocation is told so rather than racing |
| `AgentLoop.run_turn` | this runtime | durable attempt accounting, bounded waits, evidence verification, one delivery intent |
| `dispatch_reserved_delivery` | delivery ledger | one attempt per call, reserved before the send, refused when an outcome already exists |
| `finalize_turn` | ledger | one immutable terminal whose transport outcome and reach are **derived from the receipts** |
| `release` | foundation | the lease is returned; failing to return it costs nothing, it expires |

### 1.1 A turn never silently blocks its conversation

Eligibility is the oldest turn without a terminal, so a turn left open would
stop every later turn in that conversation — the runtime would go quiet with no
record. A turn that cannot run (no trusted scope, an unexpected error) is
therefore **finished as failed**. The one case that stays open is the right one:
when a delivery is reserved but not dispatched the ledger refuses completion, so
a re-entry can still send what was already reserved.

---

## 2. Delivery

`core.commerce_runtime.delivery_dispatch` does one thing per call:

```text
reserve the attempt → send → classify the response → record the receipt
```

Order and permission are enforced by the ledger, not by this module.

| Transport reported | Receipt | Turn's processing outcome |
| --- | --- | --- |
| 2xx **with** a provider message id | `accepted` | `completed` |
| 2xx **without** one | `unknown` | `failed` |
| 4xx | `rejected` | `failed` |
| 5xx, no status, timeout | `unknown` | `failed` |
| the transport raised | `unknown` | `failed` |
| the ledger refused the reservation | no receipt, nothing sent | `failed` |

Three rules this table encodes:

* **Success is never claimed after a failed or unknown send.** Only an
  acceptance the provider itself identified completes a turn. The terminal's
  transport outcome and customer reach are derived by the ledger from the
  receipts, so nothing a caller believes can override them, and acceptance is
  never recorded as reach.
* **An uncertain send is never blindly retried.** A transport exception is
  recorded as *unknown*, not as a failure: the request may already have reached
  the provider. `reserve_delivery_dispatch` then refuses a second dispatch of a
  sequence whose outcome is pending, accepted or unknown, and a finished turn is
  refused outright because it is no longer the eligible turn.
* **A refusal is an outcome, not a crash.** A blocked reservation and a lost
  lease both return `not_attempted` with the reason named; neither sends
  anything and neither raises into the caller.

---

## 3. The routing decision

`core.commerce_runtime.pilot_guard.evaluate_pilot_route` is asked **once**, in
`_handle_merchant_message`, immediately before the Merchant Brain. Its answer is
exclusive:

* not permitted → the legacy path runs exactly as it does today;
* permitted → the handler returns after the commerce runtime's turn, so the
  brain below never runs for that inbound message.

There is no third answer and **no fall-through**. A commerce-runtime turn that
fails does not hand the customer back to the legacy brain mid-turn, because both
would then be answering the same message. Once the route is taken the pilot
service cannot raise back to the webhook: a failure there is a failed turn, not
a second reply.

The only exception is a failure to *ask* — if the guard itself cannot be reached
the legacy path continues, which is the same behaviour as the pilot being off.

### 3.1 Fail closed

Every condition must hold, and the first that does not decides the outcome:

| Refusal | Meaning |
| --- | --- |
| `pilot_disabled` | the switch is off; nothing is even looked up |
| `tenant_not_allowlisted` | the tenant is not in an explicit, non-empty list |
| `recipient_missing` / `recipient_unnormalizable` / `recipient_not_allowlisted` | the conversation is not in an explicit, non-empty list |
| `connection_not_verified` | the database does not agree that this WhatsApp connection is that tenant's |
| `legacy_already_answered` | an outbound reply was already sent for this inbound message |
| `ai_gate_skipped` | pause, handoff, blocklist or another existing gate told us to skip |
| `empty_inbound` | there is no customer text to answer |
| `guard_error` | the guard could not decide, so it refuses |

An empty allowlist permits nothing, and **a tenant is never enabled wholesale**:
the recipient list is required as well, so the runtime reaches the owner's own
test conversations and nothing else. Authorisation comes from configured ids and
from the verified connection row — never from a display name, a store title or
any other label.

### 3.2 Existing guards keep their meaning

The pilot adds no guard and removes none. `store_ai_enabled`, `store_ai_mode`,
pause, handoff, the blocklist, subscription and the dry-run boundaries are all
upstream of the routing decision and are respected through `ai_gate_skipped` and
`legacy_already_answered`. The send itself goes through the established
`_send_whatsapp_message` / `_post_wa` path, so the outbound sanitiser, the
AI-disabled send gate, the burst throttle, the outbound dedup and the dashboard
status stamp all still apply.

### 3.3 Finite limits

`pilot_budget()` returns the turn's attempt, timeout and wall-clock limits.
Configuration may only make a limit **smaller**: a missing, unparsable or
above-ceiling value falls back to the bounded default, so a mis-set variable can
never widen what one turn may spend.

| Limit | Default | Ceiling |
| --- | --- | --- |
| reasoning steps | 4 | 6 |
| tool calls | 6 | 8 |
| tool wait | 10 s | 20 s |
| provider wait | 35 s | 60 s |
| turn deadline | 75 s | 120 s |

---

## 4. What is written, and where

A turn that ends in an identified acceptance persists one outbound message row
through the existing `StateManager.save_message`, with the **exact text the
ledger dispatched**, read back from the reserved intent rather than rebuilt. Its
metadata carries the runtime contract fields (`compose_source=llm`,
`response_mode=grounded`, `chosen_path=commerce_runtime_pilot`,
`llm_candidate_present=true`, `final_text_transformed=false`,
`final_transform_reasons=[]`) together with the turn id, the delivery sequence
id, the provider message id and the evidence references. A turn that was not
accepted persists no outbound row and claims nothing.

No commerce write happens anywhere on this path: no order is created, updated or
cancelled, no payment is taken, no coupon is applied.

---

## 5. One end-to-end log line

Every routed turn emits one `[COMMERCE_RUNTIME_PILOT] route=commerce_runtime`
line carrying the whole chain: `turn_id`, `duplicate_inbound`, `loop_status`,
`stop_reason`, `steps_used`, `tool_calls_used`, `tools_called`, `evidence_refs`,
`delivery_sequence_id`, `reused_delivery`, `dispatch_status`,
`provider_message_id`, `processing_outcome`, `transport_outcome`,
`customer_reach`, `input_tokens`, `output_tokens`, `model`, `latency_ms`,
`owner_id`, `replied` and `reply_chars`. The customer's text is **not** in the
log line; only its length is.

A refusal logs one `route=legacy` line with its reason, except for the two
uninteresting ones (`pilot_disabled`, `tenant_not_allowlisted`) which would
otherwise be printed for every message on the platform.

---

## 6. Schema availability

Revisions `0108` and `0109` create the runtime's tables but are not part of the
normal bootstrap target, so a database may legitimately not have them. The
runtime probes once per engine and treats **absent or partial** as unavailable:
it refuses the turn (`runtime_schema_unavailable`) rather than running
half-present. The probe is cached so a disabled pilot costs nothing per turn.

---

## 7. Proven, and not proven

The sixteen PostgreSQL proofs in
`tests/commerce_reliability/test_commerce_runtime_pilot_pg.py` run the real
entry point — real admission, ownership, loop, adapter, trusted read context and
ledger — with only the model's HTTP call and the WhatsApp transport scripted.
They establish:

* one inbound message is answered at most once, across redelivery, re-entry that
  reuses the reserved intent, and two concurrent invocations;
* a rejected, unknown or raised send is never a completed turn, and an unknown
  send refuses a second dispatch;
* a provider failure, a model refusal and a draft citing unobserved evidence all
  send nothing;
* a turn is invisible to another tenant, and a conversation outside the tenant
  yields no trusted context and no send;
* the runtime refuses when its schema is not present.

They establish **no** model quality, **no** real WhatsApp delivery and **no**
customer readiness. Those need the live trial.

### Remaining limitations

* One reply per turn: the pilot sends text, not rich or interactive messages,
  and the bounded rich→text recovery path is not used.
* A reply the transport accepted is not proof the customer read it; reach stays
  `unknown` until a delivery or read receipt is recorded, and nothing records
  those yet.
* A turn left open because its delivery was reserved but never dispatched is
  resumed only by the next inbound message for the same identity; there is no
  reconciliation worker.
* The pilot route is synchronous inside the webhook request, bounded by the turn
  deadline.
* Verification remains structural, as the loop contract states.

---

## 8. Out of scope

No broad rollout, no non-owner conversation, no commerce write, no
customer-data backfill and no unrelated V1 change. The Salla address work stays
separate and the pilot claims nothing about an address being saved or adopted.
PR #1085 and unrelated V1 work stay frozen. UC-01 and UC-02 remain open in the
active legacy paths, and commerce reliability acceptance remains **NOT
ACCEPTED**.
