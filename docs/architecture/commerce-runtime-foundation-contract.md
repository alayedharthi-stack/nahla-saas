# Commerce Runtime Foundation — ownership and state contract (dormant)

Status: **contract record, governance only.** Authorised by the owner on
2026-09-18 ("start the dormant commerce runtime foundation"). This document
formalises the agreed architecture for durable conversation ownership and
versioned state. It authorises no V1 repair, no activation, no production
migration and no change to existing governance, allowances or gate behaviour.

Companion implementation (separate PR, isolated branch): package
`backend/core/commerce_runtime/`, Alembic revision `0108`, proof module
`tests/commerce_reliability/test_commerce_runtime_foundation_pg.py`.

## 1. Origin and relationship to existing evidence

* **C0 audit and PR #1086 harness.** The reliability gate records two
  reproduced defects that this foundation is the persistence substrate for:
  `UC-01` (a V2-owned turn whose provider rejection ends as inferred
  `end_ok` with no delivery record) and `UC-02` (concurrent
  `DefaultStateStore.save` calls lose an independently changed field). Their
  replacement contracts `RC-01` and `RC-02` are tracked separately in
  `tests/commerce_reliability/reliability_manifest.json` and remain
  *unapproved future decisions*. The allowances, signatures, owners and
  expiries of UC-01 / UC-02 are unchanged by this contract and by the
  foundation implementation; passing the new foundation tests does not mean
  the legacy defects are fixed.
