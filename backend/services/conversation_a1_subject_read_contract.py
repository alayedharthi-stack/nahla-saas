"""
Platform-owned read contract for conversation → authoritative A1 subject.

The request is constructed only from trusted routed context.  Its result is
for internal AI consumers; it is not a wire or telemetry payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID


READ_STATUS_RESOLVED = "resolved"
READ_STATUS_UNRESOLVED = "unresolved"
READ_STATUSES = frozenset({READ_STATUS_RESOLVED, READ_STATUS_UNRESOLVED})

EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE = (
    "authoritative_a1_policy_eligible"
)
READ_EVIDENCE_CLASSES = frozenset({EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE})

UNRESOLVED_REASON_INVALID_TENANT = "invalid_tenant"
UNRESOLVED_REASON_INVALID_CONVERSATION = "invalid_conversation"
UNRESOLVED_REASON_INVALID_REQUEST = "invalid_request"
UNRESOLVED_REASON_CONVERSATION_ABSENT = "conversation_absent"
UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH = "conversation_tenant_mismatch"
UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT = "active_binding_absent"
UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE = "active_binding_multiple"
UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE = "binding_not_authoritative"
UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID = "binding_subject_kind_invalid"
UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID = "binding_namespace_invalid"
UNRESOLVED_REASON_BINDING_SOURCE_INVALID = "binding_source_invalid"
UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH = (
    "subject_absent_or_tenant_mismatch"
)
UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE = "canonical_proof_unavailable"
UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE = "capability_policy_unavailable"
UNRESOLVED_REASON_READ_UNAVAILABLE = "read_unavailable"

UNRESOLVED_REASONS = frozenset({
    UNRESOLVED_REASON_INVALID_TENANT,
    UNRESOLVED_REASON_INVALID_CONVERSATION,
    UNRESOLVED_REASON_INVALID_REQUEST,
    UNRESOLVED_REASON_CONVERSATION_ABSENT,
    UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
    UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE,
    UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID,
    UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID,
    UNRESOLVED_REASON_BINDING_SOURCE_INVALID,
    UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH,
    UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_READ_UNAVAILABLE,
})


@dataclass(frozen=True)
class TrustedConversationA1SubjectReadRequest:
    """Concrete routed identifiers; never construct from inbound content."""

    tenant_id: int
    conversation_id: int


class AuthoritativeA1SubjectHandle:
    """
    Opaque internal capability token.

    The binding key remains private to the Platform layer and this type has no
    public serialization method or identifier-bearing representation.
    """

    __slots__ = ("_binding_key",)

    def __init__(self, binding_key: UUID) -> None:
        self._binding_key = binding_key

    def __repr__(self) -> str:
        return "AuthoritativeA1SubjectHandle()"

    def __reduce__(self):
        raise TypeError("AuthoritativeA1SubjectHandle is not serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("AuthoritativeA1SubjectHandle is not serializable")

    def __copy__(self):
        raise TypeError("AuthoritativeA1SubjectHandle is not serializable")

    def __deepcopy__(self, memo):
        raise TypeError("AuthoritativeA1SubjectHandle is not serializable")


@dataclass(frozen=True)
class ConversationA1SubjectReadResult:
    status: str
    handle: Optional[AuthoritativeA1SubjectHandle] = None
    reason: Optional[str] = None
    evidence_class: Optional[str] = None


__all__ = [
    "AuthoritativeA1SubjectHandle",
    "ConversationA1SubjectReadResult",
    "EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE",
    "READ_EVIDENCE_CLASSES",
    "READ_STATUS_RESOLVED",
    "READ_STATUS_UNRESOLVED",
    "READ_STATUSES",
    "TrustedConversationA1SubjectReadRequest",
    "UNRESOLVED_REASONS",
    "UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT",
    "UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE",
    "UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID",
    "UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE",
    "UNRESOLVED_REASON_BINDING_SOURCE_INVALID",
    "UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID",
    "UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE",
    "UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE",
    "UNRESOLVED_REASON_CONVERSATION_ABSENT",
    "UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH",
    "UNRESOLVED_REASON_INVALID_CONVERSATION",
    "UNRESOLVED_REASON_INVALID_REQUEST",
    "UNRESOLVED_REASON_INVALID_TENANT",
    "UNRESOLVED_REASON_READ_UNAVAILABLE",
    "UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH",
]
