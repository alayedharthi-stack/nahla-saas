# Commerce runtime — effect and delivery ledgers (contract addendum)

Status: recorded 2026-09-19 as the contract of the second dormant slice;
corrected the same day after the independent ledger review (L1 completion
boundary, L2 key encoding, evidence trust boundary, receipt semantics) and
again for the final bounded completion correction (schema state: standalone
0108 / partial / complete, reservation against completion, exact duplicate
semantics of results and receipts).
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
  `derive_business_key` renders that rule with encoding **`k1`**:
  `<action>:k1:<c1>:<c2>...` where every string component is percent-escaped
  (`%` → `%25`, `:` → `%3A`) before joining, so `["a:b", "c"]` and
  `["a", "b:c"]` are different identities and so are different component
  counts. Integers render as decimal digits; empty strings, booleans and
  `None` are refused. When the encoded key exceeds the 128-character bound it
  becomes `<action>:k1#sha256:<hex>` over the encoded string; the `#` after
  the version cannot occur in the plain form, so the forms never collide.
  Identical components always reproduce the identical key. A different
  encoding is a new version marker, never a silent change of `k1`.
* The repository cannot authenticate a key's business meaning: two callers
  that derive different keys for the same real-world action create two
  effects. Deriving keys from business identifiers is the caller's contract.
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
* Dispatch requires the originating turn to still be the eligible turn, and
  a turn's terminal ends its eligibility. So that completion can never strand
  reserved work, the **completion boundary** (section 7) refuses a terminal
  while an intent of the turn is reserved but not dispatched or an attempt
  has no established outcome. The supported order is
  prepare → reserve dispatch → record outcome → finalize. This slice offers
  no cancellation, transfer, scheduler or post-terminal dispatch
  authorization: retained work is dispatched and resolved, or the turn stays
  open.

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
identical repeat returns the existing row. A `reserved` effect is retained
work: its turn cannot be completed until it is dispatched and its outcome
recorded (section 7).

Recording evidence for an existing attempt is **scope-bound, not
lease-bound**: it validates tenant, namespace, conversation and attempt, so
a worker whose ownership ended can still attach the provider's late answer
to its own attempt, while it can create no new effect or attempt. The
absence of a local success record is never treated as proof that nothing
happened: after a crash the recovering owner records `unknown`, not
"not executed".

**Evidence trust boundary.** `record_effect_result` and
`record_delivery_receipt` are internal persistence APIs that trust their
caller. Scope validation binds a record to a tenant, namespace, conversation
and attempt; it authenticates neither the recorder (`recorded_by` is a
caller-supplied label) nor the provider event behind the evidence. A
correctly scoped caller can record `confirmed` with empty evidence. Nothing
in this slice proves that an external event occurred; authenticating
external evidence and correlating incoming provider receipts are the
responsibility of a future trusted adapter that owns the provider boundary.

**Duplicate semantics (exact).** The two evidence ledgers keep different,
each safe, duplicate rules; neither ever downgrades an established outcome.

* *Effect results* (`record_effect_result`): an identical repeat of the
  attempt's **latest** result (same outcome, same evidence) returns that
  row. Replaying an older `unknown` result after `confirmed` or `rejected`
  was established is refused (`IllegalTransition`) and leaves the
  established status and `confirmed_result` intact. Any other result for an
  established attempt, and any second `unknown`, is refused; only
  `confirmed` or `rejected` evidence may resolve an `unknown` once.
* *Delivery receipts* (`record_delivery_receipt`): an **exact historical
  duplicate** of any receipt already recorded for the attempt (same kind,
  same provider message id, same evidence) returns that existing receipt,
  including an older `unknown` receipt whose send was later resolved; the
  sequence's established outcome is not touched, so nothing is downgraded.
  **Changed evidence** is refused: a transport receipt for an attempt whose
  outcome is established (other than the one-time resolution of `unknown` by
  `accepted` or `rejected`), a reach receipt of a kind already recorded with
  other evidence, and a reach receipt naming a provider message id other
  than the accepted send's, are all `IllegalTransition`; nothing is merged.
* A reach receipt that omits the provider message id is bound to the
  accepted send's id: that is an internal association, not an independent
  correlation of an incoming receipt.

## 5. Processing, transport and reach stay separate

* **Turn processing completion** is the foundation's immutable terminal.
* **External-action outcome** lives in the effect ledger.
* **Delivery transport outcome** lives in the delivery ledger: `accepted`
  (provider accepted the send **and** returned a valid provider message id),
  `rejected` (definitive; nothing sent), `unknown` (timeout, HTTP success
  without a message id, 5xx or ambiguous response).
