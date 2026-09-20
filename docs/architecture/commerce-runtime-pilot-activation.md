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
message. It is asked **before every competing branch**, which is earlier than it
used to be: the dispatcher's cash-on-delivery button routes sit above the
payment short circuits, and both act before the merchant handler is entered. A
claim routes the turn straight to `_handle_merchant_message` and none of them
run — payment receipt, payment evidence, map image, payment claim, address,
payment method, and the two COD routes all mutate order state and send, so a
decision taken only inside the handler never sees those turns at all. Inside the
handler the same claim is checked once more, above the COD text-reply
interception, which is an owner for the same reason: it transitions the order
and sends the customer a follow-up. The claim answers nothing by itself, and the
pilot has no commerce-write tool, so a claimed COD turn is answered from what
the runtime can observe rather than by confirming an order.

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

### 3.5 Handover, and what an acknowledgement promises

Switching the pilot off decides who takes **new** turns; it says nothing about
the turns the runtime already admitted, nor about the messages the provider has
already been told we have. Both are durable state, and both live in the
runtime's **own** relations (revision `0111`):

| Relation | What it holds |
| --- | --- |
| `commerce_runtime_handover_barrier` | whether this tenant admits new work, on which generation, and the evidence its settlement rested on |
| `commerce_runtime_handover_workers` | one row per process, carrying the generation and state **it observed**, and — if retired — who retired it and why |
| `commerce_runtime_deferred_inbound` | one row per accepted inbound nobody has finished, with the identity and payload a replay needs |

They were previously a namespaced key inside `tenant_settings.metadata`. That
document has other writers, each of them read-modify-write over the whole JSON,
so any of them could put back a copy taken before a drain and silently reopen
it. Coordinating every settings writer would mean rewriting code with nothing to
do with this handover; the runtime's own state belongs where only the runtime
writes, and that is what the migration does.

**The barrier.** `open → draining → settled → released → open`. A per-process
environment flag cannot be the mechanism: it cannot say the same word to every
replica at the same moment, it cannot be observed from outside the process
holding it, and it changes at a moment nobody can name.
`COMMERCE_RUNTIME_PILOT_DRAINING` remains, and takes one process out of
rotation, but it is not the handover.

**Release is a transition acceptance reads.** A settlement is evidence about
the instant it was taken; the switch is flipped later, and an inbound accepted
in between would be recorded and then abandoned. So `release` is written under
the tenant's exclusive lock — only when the settlement still holds, nothing is
pending, nothing arrived after it, and the fleet is converged *and reconciled
against the stated deployment inventory* — and `record_inbound` reads the
barrier under the shared lock in the same transaction that would insert. Either
the release committed first and the inbound is refused (`503 pilot_released`,
nothing recorded), or the insert committed first and the release counts it as
pending and refuses. There is no third ordering, so nothing accepted can fall
between the release and the configuration change.

**Draining withholds; it does not release.** While a tenant is draining — or is
settled and not yet reopened — an inbound for a conversation this pilot would
otherwise own is *deferred*: recorded durably, answered by nobody, and settlement
is blocked until each entry carries a checked disposition. Releasing such a
conversation to the legacy path is the one thing a handover must not do: the
runtime being handed over from may still have a send in flight. Traffic the pilot
would not have owned is untouched — the drain is evaluated after the tenant and
recipient allowlists.

**No turn is admitted that a post-drain check cannot see.** The barrier is read
on the admission transaction's **own connection**, under a tenant-scoped
advisory lock which a transition takes exclusively. There are only two
orderings: the admission got the shared lock first, so the transition waits and
the turn is visible to every count taken afterwards; or the transition committed
first, so the read sees `draining` and the admission is refused with nothing
written (`handover_barrier_closed`). A re-entry for a turn already admitted never
reaches the barrier, which is what lets a draining runtime still finish its own
work.

**Accepted work is not new work.** An inbound the provider was told we had —
recorded before the 200, or deferred when a drain refused it — is an obligation
the settlement already counts, and a drain that refused to admit it could never
be met: recovery would need the barrier open, reopening would need nothing
pending. The recovery runner therefore states a **grant** for one replay — this
tenant, this channel connection, this provider message id, this entry — and the
claim and the seam honour it only when the identity matches, a pending durable
acceptance exists for it, and the barrier read on the admitting connection is
open **or draining**. Settled and released still refuse; the operator drains
again first. The grant authorises nothing the database does not already owe.

**Ownership, once established, is not re-decided by a failure.** The scope
decision is captured the moment the guard makes it; every read after it — the
barrier, the fleet row, the ledger — can fail, and when one does the held claim
is built from *that* decision. Nothing evaluates the guard a second time on the
failure path, because a second evaluation can fail in its own way and release a
turn the first had already established as the runtime's. A connection lookup
that raises is `guard_error` — undecidable — and never `connection_not_verified`,
which acceptance would read as a verified negative.

**Every transition validates where it writes.** `settle` re-reads the barrier
under the lock, re-checks the generation the operator decided on, recounts the
work and the pending deferred entries on that same session, and only then writes
the transition and its evidence — so the evidence describes the state that was
actually settled, and work committing between the report and the write blocks
instead of being settled over. `reopen` re-checks under the same lock that the
barrier is still the settled one it was asked about, so a drain that started in
between is never reopened over.

