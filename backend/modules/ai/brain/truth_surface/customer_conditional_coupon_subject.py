"""
customer_conditional_coupon_subject.py
──────────────────────────────────────
AI-owned safe subject-handle boundary for conditional-coupon facts.

Consumes the Platform read bridge for authoritative opaque handles, the
atomically paired ``bound_scope``, and resolver-issued proof snapshot.
Trusted routing uses only concrete ``(tenant_id, conversation_id)``; phone,
inbound metadata, message content, and ``Conversation.customer_id`` never
authorize scope.

Cost: one Platform bridge resolution (one binding query + one A1 proof build)
per subject resolve when shadow/relevance gates allow. No post-bridge binding,
proof-builder, or identity reads in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from services.conversation_a1_subject_binding_contract import (
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (
    AuthoritativeA1SubjectHandle,
    BoundAuthoritativeA1PolicyProofSnapshot,
    BoundAuthoritativeA1SubjectScope,
    READ_STATUS_RESOLVED,
    TrustedConversationA1SubjectReadRequest,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
)
from services.conversation_a1_subject_read_service import (
    resolve_authoritative_a1_subject_for_conversation,
)

from .customer_conditional_coupon_contract import (
    CUSTOMER_SCOPE_EXTERNAL,
    CUSTOMER_SCOPE_INTERNAL,
    CUSTOMER_SCOPE_UNRESOLVED,
    IDENTITY_STATUS_AMBIGUOUS,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
)

HANDLE_SOURCE_BRIDGE = "conversation_a1_subject_read_bridge"

# Closed resolver reason codes (never expose Platform bridge reason strings).
REASON_MISSING_TENANT = "missing_tenant"
REASON_HANDLE_UNAVAILABLE = "authoritative_subject_handle_unavailable"
REASON_CUSTOMER_UNVERIFIED = "customer_unverified"
REASON_SUBJECT_AMBIGUOUS = "subject_scope_ambiguous"


@dataclass(frozen=True)
class ConditionalCouponSubjectHandle:
    """Safe per-subject handle passed from upstream state — no PII."""

    subject_kind: str
    tenant_id: int
    identity_namespace: str
    handle_source: str
    customer_id: Optional[int] = None
    external_customer_profile_id: Optional[UUID] = None
    authoritative_a1_subject_handle: Optional[AuthoritativeA1SubjectHandle] = None
    bound_authoritative_a1_subject_scope: Optional[BoundAuthoritativeA1SubjectScope] = None


@dataclass(frozen=True)
class SubjectResolutionResult:
    status: str
    handle: Optional[ConditionalCouponSubjectHandle] = None
    reason_code: Optional[str] = None


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _trusted_conversation_id(conversation: Any) -> Optional[int]:
    if conversation is None:
        return None
    raw_id = getattr(conversation, "id", None)
    if not _valid_positive_int(raw_id):
        return None
    return int(raw_id)


def _map_bridge_unresolved(bridge_reason: Optional[str]) -> SubjectResolutionResult:
    if bridge_reason == UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE:
        return SubjectResolutionResult(
            status=IDENTITY_STATUS_AMBIGUOUS,
            reason_code=REASON_SUBJECT_AMBIGUOUS,
        )
    return SubjectResolutionResult(
        status=IDENTITY_STATUS_UNRESOLVED,
        reason_code=REASON_CUSTOMER_UNVERIFIED,
    )


def bound_proof_snapshot_from_handle(
    handle: ConditionalCouponSubjectHandle,
) -> Optional[BoundAuthoritativeA1PolicyProofSnapshot]:
    """
    Return the resolver-issued proof snapshot when handle, scope, and snapshot
    are mutually bound. Never rebuilds A1 proof or re-reads bindings.
    """
    if handle.handle_source != HANDLE_SOURCE_BRIDGE:
        return None
    bridge_handle = handle.authoritative_a1_subject_handle
    bound_scope = handle.bound_authoritative_a1_subject_scope
    if bridge_handle is None or bound_scope is None:
        return None
    if not bridge_handle.is_bound_to(bound_scope) or not bound_scope.is_bound_to(bridge_handle):
        return None
    snapshot = bound_scope.proof_snapshot()
    if not snapshot.is_bound_to(bridge_handle) or not snapshot.is_bound_to(bound_scope):
        return None
    if snapshot.subject_kind() != bound_scope.subject_kind():
        return None
    if snapshot.identity_namespace() != bound_scope.identity_namespace():
        return None
    return snapshot


def _coupon_handle_from_bound_pair(
    *,
    tenant_id: int,
    bridge_handle: AuthoritativeA1SubjectHandle,
    bound_scope: BoundAuthoritativeA1SubjectScope,
) -> Optional[ConditionalCouponSubjectHandle]:
    if not bridge_handle.is_bound_to(bound_scope) or not bound_scope.is_bound_to(bridge_handle):
        return None
    if int(bound_scope.tenant_id()) != int(tenant_id):
        return None

    snapshot = bound_scope.proof_snapshot()
    if not snapshot.is_bound_to(bridge_handle) or not snapshot.is_bound_to(bound_scope):
        return None
    if snapshot.subject_kind() != bound_scope.subject_kind():
        return None
    if snapshot.identity_namespace() != bound_scope.identity_namespace():
        return None

    subject_kind = bound_scope.subject_kind()
    identity_namespace = bound_scope.identity_namespace()

    if subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER:
        customer_id = bound_scope.internal_customer_id()
        if not _valid_positive_int(customer_id):
            return None
        return ConditionalCouponSubjectHandle(
            subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
            tenant_id=int(tenant_id),
            identity_namespace=str(identity_namespace),
            handle_source=HANDLE_SOURCE_BRIDGE,
            customer_id=int(customer_id),
            authoritative_a1_subject_handle=bridge_handle,
            bound_authoritative_a1_subject_scope=bound_scope,
        )

    if subject_kind == SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE:
        profile_id = bound_scope.external_customer_profile_id()
        if profile_id is None:
            return None
        return ConditionalCouponSubjectHandle(
            subject_kind=SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
            tenant_id=int(tenant_id),
            identity_namespace=str(identity_namespace),
            handle_source=HANDLE_SOURCE_BRIDGE,
            external_customer_profile_id=profile_id,
            authoritative_a1_subject_handle=bridge_handle,
            bound_authoritative_a1_subject_scope=bound_scope,
        )

    return None


def resolve_conditional_coupon_subject_handle(
    *,
    tenant_id: int,
    db: Any = None,
    conversation: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> SubjectResolutionResult:
    """
    Resolve an authoritative subject handle via the Platform read bridge.

    ``Conversation.customer_id`` is a general customer relation, not a
    provenance-bearing A1 scope contract. Inbound metadata has no established
    trusted writer. Neither may authorize an A1 history query here.
    """
    _ = inbound_metadata

    if not tenant_id:
        return SubjectResolutionResult(
            status=IDENTITY_STATUS_UNRESOLVED,
            reason_code=REASON_MISSING_TENANT,
        )

    conversation_id = _trusted_conversation_id(conversation)
    if db is None or conversation_id is None:
        return SubjectResolutionResult(
            status=IDENTITY_STATUS_UNRESOLVED,
            reason_code=REASON_HANDLE_UNAVAILABLE,
        )

    bridge_result = resolve_authoritative_a1_subject_for_conversation(
        db,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=int(tenant_id),
            conversation_id=conversation_id,
        ),
    )

    if (
        bridge_result.status != READ_STATUS_RESOLVED
        or bridge_result.handle is None
        or bridge_result.bound_scope is None
    ):
        return _map_bridge_unresolved(bridge_result.reason)

    materialized = _coupon_handle_from_bound_pair(
        tenant_id=int(tenant_id),
        bridge_handle=bridge_result.handle,
        bound_scope=bridge_result.bound_scope,
    )
    if materialized is None:
        return SubjectResolutionResult(
            status=IDENTITY_STATUS_UNRESOLVED,
            reason_code=REASON_CUSTOMER_UNVERIFIED,
        )

    return SubjectResolutionResult(
        status=IDENTITY_STATUS_RESOLVED,
        handle=materialized,
    )


def customer_scope_for_handle(handle: ConditionalCouponSubjectHandle) -> str:
    if handle.subject_kind == "external_customer_profile":
        return CUSTOMER_SCOPE_EXTERNAL
    if handle.subject_kind == "nahla_internal_customer":
        return CUSTOMER_SCOPE_INTERNAL
    return CUSTOMER_SCOPE_UNRESOLVED


__all__ = [
    "ConditionalCouponSubjectHandle",
    "HANDLE_SOURCE_BRIDGE",
    "REASON_CUSTOMER_UNVERIFIED",
    "REASON_HANDLE_UNAVAILABLE",
    "REASON_MISSING_TENANT",
    "REASON_SUBJECT_AMBIGUOUS",
    "SubjectResolutionResult",
    "bound_proof_snapshot_from_handle",
    "customer_scope_for_handle",
    "resolve_conditional_coupon_subject_handle",
]
