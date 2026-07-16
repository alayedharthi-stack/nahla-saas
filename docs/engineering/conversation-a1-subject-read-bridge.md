# Conversation A1-subject read bridge

## Ownership

The Platform Identity Bridge owns the read contract in
`backend/services/conversation_a1_subject_read_*`. It accepts only
`TrustedConversationA1SubjectReadRequest(tenant_id, conversation_id)` created
from trusted routed context and reads the authoritative binding written by PR1.

It never derives subject authority from a phone number, provider/inbound
metadata, message content, `Conversation.customer_id`, or an external ID.

## Closed outcomes

The bridge returns an opaque `AuthoritativeA1SubjectHandle` only when exactly
one active binding has an authoritative evidence class, canonical
subject-kind/namespace/source pairing, a tenant-matching canonical subject, and
a policy-eligible A1 safe proof. All other outcomes are `unresolved` with one
of the closed reason codes in `conversation_a1_subject_read_contract.py`.

The handle deliberately has no public serialization surface and its
representation carries no identifier. Pickle, copy/deepcopy, and dataclass
recursive serialization fail closed rather than retaining its private binding
key. Bridge logging emits only closed status, reason, and evidence
classification.

All resolver ORM reads and canonical proof building run under `db.no_autoflush`.
The bridge therefore cannot flush or persist caller-pending writes.

## Later AI-consumer boundary

A later consumer may receive the result from a trusted routed context and pass
the opaque handle to a Platform-owned capability operation. It must not inspect
or serialize the handle, reconstruct a subject from other conversation fields,
or add it to AI facts, prompt context, telemetry, or public API payloads.

This PR intentionally does not connect the bridge to coupon resolution,
compose, dispatch, or any customer-visible path.
