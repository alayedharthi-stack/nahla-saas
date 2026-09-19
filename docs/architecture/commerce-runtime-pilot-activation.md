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

Ownership is decided in two places, because the paths that can answer a turn
live in two places.

**At the dispatcher**, in `_dispatch_message`, before any of its own owners:
`commerce_runtime_claims_inbound` asks whether this runtime owns the inbound —
either because the pilot is configured for this tenant and recipient on a
verified connection, or because this runtime already admitted this exact inbound
message. A claim routes the turn straight to `_handle_merchant_message` and none
of the dispatcher's short circuits run: payment receipt, payment evidence, map
image, payment claim, address and payment method all mutate order state and send
*before* the handler is entered, so a decision taken only inside the handler
never sees those turns at all. The claim answers nothing by itself.

**The claim is sticky, and it is scoped.** It is carried into the handler and
honoured only for the exact turn it was established for — tenant, normalised
recipient and provider message id all have to match, and a claim that cannot be
re-checked is held rather than released. The dispatcher withheld this turn from
its own owners on the strength of that claim, so from there a guard refusal, a
ledger lookup that fails, or an exception asking may stop the runtime
*executing*; none of them may hand the turn to the legacy path, which would
answer a message this runtime owns and may already have answered. Withholding is
not silence chosen over an answer: every gate that can silence a turn still
decides first, and a claimed turn that was not executed is logged as exactly
that.

**In the handler**, `pilot_guard.evaluate_pilot_route` is asked **once**, at the
one point that makes it a routing decision: **after** every gate that can
silence the turn — the AI pause / handoff / blocklist gate, the billing guard and
the conversation quota guard — and **before** every path that can answer it: the
Commerce Agent V2 owner, OrderFlowV2, checkout routing, the pre-brain routers
(branch trigger, location, arrival contact, staff contact, Layer 0) and the
Merchant Brain. Its answer is exclusive:

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
Once `handled` is true the return is unconditional: the observability sync that
runs after it is caught, so a failure there cannot reopen the legacy path for a
message the runtime may already have answered. The handler's own teardown sync is
caught too — not to protect this turn, which is already finished, but because
both provider entry points acknowledge the webhook before processing it, so an
exception escaping the handler abandons the rest of an acknowledged batch: the
next message in it and the status receipts after it.

### 3.1 Fail closed

Every condition must hold, and the first that does not decides the outcome:

| Refusal | Meaning |
| --- | --- |
| `pilot_disabled` | the switch is off; nothing is even looked up |
| `pilot_draining` | this process is out of rotation: it takes no new turn, its own unfinished turns are still finished, and the conversation is released to nobody (§3.5) |
| `tenant_not_allowlisted` | the tenant is not in an explicit, non-empty list |
| `recipient_missing` / `recipient_unnormalizable` / `recipient_not_allowlisted` | the conversation is not in an explicit, non-empty list |
| `model_not_configured` | no model was chosen for this pilot; nothing is looked up |
| `connection_not_verified` | the database does not agree that this WhatsApp connection is that tenant's |
| `legacy_already_answered` | an outbound reply was already sent for this inbound message |
| `ai_gate_skipped` | pause, handoff, blocklist or another existing gate told us to skip |
| `empty_inbound` | there is no customer text to answer |
| `guard_error` | the guard could not decide, so it refuses |

The model is part of that list on purpose. The loop is model-neutral and selects
nothing: `COMMERCE_RUNTIME_PILOT_MODEL` is read with **no default**, carried on
the decision, threaded through the entry and handed to the provider call, and
the entry refuses (`model_not_configured`) before admitting anything when it is
absent. Inheriting the legacy path's `CLAUDE_MODEL` or the repository fallback
would mean activating a pilot on a model nobody chose for it.

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

### 3.5 Handover

Switching the pilot off decides who takes **new** turns; it says nothing about
the turns the runtime already admitted. A handover is therefore a procedure, not
a flag, and it is performed against a **shared barrier** in the database —
`core.commerce_runtime.handover`, stored under one namespaced key in the
tenant's own `tenant_settings` row, so it needs no migration. Its states are
`open → draining → settled → open`.

