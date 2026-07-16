# Conversation A1-subject read bridge

## Ownership

The Platform Identity Bridge owns the read contract in
`backend/services/conversation_a1_subject_read_*`. It accepts only
`TrustedConversationA1SubjectReadRequest(tenant_id, conversation_id)` created
from trusted routed context and reads the authoritative binding written by PR1.

It never derives subject authority from a phone number, provider/inbound
metadata, message content, `Conversation.customer_id`, or an external ID.

## Closed outcomes

The bridge returns an opaque `AuthoritativeA1SubjectHandle` and an in-process
`BoundAuthoritativeA1SubjectScope` only when exactly one active binding has an
authoritative evidence class, canonical subject-kind/namespace/source pairing,
a tenant-matching canonical subject, and a policy-eligible A1 safe proof. All
other outcomes are `unresolved` with one of the closed reason codes in
`conversation_a1_subject_read_contract.py`. Unresolved results always carry
`handle=None` and `bound_scope=None` (no proof snapshot).

## Atomic resolver pairing

On `resolved`, the resolver issues the handle, `bound_scope`, and
`BoundAuthoritativeA1PolicyProofSnapshot` together in one `no_autoflush` read
path. All three carry the same private binding key and the same per-resolution
issuance token (`secrets.token_bytes(32)`). The token is created only inside
`_issue_authoritative_a1_subject_pair` and is never logged, serialized, or
exposed through public accessors.

`AuthoritativeA1SubjectHandle.is_bound_to(scope)`,
`BoundAuthoritativeA1SubjectScope.is_bound_to(handle)`, and
`BoundAuthoritativeA1PolicyProofSnapshot.is_bound_to(handle|scope)` succeed
only when both the binding key and issuance token match. A known binding UUID
paired with a forged wrong-tenant scope, or a handle from an earlier resolution
against a later scope for the same binding row, must fail pairing.

External construction of any of these types is intentionally blocked. Callers
outside Platform resolution receive `TypeError` if they attempt to instantiate
them directly.

## `bound_scope` (trusted in-process only)

`ConversationA1SubjectReadResult.bound_scope` is not a wire, telemetry, facts,
or public API payload. It exists so trusted in-process consumers can build
repository query keys from the exact binding and canonical proof already
evaluated by the resolver, without re-querying `ConversationA1SubjectBinding`
or rebuilding A1 proof.

Trusted consumers may call only the explicit accessor methods:

- `tenant_id()`, `conversation_id()`
- `subject_kind()`, `identity_namespace()`
- `binding_source()`, `binding_evidence_class()`
- `internal_customer_id()` or `external_customer_profile_id()` (private subject
  identity for in-process repository filters only)
- `proof_subject_kind()`, `proof_identity_namespace()`,
  `proof_policy_eligibility_ready()`
- `proof_snapshot()` → `BoundAuthoritativeA1PolicyProofSnapshot`

Subject identity values must never appear in repr, serialization, logs, AI
facts, prompt context, telemetry, or customer-facing surfaces.

## `BoundAuthoritativeA1PolicyProofSnapshot`

On successful resolution only, the resolver materializes a privacy-safe,
in-memory categorical snapshot from the **exact** canonical proof already
evaluated in that same read. The snapshot is bound to the same issuance token
as the handle and scope. There is no snapshot on unresolved or error outcomes.

Safe snapshot accessors (closed categorical fields only):

- `subject_kind()`, `identity_namespace()`
- `policy_eligibility_ready()`
- `authoritative_source_history_completeness()` — `complete` or `incomplete`
- `forward_sync_health()` — `healthy`, `degraded`, or `stale`

The snapshot deliberately does **not** expose raw IDs, refs, provider values,
timestamps, phones, order counts, watermark flags, coverage scope claims, or
arbitrary proof payload. Repr, pickle, copy/deepcopy, and JSON serialization
fail closed.

## Cost contract

One successful resolution uses exactly:

1. One `ConversationA1SubjectBinding` lookup (within the existing read path)
2. One canonical A1 proof build (`build_safe_internal_customer_proof` or
   `build_safe_external_profile_proof`)

The proof snapshot is derived in-process from that single proof evaluation.
Consumers must not call proof builders again for the same turn.

All resolver ORM reads and canonical proof building run under `db.no_autoflush`.
The bridge therefore cannot flush or persist caller-pending writes.

## Privacy and serialization

The handle, scope, and proof snapshot deliberately have no public serialization
surface and their representations carry no identifier. Pickle, copy/deepcopy, and
JSON serialization fail closed rather than retaining private binding keys or
issuance tokens. Bridge logging emits only closed status, reason, and evidence
classification.

## AI-consumer boundary

A trusted consumer may receive the result from routed context, verify
`handle.is_bound_to(bound_scope)`, and use `bound_scope` accessors plus
`bound_scope.proof_snapshot()` for conditional eligibility gates.

**Consumers must use snapshot accessors and must never rebuild canonical A1
proof for the same turn.** Do not call `build_safe_internal_customer_proof` or
`build_safe_external_profile_proof` after a successful bridge resolution when
the snapshot already carries the categorical completeness and sync-health
signals needed for loader gates.

Consumers must not inspect or serialize the handle or snapshot, reconstruct a
subject from other conversation fields, re-read bindings, or add handle/scope
identity to AI facts, prompt context, telemetry, or public API payloads.

This bridge intentionally does not connect to coupon resolution, compose,
dispatch, or any customer-visible path.

## Customer-text provenance

This bridge emits no customer-facing text. It produces only closed internal
status/reason/evidence classification for logging and in-process trusted
handles. Any future consumer that uses snapshot fields for conditional-coupon
facts must keep those facts in the truth-surface layer (structured records with
sanitized telemetry) and must route all customer wording through LLM compose —
never through deterministic prose derived from snapshot accessors.
