# Commerce runtime — effect and delivery ledgers (contract addendum)

Status: recorded 2026-09-19 as the contract of the second dormant slice.
Extends `commerce-runtime-foundation-contract.md`; nothing here reopens the
closed findings B1, B2, B3, processing order, database-target enforcement or
report freshness. Authority for the implementation in
`backend/core/commerce_runtime/{ledger_contracts,ledger_models,ledgers}.py`
and revision `0109`.

## 1. Purpose and boundary

The slice records, durably and honestly, what the runtime *intends* to do
outside itself (external mutations and outbound delivery), what it *reserved*
for dispatch, and what *evidence* exists about the outcome, so that recovery
after an uncertain outcome can never blindly execute the same business action
twice and never claims a delivery that is not proven.

It is dormant: no worker, scheduler, webhook, provider adapter, model call,
activation flag or production migration execution exists in this slice. It
registers no upstream idempotency guarantee and no production reconciliation
source. Local uniqueness proves that at most one dispatch was **reserved**;
it never proves that a provider executed an action exactly once.

## 2. Business-action identity (effects)

* An **effect** is reserved before any attempted execution. It is bound to
  the authenticated tenant, the runtime namespace, the conversation, the
  originating turn (the eligible turn at reservation), an action type from a
  closed vocabulary, and a **business idempotency key** that is unique per
  tenant and namespace.
* The key is derived from what the action is (order reference, coupon code,
  payment reference), never from a provider tool-call id;
  `derive_business_key` renders that rule.
* Reserving the same key with the same action type, payload hash and
  conversation returns the existing record, whatever its status, so a
  reasoning retry or a provider failover reuses a confirmed result. The same
  key with a different action, payload or conversation is an explicit
  `EffectConflict`; nothing is written.
* Reservation requires a valid scoped ownership token (foundation rules:
  fence, epoch, owner, database-clock lease, scope binding) and the
  originating turn must be the eligible turn.

## 3. Dispatch boundaries

* A **dispatch attempt** is a durable, append-only row with its own identity
  (`dispatch_key`) created by `reserve_effect_dispatch` /
  `reserve_delivery_dispatch`. Reservation re-checks authorization on the
  database clock inside the transaction and requires the originating turn to
  still be eligible; authority at intent time alone dispatches nothing.
* The external call is performed by the caller **after** the reservation
  transaction committed and **before** the result transaction opens. No
  transaction is held across an external call.
* An effect is dispatched at most once (`MAX_EFFECT_ATTEMPTS = 1`). A
  delivery sequence has at most two attempts (`MAX_DELIVERY_ATTEMPTS = 2`).
* Lease expiry, takeover or administrative invalidation never makes an
  attempt whose completion is not established, or whose outcome is unknown,
  eligible for redispatch: every owner, including the new one, receives
  `DispatchBlocked` / `DeliveryDispatchBlocked` with the exact reason and the
  open attempt.

## 4. Honest outcome semantics

Effect status projection (append-only results are the evidence):

| Status | Meaning |
| --- | --- |
| `reserved` | reserved, not dispatched |
| `dispatching` | dispatch started; completion not yet established |
| `confirmed` | confirmed success; `confirmed_result` is reusable |
| `rejected` | definitive rejection or failure; final |
| `unknown` | unknown external outcome; retained until evidence |

Legal transitions: `reserved → dispatching`; `dispatching → confirmed |
rejected | unknown`; `unknown → confirmed | rejected` (late evidence for the
**same** attempt, once). `confirmed` and `rejected` are final. A confirmed
result cannot be overwritten and is never replayed as a fresh action.
Contradictory or out-of-order evidence is an `IllegalTransition`; an
identical repeat returns the existing row.

Recording evidence for an existing attempt is **scope-bound, not
lease-bound**: it validates tenant, namespace, conversation and attempt, so
a worker whose ownership ended can still attach the provider's late answer
to its own attempt, while it can create no new effect or attempt. The
absence of a local success record is never treated as proof that nothing
happened: after a crash the recovering owner records `unknown`, not
"not executed".

## 5. Processing, transport and reach stay separate

* **Turn processing completion** is the foundation's immutable terminal.
* **External-action outcome** lives in the effect ledger.
* **Delivery transport outcome** lives in the delivery ledger: `accepted`
  (provider accepted the send **and** returned a valid provider message id),
  `rejected` (definitive; nothing sent), `unknown` (timeout, HTTP success
  without a message id, 5xx or ambiguous response).