* **Customer reach** is evidence only: `delivered` / `read` receipts bound
  to the accepted provider message id. Acceptance alone leaves reach
  `unknown`. A `failed` receipt after acceptance makes reach `not_reached`
  only when no `delivered` or `read` evidence exists; established
  delivered/read evidence is retained and a later `failed` receipt does not
  downgrade it.
* `finalize_turn` derives the terminal's transport outcome and customer reach
  from the ledgers under the same conversation lock, is subject to the
  completion boundary of section 7, and records the summary in the
  terminal's `details.ledger`. A recorded `unknown` is retained in that
  summary as unknown, never as success. Evidence recorded later updates the
  ledgers only; the terminal never changes.
* A handoff request is a request. `human_transfer_established` is true only
  for a confirmed `handoff_request` whose result carries explicit transfer
  evidence (`transfer.human_owner_ref`, `transfer.accepted_at`); a
  `needs_human` flag or a recorded request never proves a completed human
  ownership transfer, and the runtime's lease is untouched by it. The helper
  checks the supplied fields only; their authenticity is the trusted
  caller's responsibility, not proof of an external event.

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
* **Completion boundary (both terminal entry points).** The foundation's
  terminal path enforces it under the conversation lock, before any write,
  on a database whose ledger schema is complete: a terminal is refused
  (`CompletionBlocked`, `actionable_work_remains`) while an effect or
  delivery intent of the turn is reserved but not dispatched or an attempt
  has no established outcome; a refused completion writes nothing, not even
  its state transition. A recorded `unknown` and a definitive rejection are
  established outcomes and do not block. The foundation's own
  `record_terminal`, which records caller-supplied transport and reach, is
  refused outright for a ledger-bearing turn (`ledger_bearing_turn`): such
  turns complete only through the ledger-derived path. Turns without ledger
  records keep the foundation's behaviour unchanged.
* **Schema state (standalone 0108, partial, complete).** Before any write
  the guard classifies the database over every ledger relation of revision
  `0109` (the six ledger tables), by name. *Absent* (no ledger relation at
  all) is the standalone `0108` foundation schema: nothing is consulted and
  the foundation's behaviour is unchanged. *Complete* (all six present)
  applies the ledger-aware rules above. *Partial* (some present, some
  missing) cannot establish whether the turn's obligations are complete, so
  every terminal write, for every turn and on both entry points, is refused
  (`LedgerSchemaIncomplete`, naming the missing and the present relations)
  before any state or terminal write; retained obligations stay where they
  are. Nothing repairs the schema automatically, and no cancellation,
  transfer or post-terminal dispatch is offered; once the schema is
  restored, the ledger-aware rules apply again and retained work completes
  through the supported lifecycle.
* **Reservation against completion.** Both take the conversation row lock,
  so on independent connections they resolve in lock-acquisition order:
  when the reservation wins, the completion runs after it, sees the new
  obligation and is refused (`actionable_work_remains`); when the
  completion wins, the terminal ends the turn's eligibility and the waiting
  reservation is refused (`turn_not_eligible`) and inserts nothing into the
  completed turn.
* Every other operation runs one write transaction; `finalize_turn` may open
  one read-only transaction after a terminal primary-key race, exactly like
  the foundation's `record_terminal`.
* **Caller precondition (additive).** `commit_turn_decision` accepts an
  optional `precondition(conn, snapshot)` evaluated after the conversation row
  lock, the ownership guard and the eligibility check, and before any write of
  that transaction. Its snapshot carries the database clock read after every
  lock wait, so a caller whose work has a deadline can refuse a reservation
  that became invalid while it waited; raising aborts the transaction and
  writes nothing. Omitting it preserves the previous behaviour exactly, and it
  weakens no ledger rule: the ownership, eligibility, revision and
  one-sequence-per-turn guarantees are unchanged and still enforced first.

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
transaction before returning; reserved intents and pending attempts block
premature completion on both terminal entry points while turns without
ledger records keep the foundation path; distinct business identities with
equal payloads stay distinct; a partial ledger schema fails closed in both
directions (delivery parent relation unavailable with reserved and
dispatching effects, effects parent relation unavailable with a reserved
delivery) on both entry points, leaving state and terminal unchanged, and
the retained work completes once the relation is restored; the standalone
`0108` schema keeps the foundation terminal available; a reservation and a
completion racing on independent connections resolve in both lock orders
(reservation first: the completion is refused on the new obligation;
completion first: the reservation is refused as not eligible and inserts
nothing). The strict inventory lists them as required proofs
(`commerce_runtime_ledgers`, `commerce_runtime_ledgers_migration`).

## 10. Out of scope (unchanged decisions)

No V1 repair, no provider adapters, no dispatch worker, no reconciliation
source, no activation, no production migration execution, no change to
legacy debt owners, expiries, signatures or acceptance status; UC-01 / UC-02
remain open baseline allowances of the reliability gate, which remains NOT
ACCEPTED.
