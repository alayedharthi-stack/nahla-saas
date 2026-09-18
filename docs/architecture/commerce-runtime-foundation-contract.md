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
  **database clock** (`now()`), never the worker's clock.
* Every successful claim issues `lease_fence + 1`. Fences are per
  conversation, strictly increasing, and are never reset or reused by expiry,
  release, cleanup or invalidation.
* `renew` and `release` require the presented token to match the current
  owner, fence and epoch and the lease to be unexpired.

### 3.4 State revision and ownership epoch

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
  `stale_revision`; a guard failure no rule explains is `unclassified` and
  still fails closed.

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

### 3.6 UNKNOWN external outcomes never authorise blind replay

* `transport_outcome = unknown` is stored as unknown. The foundation exposes
  no replay, resend or retry operation, and a repeated admission of the same
  inbound identity resolves to the already admitted turn, which keeps its
  single terminal. Any future retry contract must present validated evidence
  bound to the exact prior attempt (the reliability gate's retry-evidence
  rule); nothing here weakens that rule.

### 3.7 No database transaction across model or network calls

* Every repository operation opens exactly one short transaction and closes
  it before returning. The foundation makes no model, provider or network
  call, so no transaction can be held across one. Callers that later compose
  or send must do so **outside** these transactions and re-present their
  token afterwards.

## 4. Boundaries of the persistence slice (accurate)

Implemented: dedicated tables and repository operations (admission, claim,
renew, release, revision-based commit, atomic terminal + state transition,
administrative invalidation), bounded and validated payloads, PostgreSQL
proofs with independent processes.

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