* **Merged base.** `main` at `5a339e5d2ae4c317fe0e38425d7e98f1e16da47c`
  (PR #1087 merged). No open PR touches the files named above.

## 2. Standing V1 freeze (recorded, not changed)

The owner's standing decision is that frozen V1 browse / compose paths are
not repaired without a separate, scoped authorisation
(`docs/engineering/commerce-reliability-gate.md` §3). Every recorded
allowance can only be retired through (a) a separately authorised, scoped
repair of the affected path, or (b) a verified replacement **followed by
retirement of the affected legacy path**. Passing replacement tests alone is
insufficient while the defective path remains active. RB-05's intended
fulfilment / discovery behaviour remains a pending product decision.

## 3. Contract

### 3.1 Tenant, conversation and namespace

* Every row and every operation is scoped by `tenant_id` (foreign key to
  `tenants.id`) **and** a `namespace` from the closed set `live` | `shadow`.
  A conversation is identified by `(tenant_id, namespace, conversation_ref)`;
  `conversation_ref` is an opaque, bounded reference (≤ 128 printable,
  whitespace-free characters).
* `shadow` rows are for synthetic or replayed work; `live` rows are for
  execution. The two never share rows, sequences, leases or terminals, and a
  token issued in one namespace cannot act in the other.
* A foreign tenant receives the same "not found" answer as a missing row; the
  foundation never reveals another tenant's data or existence.

### 3.2 Durable inbound identity and per-conversation receive order

* Inbound admission identity is `(tenant_id, namespace,
  channel_connection_ref, provider_message_id)`: the authenticated tenant
  channel connection plus the provider's message identity. The identity is
  unique in the database; a repeated admission returns the existing turn as a
  duplicate and consumes no sequence number; the same identity presented for
  a different conversation is an explicit `AdmissionConflict`.
* Each admitted turn receives the conversation's next `sequence`, assigned
  atomically under the conversation row lock in the same transaction as the
  insert; `(conversation_id, sequence)` is unique. Concurrent admissions to
  the same conversation therefore yield contiguous, unique order; different
  conversations do not serialise against each other.

### 3.3 Lease owner, database-time expiry, monotonic fencing token

* A worker obtains exclusive ownership by a `claim`: allowed only when no
  lease exists or the existing lease has expired according to the
  **database wall clock** (`clock_timestamp()`), never the worker's clock.
* The clock is sampled **after the conversation row lock has been acquired**
  and again inside every guarded UPDATE. `now()` (the transaction start
  time) is never used for validity or expiry: a connection that waited on
  the row lock across a lease expiry must see that expiry, an expired holder
  that finishes waiting is refused, and a claimant that finishes waiting
  takes over an expired lease and receives an expiry measured from that
  moment, never one that has already passed.
* Every successful claim issues `lease_fence + 1`. Fences are per
  conversation, strictly increasing, and are never reset or reused by expiry,
  release, cleanup or invalidation.
* `renew`, `release`, state commits and terminal records require the
  presented token to match the current owner, fence and epoch and the lease
  to be unexpired **at the guarded mutation itself**, on that same clock.

### 3.4 State revision, ownership epoch and scope-bound tokens

* An ownership token carries `owner_id`, `fence`, `epoch` **and the scope it
  was issued for: `tenant_id`, `namespace`, `conversation_id`**. Every
  token-bearing operation validates the complete binding before reading or
  writing anything; a token presented for another conversation, namespace or
  tenant is refused (`ScopeMismatch`) even when owner, fence and epoch would
  match. For a terminal record the binding is compared with the scope derived
  from the turn row, not with the caller's arguments alone.
* State is committed by compare-and-set: the caller presents
  `expected_revision`; success stores `expected_revision + 1` with the new
  payload and records the committing fence and epoch. A stale revision is an
  explicit `StateConflict(stale_revision)`, never a silent merge or discard.
* `ownership_epoch` advances when ownership is invalidated: taking over a
  lease that expired without release, or an administrative
  `invalidate_ownership`. Any token from an earlier epoch is refused
  (`obsolete_epoch`) even if its fence is still the highest.
* Rejection reasons are exact and ordered: `superseded_fence`,
  `invalid_fence`, `obsolete_epoch`, `stale_owner`, `expired_lease`,
  `stale_revision`, `turn_not_eligible`; a guard failure no rule explains is
  `unclassified` and still fails closed.

### 3.5 Processing completion ≠ transport outcome ≠ customer reach

* Each turn has **at most one immutable terminal record** (primary key on
  `turn_id`, plus a database trigger that refuses UPDATE and DELETE). It
  carries three separate facts: `processing_outcome` (`completed` | `failed`
  | `abandoned`), `transport_outcome` (`accepted` | `rejected_definitive` |
  `unknown` | `not_attempted`) and `customer_reach` (`reached` |
  `not_reached` | `unknown` | `not_applicable`).
* Recording a terminal requires a valid ownership token and may be combined
  atomically with a state transition; either both persist or neither does.
  A crash before commit leaves no partial transition.

### 3.5a Ordered processing at the repository boundary

* The **eligible turn** of a conversation is its oldest admitted turn that
  has no terminal record. Receive sequencing alone does not define ordered
  processing; this rule does.
* State commits and terminal records are bound to the eligible turn: a
  commit or a terminal for any other turn is refused with
  `turn_not_eligible`, so sequence 2 cannot be finalised while sequence 1 is
  unresolved. A claim may name the turn it intends to process and is refused
  when that turn is not the eligible one; every claim reports the eligible
  turn at claim time. Eligibility advances only when a terminal is recorded,
  whatever its processing outcome.
* Scheduling (which worker claims which conversation, and when) is outside
  this slice: the repository enforces the order for whoever calls it. No
  scheduler, worker, webhook integration or external-effect executor is
  built or activated here.

### 3.6 UNKNOWN external outcomes never authorise blind replay

* `transport_outcome = unknown` is stored as unknown. The foundation exposes
  no replay, resend or retry operation, and a repeated admission of the same
  inbound identity resolves to the already admitted turn, which keeps its
  single terminal. Any future retry contract must present validated evidence
  bound to the exact prior attempt (the reliability gate's retry-evidence
  rule); nothing here weakens that rule.

### 3.7 No database transaction across model or network calls

* Every repository operation runs **one write transaction**, committed or
  rolled back before it returns. Two operations may open **one further
  read-only transaction** after their write transaction rolled back on a
  database conflict, to report the committed truth: admission after an
  inbound-identity race, and terminal recording after a terminal
  primary-key race. No operation opens more than those.
* The foundation makes no model, provider or network call, so no transaction
  is ever held across one. Callers that later compose or send must do so
  **outside** these transactions and re-present their token afterwards.

## 4. Boundaries of the persistence slice (accurate)

Implemented: dedicated tables and repository operations (admission, claim,
renew, release, revision-based commit bound to the eligible turn, atomic
terminal + state transition of the eligible turn, administrative
invalidation), scope-bound ownership tokens, wall-clock lease validity after
lock acquisition, bounded and validated payloads, a migration that verifies
any pre-existing schema by definition, PostgreSQL proofs with independent
processes and connections.

Not implemented and not claimed: production delivery, order mutation,
exactly-once external effects, provider adapters, search replacement,
callbacks, webhook or worker wiring, application startup registration,
activation flags, live state writes, production migration execution. The
package joins no live request path; its tables exist only where revision
`0108` is applied on purpose (production startup pins `alembic upgrade 0093`
and materialises only the application `models.Base`, which this package does
not join).

## 5. Governance

* GOV-001 flags for both PRs: `INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE`,
  `MODEL_CHANGED=NO`, `PROMPT_CHANGED=NO`, `PERSONA_CHANGED=NO`,
  `PHRASE_MAP_CHANGED=NO`, `KEYWORD_ROUTER_CHANGED=NO`,
  `CUSTOMER_REGEX_CHANGED=NO`.
* GOV-002: `backend/core/commerce_runtime/` is outside every semantic, model,
  prompt, persona, ownership and runtime-AI prefix the trusted scanner
  guards; the trusted-base scanner reports no findings for either PR.
* Constitution: the foundation emits no customer-facing text.
* Migration head contract: the eight files that pin the repository's Alembic
  head set (`scripts/operators/bootstrap_migration_contract.py` and seven
  test modules) advance `0107 → 0108` under explicit owner authorisation;
  the bootstrap target stays `0093` and the two intentional parallel
  branches (`0092`, `0108`) are not joined.
* Reliability gate: manifest, runner, baselines, owners, expiries,
  signatures and `ci.yml` are untouched. CI wiring of the gate remains the
  separate governance-only change (Variant B: both tiers inside
  `lint-and-test` with a disposable PostgreSQL service).

## 6. Dependency order

1. **This document** (governance / documentation PR) records the contract
   and the freeze. It has no code.
2. **Persistence foundation PR** implements §3 on an isolated branch. It was
   built and validated independently of this PR (it does not import anything
   from it), so validation does not wait on this merge; merging it *before*
   this document would leave the implemented contract unrecorded, so the
   recommended review order is this document first.
3. Later slices (not authorised here): provider adapters, search replacement,
   callbacks, production integration, and the RC-01 / RC-02 decisions.

## 7. Proof expectations for the foundation PR

Real PostgreSQL, disposable database migrated with the repository chain,
independent spawned processes; a skipped suite is not a pass. Required
proofs: duplicate admission resolves to one durable turn; concurrent
admission preserves unique order; one owner at a time; independent
conversations; expiry permits recovery with a higher fence; an expired or
superseded worker cannot renew, commit or finalise; stale revisions cannot
overwrite; epoch changes invalidate older work; cross-tenant access is
rejected; competing completions cannot create two terminals; a crash before
commit leaves no partial transition; revision `0108` applies cleanly on a
database at the chain head `0107` and downgrades cleanly, with `0092` left
as the pre-existing second head.

Added after the independent review of 2026-09-18: a connection that waits on
the row lock across a lease expiry is refused for renew, release, commit and
finalisation, a waiting claimant takes over the expired lease with a fresh
expiry, and a claim's expiry is measured after its lock wait; the same owner
with identical fence and epoch cannot act across conversations, namespaces
or tenants, and a terminal's scope is derived from its turn; sequence 2
cannot be committed or finalised while sequence 1 is unresolved; revision
`0108` refuses, without stamping, a pre-existing schema missing the
admission uniqueness, with a changed default, with a narrowed or extra
column, with a missing check constraint, with a disabled immutability
trigger, or with a same-named function of a different body, and installs
its trigger on the runtime terminal relation even when an unrelated table
carries a trigger of the same name.