A per-process environment flag cannot be the mechanism. It cannot say the same
word to every replica at the same moment, it cannot be observed from outside the
process holding it, and it changes at a moment nobody can name; a handover
decided on one is a handover decided on nothing. `COMMERCE_RUNTIME_PILOT_DRAINING`
remains, and takes one process out of rotation, but it is not the handover.

**Draining withholds; it does not release.** While a tenant is draining, an
inbound for a conversation this pilot would otherwise own is *buffered* —
recorded durably, answered by nobody — and settlement is blocked until each
buffered entry carries a recorded disposition. Releasing such a conversation to
the legacy path is the one thing a handover must not do: the runtime being
handed over from may still have a send in flight, and a second answer from a
second runtime is precisely the outcome being prevented. Traffic the pilot would
not have owned is untouched: the drain is evaluated after the tenant and
recipient allowlists, so a recipient outside them keeps exactly the behaviour it
has today.

**No turn is admitted that a post-drain check cannot see.** The barrier is read
on the admission transaction's **own connection**, under a tenant-scoped
advisory lock which the drain takes exclusively. The database therefore orders
the two, and there are only two orderings: the admission got the shared lock
first, so the drain waits and the turn is visible to every count taken
afterwards; or the drain committed first, so the read sees `draining` and the
admission is refused with nothing written (`handover_barrier_closed`). An
invocation that selected the runtime *before* the drain cannot slip between a
zero count and a settlement. A re-entry for a turn already admitted never
reaches the barrier, which is what lets a draining runtime still finish its own
work.

`scripts/operators/commerce_runtime_pilot_handover.py` is the executable
procedure — `status | drain | dispose | settle | reopen`. `settle` exits 0 only
when the barrier is draining, every live worker has reported the current
generation, every count is zero, and nothing buffered is undisposed; otherwise
it exits 1 and names each blocker. The counts are admitted turns with no
terminal, reserved intents nothing dispatched, attempts with no established
outcome, and attempts whose established outcome is `unknown`. The last counts
because an unknown send may still be with the provider and may still deliver;
treating a recorded unknown as resolved would be claiming non-delivery on no
evidence, so it blocks rather than ageing out. `settle` writes its evidence
snapshot before anything reopens, and `reopen` refuses unless the barrier is
settled.

What the job can prove is bounded, and it says so rather than overstating it.
Convergence is evidence from the workers themselves — each records the barrier
generation it is running the first time it evaluates a route after the change —
not a statement that they were restarted; a worker last seen before the drain
and still inside the liveness window has an *unknown* disposition and blocks
settlement by name. Counts are the database's account of recorded work. Neither
can see a request already on the wire to the provider. There is no attestation
step and no attestation variable: the verdict rests on the procedure having been
executed, never on an assertion that the fleet is quiesced. The job also refuses
an allowlist larger than it will inspect, rather than reporting on a silent
subset.

The emergency stop — switching the pilot off outright — remains available. It
stops this process from taking new turns and from recovering; it is not
instantaneous across a fleet and does not stop a send already in flight. The same
job then reports what it left behind.

---

## 3.4 The conversation so far

A follow-up needs what came before it. The pilot reads the prior turns **bound
to the conversation the runtime admitted this turn under**, not resolved from the
phone number: one tenant can hold several conversations for one number, and a
phone lookup answers with the most recent of them, which is not necessarily this
one. Rows written before conversation linking carry no conversation id and are
admitted only where the number is *established* to name this one conversation
and no other. Two answers are both "not established" and both exclude them:
several conversations carry the number, or none does — a conversation whose
number lives only in `external_id` produces no association row at all, and
finding no evidence of an association is not evidence of a unique one.

An outbound row contributes only text established to have been transmitted: the
wire audit's own record of it, or a body written while the wire was observed. A
row this runtime wrote while the wire was *unobserved* holds the reserved intent,
which the send path may have rewritten before it went out — it stays in the store
for the operator and is left out of the model's history, because showing it would
present a draft as the thing that was said. The inbound message being
answered is dropped when the store has already persisted it, and a history read
that fails yields no history rather than a guess.

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
runtime probes **all nine** relations it uses — the three foundation relations
and the six ledger relations — and treats *absent* or *partial* as unavailable:
it refuses the turn (`runtime_schema_unavailable`) rather than running
half-present. Checking fewer would pass on a foundation-only database that has
no terminals table, which is exactly the shape revision `0108` leaves behind.

