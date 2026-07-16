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
`handle=None` and `bound_scope=None`.

## Atomic resolver pairing

On `resolved`, the resolver issues the handle and `bound_scope` together in one
`no_autoflush` read path. Both objects carry the same private binding key and
the same per-resolution issuance token (`secrets.token_bytes(32)`). The token
is created only inside `_issue_authoritative_a1_subject_pair` and is never
logged, serialized, or exposed through public accessors.

`AuthoritativeA1SubjectHandle.is_bound_to(scope)` and
`BoundAuthoritativeA1SubjectScope.is_bound_to(handle)` succeed only when both
the binding key and issuance token match. A known binding UUID paired with a
forged wrong-tenant scope, or a handle from an earlier resolution against a
later scope for the same binding row, must fail pairing.

External construction of either type is intentionally blocked. Callers outside
Platform resolution receive `TypeError` if they attempt to instantiate these
types directly.

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

Subject identity values must never appear in repr, serialization, logs, AI
facts, prompt context, telemetry, or customer-facing surfaces.

## Privacy and serialization

The handle and scope deliberately have no public serialization surface and their
representations carry no identifier. Pickle, copy/deepcopy, and JSON
serialization fail closed rather than retaining private binding keys or issuance
tokens. Bridge logging emits only closed status, reason, and evidence
classification.

All resolver ORM reads and canonical proof building run under `db.no_autoflush`.
The bridge therefore cannot flush or persist caller-pending writes.

## Later AI-consumer boundary

A later consumer may receive the result from a trusted routed context, verify
`handle.is_bound_to(bound_scope)`, and use `bound_scope` accessors to build its
internal query scope. It must not inspect or serialize the handle, reconstruct
a subject from other conversation fields, re-read bindings, or add handle/scope
identity to AI facts, prompt context, telemetry, or public API payloads.

This bridge intentionally does not connect to coupon resolution, compose,
dispatch, or any customer-visible path.
