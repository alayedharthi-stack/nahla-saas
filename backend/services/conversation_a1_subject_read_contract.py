"""
Platform-owned read contract for conversation → authoritative A1 subject.

The request is constructed only from trusted routed context. Its result is for
internal AI consumers; it is not a wire or telemetry payload.

On successful resolution, ``ConversationA1SubjectReadResult.bound_scope`` carries
an in-process ``BoundAuthoritativeA1SubjectScope`` tied to the exact active
binding and canonical proof evaluated in that same read. The handle, scope,
and ``BoundAuthoritativeA1PolicyProofSnapshot`` are atomically paired by the
resolver through a per-resolution issuance token. Trusted consumers use the
scope accessors to build repository query keys and the proof snapshot accessors
for conditional eligibility gates without re-reading bindings or rebuilding A1
proof. The scope and snapshot are never public payloads.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from services.order_customer_identity_contract import (
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)

if TYPE_CHECKING:
    from services.order_customer_identity_read_contract import (
        SafeExternalProfileSourceHistoryProof,
        SafeInternalCustomerSourceHistoryProof,
    )

_SNAPSHOT_COMPLETENESS_VALUES = frozenset({
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
})
_SNAPSHOT_SYNC_HEALTH_VALUES = frozenset({
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_STALE,
})


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

_ISSUANCE_SENTINEL = object()


@dataclass(frozen=True)
class TrustedConversationA1SubjectReadRequest:
    """Concrete routed identifiers; never construct from inbound content."""

    tenant_id: int
    conversation_id: int


class AuthoritativeA1SubjectHandle:
    """
    Opaque internal capability token.

    The binding key and per-resolution issuance token remain private to the
    Platform layer. This type has no public serialization surface or
    identifier-bearing representation.
    """

    __slots__ = ("_binding_key", "_issuance_token")

    def __init__(
        self,
        binding_key: UUID,
        *,
        _issuance: object = None,
        _issuance_token: bytes | None = None,
    ) -> None:
        if _issuance is not _ISSUANCE_SENTINEL or _issuance_token is None:
            raise TypeError(
                "AuthoritativeA1SubjectHandle cannot be constructed outside "
                "Platform resolution"
            )
        self._binding_key = binding_key
        self._issuance_token = _issuance_token

    def is_bound_to(self, scope: "BoundAuthoritativeA1SubjectScope") -> bool:
        """True when ``scope`` was issued with this handle in one resolution."""
        if not isinstance(scope, BoundAuthoritativeA1SubjectScope):
            return False
        return scope.is_bound_to(self)

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


class BoundAuthoritativeA1PolicyProofSnapshot:
    """
    In-process privacy-safe categorical snapshot of the canonical A1 proof
    already evaluated by Platform in one successful resolution.

    Carries only closed non-PII eligibility signals for trusted consumers.
    Never a wire, telemetry, facts, or public API payload.
    """

    __slots__ = (
        "_binding_key",
        "_issuance_token",
        "_subject_kind",
        "_identity_namespace",
        "_policy_eligibility_ready",
        "_authoritative_source_history_completeness",
        "_forward_sync_health",
    )

    def __init__(
        self,
        *,
        binding_key: UUID,
        _issuance: object = None,
        _issuance_token: bytes | None = None,
        subject_kind: str,
        identity_namespace: str,
        policy_eligibility_ready: bool,
        authoritative_source_history_completeness: str,
        forward_sync_health: str,
    ) -> None:
        if _issuance is not _ISSUANCE_SENTINEL or _issuance_token is None:
            raise TypeError(
                "BoundAuthoritativeA1PolicyProofSnapshot cannot be constructed "
                "outside Platform resolution"
            )
        if authoritative_source_history_completeness not in _SNAPSHOT_COMPLETENESS_VALUES:
            raise ValueError("invalid authoritative_source_history_completeness")
        if forward_sync_health not in _SNAPSHOT_SYNC_HEALTH_VALUES:
            raise ValueError("invalid forward_sync_health")
        self._binding_key = binding_key
        self._issuance_token = _issuance_token
        self._subject_kind = subject_kind
        self._identity_namespace = identity_namespace
        self._policy_eligibility_ready = policy_eligibility_ready
        self._authoritative_source_history_completeness = (
            authoritative_source_history_completeness
        )
        self._forward_sync_health = forward_sync_health

    def is_bound_to(
        self,
        peer: AuthoritativeA1SubjectHandle | "BoundAuthoritativeA1SubjectScope",
    ) -> bool:
        """True when ``peer`` was issued with this snapshot in one resolution."""
        if isinstance(peer, AuthoritativeA1SubjectHandle):
            return (
                peer._binding_key == self._binding_key
                and secrets.compare_digest(peer._issuance_token, self._issuance_token)
            )
        if isinstance(peer, BoundAuthoritativeA1SubjectScope):
            return (
                peer._binding_key == self._binding_key
                and secrets.compare_digest(peer._issuance_token, self._issuance_token)
            )
        return False

    def subject_kind(self) -> str:
        return self._subject_kind

    def identity_namespace(self) -> str:
        return self._identity_namespace

    def policy_eligibility_ready(self) -> bool:
        return self._policy_eligibility_ready

    def authoritative_source_history_completeness(self) -> str:
        return self._authoritative_source_history_completeness

    def forward_sync_health(self) -> str:
        return self._forward_sync_health

    def __repr__(self) -> str:
        return "BoundAuthoritativeA1PolicyProofSnapshot()"

    def __reduce__(self):
        raise TypeError("BoundAuthoritativeA1PolicyProofSnapshot is not serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("BoundAuthoritativeA1PolicyProofSnapshot is not serializable")

    def __copy__(self):
        raise TypeError("BoundAuthoritativeA1PolicyProofSnapshot is not serializable")

    def __deepcopy__(self, memo):
        raise TypeError("BoundAuthoritativeA1PolicyProofSnapshot is not serializable")


class BoundAuthoritativeA1SubjectScope:
    """
    In-process trusted-only query scope for one successful bridge resolution.

    This is not a wire, telemetry, facts, or public API payload. It carries the
    minimum subject identity needed for trusted in-process repository queries.
    Identity fields remain private and must never appear in repr, serialization,
    logs, or customer-facing surfaces.
    """

    __slots__ = (
        "_binding_key",
        "_issuance_token",
        "_tenant_id",
        "_conversation_id",
        "_subject_kind",
        "_identity_namespace",
        "_binding_source",
        "_binding_evidence_class",
        "_internal_customer_id",
        "_external_customer_profile_id",
        "_proof_subject_kind",
        "_proof_identity_namespace",
        "_proof_policy_eligibility_ready",
        "_proof_snapshot",
    )

    def __init__(
        self,
        *,
        binding_key: UUID,
        _issuance: object = None,
        _issuance_token: bytes | None = None,
        tenant_id: int,
        conversation_id: int,
        subject_kind: str,
        identity_namespace: str,
        binding_source: str,
        binding_evidence_class: str,
        proof_subject_kind: str,
        proof_identity_namespace: str,
        proof_policy_eligibility_ready: bool,
        proof_snapshot: BoundAuthoritativeA1PolicyProofSnapshot,
        internal_customer_id: Optional[int] = None,
        external_customer_profile_id: Optional[UUID] = None,
    ) -> None:
        if _issuance is not _ISSUANCE_SENTINEL or _issuance_token is None:
            raise TypeError(
                "BoundAuthoritativeA1SubjectScope cannot be constructed outside "
                "Platform resolution"
            )
        self._binding_key = binding_key
        self._issuance_token = _issuance_token
        self._tenant_id = tenant_id
        self._conversation_id = conversation_id
        self._subject_kind = subject_kind
        self._identity_namespace = identity_namespace
        self._binding_source = binding_source
        self._binding_evidence_class = binding_evidence_class
        self._internal_customer_id = internal_customer_id
        self._external_customer_profile_id = external_customer_profile_id
        self._proof_subject_kind = proof_subject_kind
        self._proof_identity_namespace = proof_identity_namespace
        self._proof_policy_eligibility_ready = proof_policy_eligibility_ready
        if not isinstance(proof_snapshot, BoundAuthoritativeA1PolicyProofSnapshot):
            raise TypeError("proof_snapshot must be BoundAuthoritativeA1PolicyProofSnapshot")
        if (
            proof_snapshot._binding_key != binding_key
            or not secrets.compare_digest(proof_snapshot._issuance_token, _issuance_token)
        ):
            raise ValueError("proof_snapshot issuance pair mismatch")
        self._proof_snapshot = proof_snapshot

    def is_bound_to(self, handle: AuthoritativeA1SubjectHandle) -> bool:
        """True when ``handle`` was issued with this scope in one resolution."""
        if not isinstance(handle, AuthoritativeA1SubjectHandle):
            return False
        return (
            handle._binding_key == self._binding_key
            and secrets.compare_digest(handle._issuance_token, self._issuance_token)
        )

    def tenant_id(self) -> int:
        return self._tenant_id

    def conversation_id(self) -> int:
        return self._conversation_id

    def subject_kind(self) -> str:
        return self._subject_kind

    def identity_namespace(self) -> str:
        return self._identity_namespace

    def binding_source(self) -> str:
        return self._binding_source

    def binding_evidence_class(self) -> str:
        return self._binding_evidence_class

    def internal_customer_id(self) -> Optional[int]:
        return self._internal_customer_id

    def external_customer_profile_id(self) -> Optional[UUID]:
        return self._external_customer_profile_id

    def proof_subject_kind(self) -> str:
        return self._proof_subject_kind

    def proof_identity_namespace(self) -> str:
        return self._proof_identity_namespace

    def proof_policy_eligibility_ready(self) -> bool:
        return self._proof_policy_eligibility_ready

    def proof_snapshot(self) -> BoundAuthoritativeA1PolicyProofSnapshot:
        """Categorical proof snapshot from the resolver's canonical evaluation."""
        return self._proof_snapshot

    def __repr__(self) -> str:
        return "BoundAuthoritativeA1SubjectScope()"

    def __reduce__(self):
        raise TypeError("BoundAuthoritativeA1SubjectScope is not serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("BoundAuthoritativeA1SubjectScope is not serializable")

    def __copy__(self):
        raise TypeError("BoundAuthoritativeA1SubjectScope is not serializable")

    def __deepcopy__(self, memo):
        raise TypeError("BoundAuthoritativeA1SubjectScope is not serializable")


def _proof_snapshot_from_evaluated_proof(
    *,
    binding_key: UUID,
    _issuance_token: bytes,
    proof: "SafeExternalProfileSourceHistoryProof | SafeInternalCustomerSourceHistoryProof",
) -> BoundAuthoritativeA1PolicyProofSnapshot:
    """Resolver-only: categorical fields from the canonical proof just evaluated."""
    return BoundAuthoritativeA1PolicyProofSnapshot(
        binding_key=binding_key,
        _issuance=_ISSUANCE_SENTINEL,
        _issuance_token=_issuance_token,
        subject_kind=str(proof.subject_kind),
        identity_namespace=str(proof.identity_namespace),
        policy_eligibility_ready=bool(proof.policy_eligibility_ready),
        authoritative_source_history_completeness=str(
            proof.authoritative_source_history_completeness
        ),
        forward_sync_health=str(proof.forward_sync_health),
    )


def _issue_authoritative_a1_subject_pair(
    *,
    binding_key: UUID,
    tenant_id: int,
    conversation_id: int,
    subject_kind: str,
    identity_namespace: str,
    binding_source: str,
    binding_evidence_class: str,
    proof: "SafeExternalProfileSourceHistoryProof | SafeInternalCustomerSourceHistoryProof",
    internal_customer_id: Optional[int] = None,
    external_customer_profile_id: Optional[UUID] = None,
) -> tuple[AuthoritativeA1SubjectHandle, BoundAuthoritativeA1SubjectScope]:
    """Resolver-only atomic pairing for one successful read."""
    issuance_token = secrets.token_bytes(32)
    proof_snapshot = _proof_snapshot_from_evaluated_proof(
        binding_key=binding_key,
        _issuance_token=issuance_token,
        proof=proof,
    )
    handle = AuthoritativeA1SubjectHandle(
        binding_key,
        _issuance=_ISSUANCE_SENTINEL,
        _issuance_token=issuance_token,
    )
    scope = BoundAuthoritativeA1SubjectScope(
        binding_key=binding_key,
        _issuance=_ISSUANCE_SENTINEL,
        _issuance_token=issuance_token,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        subject_kind=subject_kind,
        identity_namespace=identity_namespace,
        binding_source=binding_source,
        binding_evidence_class=binding_evidence_class,
        proof_subject_kind=str(proof.subject_kind),
        proof_identity_namespace=str(proof.identity_namespace),
        proof_policy_eligibility_ready=bool(proof.policy_eligibility_ready),
        proof_snapshot=proof_snapshot,
        internal_customer_id=internal_customer_id,
        external_customer_profile_id=external_customer_profile_id,
    )
    return handle, scope


@dataclass(frozen=True)
class ConversationA1SubjectReadResult:
    status: str
    handle: Optional[AuthoritativeA1SubjectHandle] = None
    bound_scope: Optional[BoundAuthoritativeA1SubjectScope] = None
    reason: Optional[str] = None
    evidence_class: Optional[str] = None


__all__ = [
    "AuthoritativeA1SubjectHandle",
    "BoundAuthoritativeA1PolicyProofSnapshot",
    "BoundAuthoritativeA1SubjectScope",
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