The probe is cached per engine so a disabled pilot costs nothing per turn. That
cache is per process and is not invalidated by applying the migration, so the
service is restarted after it; until then every turn refuses, which is the safe
direction.

---

## 6.1 Recovery

A turn is left unfinished when the worker holding it stopped between admitting
it and recording its terminal. The turn is keyed by the inbound provider message
id, so the redelivery carrying that id is the one event that can reach it — and
deduplication is what normally stops that redelivery, correctly, because a
duplicate must never produce a second answer.

`core.commerce_runtime.recovery` answers two questions about one inbound
identity, and the difference matters. *Is there unfinished work* decides
deduplication: a duplicate carrying an admitted turn with no terminal reaches the
handler, a duplicate of finished work is dropped exactly as before, and anything
the lookup cannot establish is dropped as well. *Did this runtime admit it at
all* decides ownership, and is deliberately wider: a turn can be completed by
another invocation between deduplication letting the retry through and routing
looking at it, and asking only about unfinished work then answers no — handing an
inbound this runtime owns and has already answered to the legacy path as if it
were new. Both an open and a finished turn are withheld from legacy; only an open
one is resumed. The question is only asked for a tenant and a
recipient the pilot is explicitly configured for, and only while the pilot owns
open work (on, or draining).

Letting the retry through resends nothing. The delivery ledger still refuses to
dispatch an attempt whose outcome is pending, accepted or unknown; a refused
redispatch now reports the outcome that reservation already **established** —
reusing an accepted send's own provider message id rather than reporting
`not_attempted` and completing the turn as failed while the customer holds the
message. The row for a recovered acceptance is written once: a re-entry that
finds that provider message id already in the conversation persists nothing.

---

## 7. Proven, and not proven

The PostgreSQL proofs in
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

The handover controls the closure review reproduced are in
`tests/commerce_reliability/test_commerce_runtime_pilot_handover_controls_pg.py`,
each in two arms — the reviewed shape and the corrected one — on the same real
database, the real admission path, the real ledger and the real operator job.

### Remaining limitations

* One reply per turn: the pilot sends text, not rich or interactive messages,
  and the bounded rich→text recovery path is not used.
* **Provider acceptance is not confirmed customer delivery.** `replied=True`
  with a `provider_message_id` means the provider accepted the send; reach stays
  `unknown` until a delivery or read receipt is recorded, and nothing records
  those yet. Nothing in the pilot may be read as evidence a customer received or
  saw a message.
* **The reply the model composed and the text on the wire are two different
  things.** The send path may postprocess the LLM's reply intent, so what
  WhatsApp receives is not always what the model produced. The persisted row
  keeps the distinction — `commerce_runtime_intent_sha256` for the reserved
  intent, `final_text_transformed` with `final_transform_reasons` for what was
  transmitted — rather than presenting one as the other.
* **The integration is Anthropic-specific.** The loop's contracts are
  model-neutral, but the shipped adapter, tool-call shape and error handling are
  written against Anthropic's API; another provider is not a configuration
  change. Choosing the model is likewise one activation prerequisite among
  several — the tenant, the verified connection, the recipient handsets, the
  schema and, where it applies, the authorised shared migration target are each
  separate and each still required.
* **A handover depends on the procedure in §3.5 actually being executed** — a
  recorded drain, convergence the workers themselves reported, zero counts and a
  disposition for every buffered inbound. It does not depend on, and cannot be
  satisfied by, an assertion that the fleet is quiesced.
* A turn left open because its delivery was reserved but never dispatched is
  resumed only by a redelivery of the same inbound identity; there is no
  reconciliation worker, so a turn whose retry never arrives is finished by an
  operator, and the handover job is what surfaces it.
* The transmitted text is observed through the platform's wire audit. When that
  observation is unavailable the persisted row records
  `final_text_transformed=true` with `wire_text_unobserved` — unverified, never
  certified unchanged.
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