**Convergence is observed, and silence is not retirement.** A worker records the
generation and state **it read**, passed in by the worker itself rather than
re-derived when the row is written — a heartbeat that arrives after a drain
still says what that worker saw. The expected set is every worker row that has
not been retired, reconciled — inside `settle` and `release` themselves, not
only in the operator's report — against the deployment inventory the operator
states; a replica in the inventory that never wrote a row blocks, and an
unstated inventory blocks. A worker that stops reporting is *stale* and blocks,
and only an operator retires one, naming themselves, their reason, the
deployment, how the stop was verified, when — and handing over the platform's
own record of the stop, which is retained on the row with its digest. Shared
admission locking orders this job against the workers that take it; it does not
prove a fleet was rolled out or shut down, and the stop record is what the
operator captured rather than something this platform fetched.

**Disposition is per entry and evidenced — and bound to the entry.** Each
deferred row is named by id and disposed of individually, with a disposition
from a closed set (`replayed`, `answered`, `superseded`, `not_required`) and
evidence checked against the records before the row closes. `replayed` names
this entry's own provider message id and requires the runtime turn admitted for
it to have reached a terminal; `answered` names another turn that must be the
same tenant's, on the same channel connection, in a conversation bound to the
same customer, with a terminal recorded no earlier than this message arrived —
another conversation's terminal proves nothing about this one; `superseded`
names a later entry for the same tenant, connection and recipient, later in
arrival order — an older message replaces nothing. An entry that arrived after
the operator last looked is not in their list and is not disposed of. Disposed
and resolved rows stay as history and stop counting against the pending limit.
An `unknown` delivery outcome still blocks settlement, is never replayed over,
and is never resolved by elapsed time.

**An acknowledgement is a promise.** Both webhook entry points acknowledge first
and process in the background. For the legacy path that is right; for a
pilot-scoped message it is not, because the 200 ends the provider's retries
before anything durable exists. A pilot-scoped inbound is therefore written to
`commerce_runtime_deferred_inbound` **before** the route answers, and resolved
when its turn reaches a terminal; until then it counts as outstanding work.
When that record cannot be written the route answers `503` and spawns nothing,
so the provider redelivers the whole batch and the existing deduplication keeps
the unaffected messages in it from being processed twice. Nothing in this path
runs while the pilot is off.

Three more things the promise never covers. **A sender we could not
authenticate:** a pilot obligation is recorded and processed only when the
request's `X-Hub-Signature-256` verified — the legacy path's audit mode does not
extend to the pilot — and otherwise the request is `503 pilot_scope_unauthenticated`
with nothing recorded. **A released tenant:** after `release`, `503
pilot_released`, nothing recorded, until the switch is off. **A nonce:** replay
protection claims its nonce before anything is durable, so a process that dies
in between leaves a nonce and no record; before a nonce alone answers 200, the
route checks that every pilot-scoped message in the body is on record, and one
that is not is a first attempt. Concurrent and completed duplicates stay
idempotent — the record is written once and the dispatcher's deduplication
holds.

`scripts/operators/commerce_runtime_pilot_handover.py` is the executable
procedure — `status | drain | retire | dispose | recover | settle | release |
reopen`. `settle` and `release` exit 0 only when every one of those conditions
holds at the instant of the write; otherwise they exit 1 and name each blocker.
There is no attestation step and no attestation variable: the verdict rests on
the procedure having been executed.

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

Revisions `0108`, `0109` and `0111` create the runtime's tables but are not part
of the normal bootstrap target, so a database may legitimately not have them.
The runtime probes **all twelve** relations it uses — the three foundation
relations, the six ledger relations and the three handover relations — and
treats *absent* or *partial* as unavailable: it refuses the turn
(`runtime_schema_unavailable`) rather than running half-present. Checking fewer
would pass on a foundation-only database that has no terminals table, which is
exactly the shape revision `0108` leaves behind — or on a nine-relation database
that can admit turns but has nowhere to record an acceptance.

Readiness decides whether a turn *runs*. It does not release ownership already
established: fresh HTTP traffic on a database without the schema was never
pilot-scoped and keeps today's behaviour, while a turn the guard or a durable
acceptance record has established as the runtime's is held rather than handed
over (§3).

The probe is cached per engine so a disabled pilot costs nothing per turn. That
cache is per process and is not invalidated by applying the migration, so the
service is restarted after it; until then every turn refuses, which is the safe
direction.

Revision `0111` verifies a pre-existing relation of one of its names by
**definition**, not by name: primary-key columns, unique-constraint columns,
check-constraint expressions, foreign keys and index columns, uniqueness and
partial predicates are compared against the declared table created in a scratch
schema on the same server and reflected the same way, so both sides are spelled
by PostgreSQL. A relation with the right names over the wrong columns, a wider
check, or a non-partial "pending" index is refused rather than reconciled.
`0111` and the address revision `0110` are siblings of `0109`; rolling back the
runtime alone is `alembic downgrade 0111@-1` (§1.3 of the runbook says why the
common-ancestor spellings are wrong).

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