* **Customer reach** is evidence only: `delivered` / `read` receipts bound
  to the accepted provider message id. Acceptance alone leaves reach
  `unknown`; a `failed` receipt after acceptance makes it `not_reached`.
* `finalize_turn` derives the terminal's transport outcome and customer reach
  from the ledgers under the same conversation lock, refuses
  (`CompletionBlocked`) while any attempt has no established outcome, and
  records the summary in the terminal's `details.ledger`. Evidence recorded
  later updates the ledgers only; the terminal never changes.
* A handoff request is a request. `human_transfer_established` is true only
  for a confirmed `handoff_request` whose result carries explicit transfer
  evidence (`transfer.human_owner_ref`, `transfer.accepted_at`); a
  `needs_human` flag or a recorded request never proves a completed human
  ownership transfer, and the runtime's lease is untouched by it.

## 6. Logical delivery sequences

* At most one delivery sequence per turn (`UNIQUE (turn_id)`), reserved with
  its intent (kind `rich` or `text`, payload) by the eligible turn's owner.
* Attempt 1 dispatches the intent. Only a **proven definitive rejection** of
  a **rich** first attempt permits one **text** recovery attempt
  (`reserve_delivery_recovery`). An accepted or unknown send permits no
  retry, no fallback and no alternate provider; a pending attempt permits
  nothing; a text first attempt has no recovery; two attempts exhaust the
  sequence.
* Receipts are append-only per attempt: one outcome receipt (`accepted`,
  `rejected`, `unknown`; `unknown` may be resolved once), then reach
  receipts (`delivered`, `read`, `failed`) bound to the accepted id.

## 7. Atomicity

* `commit_turn_decision` persists, in one transaction, an optional
  compare-and-set state transition, every effect reservation and the delivery
  sequence reservation of the eligible turn. A crash before commit leaves no
  state change, no effect and no sequence.
* `finalize_turn` persists the terminal atomically with an optional state
  transition and the ledger-derived facts. A crash before commit leaves no
  terminal.
* Every other operation runs one write transaction; `finalize_turn` may open
  one read-only transaction after a terminal primary-key race, exactly like
  the foundation's `record_terminal`.

## 8. Schema (revision 0109, revises 0108)

Six tables on `RuntimeBase`, outside the application `Base` and outside
startup imports: `commerce_runtime_effects`, `commerce_runtime_effect_attempts`,
`commerce_runtime_effect_results`, `commerce_runtime_delivery_sequences`,
`commerce_runtime_delivery_attempts`, `commerce_runtime_delivery_receipts`.
Attempts, results and receipts are append-only, enforced by the trigger
`trg_commerce_runtime_ledger_immutable` on each of the four relations
(function `commerce_runtime_ledger_rows_immutable`). The revision reconciles
only absent tables, absent indexes, an absent function and an absent trigger
on a ledger relation; every other pre-existing shape is verified by
definition and refused without stamping. It requires revision 0108's tables.
Heads stay `{0092, 0109}`; production bootstrap stays `0093`; the eight
head-pin files and the foundation proof advance `0108 → 0109` mechanically.

## 9. Proof expectations

Real PostgreSQL, scripted external outcomes, independent processes where
concurrency matters: concurrent duplicate reservations produce one logical
action; same key with a different payload is rejected; tenant, namespace,
conversation and turn isolation; stale lease, fence and epoch rejection;
ownership change between intent and dispatch; confirmed mutation reused
without a second execution; unknown mutation blocks replay and failover;
crash before commit leaves no partial intent, state or terminal; crash after
a durable dispatch reservation permits no blind redispatch; late evidence
attaches only to its existing attempt; definitive rejection permits one
bounded recovery; timeout or missing provider message id prevents fallback;
acceptance, delivery and read receipts remain distinct; illegal or
contradictory transitions fail explicitly; every operation closes its
transaction before returning. The strict inventory lists them as required
proofs (`commerce_runtime_ledgers`, `commerce_runtime_ledgers_migration`).

## 10. Out of scope (unchanged decisions)

No V1 repair, no provider adapters, no dispatch worker, no reconciliation
source, no activation, no production migration execution, no change to
legacy debt owners, expiries, signatures or acceptance status; UC-01 / UC-02
remain open baseline allowances of the reliability gate, which remains NOT
ACCEPTED.
