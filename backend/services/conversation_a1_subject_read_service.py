"""
Read-only, tenant-safe bridge from a routed conversation to an A1 subject.

This service deliberately has no path from phone numbers, inbound provider
metadata, message content, ``Conversation.customer_id``, or external-id
parsing to subject authority.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from services.conversation_a1_subject_binding_contract import (
    BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
    BINDING_SOURCE_SALLA_ORDER_CONVERSATION_ATTESTATION,
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
    namespace_for_subject_kind,
)
from services.conversation_a1_subject_read_contract import (
    AuthoritativeA1SubjectHandle,
    ConversationA1SubjectReadResult,
    EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE,
    READ_STATUS_RESOLVED,
    READ_STATUS_UNRESOLVED,
    TrustedConversationA1SubjectReadRequest,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
    UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID,
    UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE,
    UNRESOLVED_REASON_BINDING_SOURCE_INVALID,
    UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID,
    UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_CONVERSATION_ABSENT,
    UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH,
    UNRESOLVED_REASON_INVALID_CONVERSATION,
    UNRESOLVED_REASON_INVALID_REQUEST,
    UNRESOLVED_REASON_INVALID_TENANT,
    UNRESOLVED_REASON_READ_UNAVAILABLE,
    UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH,
)
from services.conversation_a1_subject_read_logging import log_subject_read_event
from services.order_customer_identity_contract import EVIDENCE_AUTHORITATIVE
from services.order_customer_identity_read_contract import (
    build_safe_external_profile_proof,
    build_safe_internal_customer_proof,
)

_AUTHORITATIVE_SOURCES_BY_SUBJECT_KIND = {
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER: frozenset({
        BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    }),
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE: frozenset({
        BINDING_SOURCE_SALLA_ORDER_CONVERSATION_ATTESTATION,
        BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
    }),
}


def _unresolved(reason: str) -> ConversationA1SubjectReadResult:
    log_subject_read_event(status=READ_STATUS_UNRESOLVED, reason=reason)
    return ConversationA1SubjectReadResult(
        status=READ_STATUS_UNRESOLVED,
        reason=reason,
    )


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _subject_exists_for_tenant(db: Session, *, binding: Any, tenant_id: int) -> bool:
    from models import Customer, ExternalCustomerProfile  # noqa: PLC0415

    if binding.subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER:
        return (
            db.query(Customer.id)
            .filter(
                Customer.id == binding.internal_customer_id,
                Customer.tenant_id == tenant_id,
            )
            .first()
            is not None
        )
    if binding.subject_kind == SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE:
        return (
            db.query(ExternalCustomerProfile.id)
            .filter(
                ExternalCustomerProfile.id == binding.external_customer_profile_id,
                ExternalCustomerProfile.tenant_id == tenant_id,
            )
            .first()
            is not None
        )
    return False


def _policy_proof_status(
    db: Session, *, binding: Any, tenant_id: int,
) -> tuple[bool, bool]:
    if binding.subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER:
        proof = build_safe_internal_customer_proof(
            db, tenant_id=tenant_id, customer_id=binding.internal_customer_id,
        )
    elif binding.subject_kind == SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE:
        proof = build_safe_external_profile_proof(
            db,
            tenant_id=tenant_id,
            external_customer_profile_id=binding.external_customer_profile_id,
        )
    else:
        return False, False
    if proof is None:
        return False, False
    if (
        str(proof.subject_kind) != str(binding.subject_kind)
        or str(proof.identity_namespace) != str(binding.identity_namespace)
    ):
        return False, False
    return True, bool(proof.policy_eligibility_ready)


def resolve_authoritative_a1_subject_for_conversation(
    db: Session,
    *,
    request: TrustedConversationA1SubjectReadRequest,
) -> ConversationA1SubjectReadResult:
    """
    Return one opaque handle only when the canonical A1 proof is policy-ready.

    ``request`` must originate in trusted routing after tenant and concrete
    conversation selection.  This method intentionally does not accept a
    Conversation object, phone, provider metadata, or message data.
    """
    if type(request) is not TrustedConversationA1SubjectReadRequest:
        return _unresolved(UNRESOLVED_REASON_INVALID_REQUEST)
    if not _valid_positive_int(request.tenant_id):
        return _unresolved(UNRESOLVED_REASON_INVALID_TENANT)
    if not _valid_positive_int(request.conversation_id):
        return _unresolved(UNRESOLVED_REASON_INVALID_CONVERSATION)

    tenant_id = request.tenant_id
    conversation_id = request.conversation_id
    # This entire read path, including the proof builders, must not flush any
    # caller-pending writes. A resolver must remain purely observational.
    try:
        with db.no_autoflush:
            from models import Conversation, ConversationA1SubjectBinding  # noqa: PLC0415

            conversation = (
                db.query(Conversation)
                .filter(Conversation.id == conversation_id)
                .first()
            )
            if conversation is None:
                return _unresolved(UNRESOLVED_REASON_CONVERSATION_ABSENT)
            if int(conversation.tenant_id) != tenant_id:
                return _unresolved(UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH)

            bindings = (
                db.query(ConversationA1SubjectBinding)
                .filter(
                    ConversationA1SubjectBinding.tenant_id == tenant_id,
                    ConversationA1SubjectBinding.conversation_id == conversation_id,
                    ConversationA1SubjectBinding.binding_state == BINDING_STATE_ACTIVE,
                )
                .all()
            )
            if not bindings:
                return _unresolved(UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT)
            if len(bindings) != 1:
                return _unresolved(UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE)
            binding = bindings[0]

            expected_namespace: Optional[str] = namespace_for_subject_kind(binding.subject_kind)
            if expected_namespace is None:
                return _unresolved(UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID)
            if binding.identity_namespace != expected_namespace:
                return _unresolved(UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID)
            if binding.evidence_class != EVIDENCE_AUTHORITATIVE:
                return _unresolved(UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE)
            if binding.binding_source not in _AUTHORITATIVE_SOURCES_BY_SUBJECT_KIND[binding.subject_kind]:
                return _unresolved(UNRESOLVED_REASON_BINDING_SOURCE_INVALID)
            if not _subject_exists_for_tenant(db, binding=binding, tenant_id=tenant_id):
                return _unresolved(UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH)
            proof_present, policy_ready = _policy_proof_status(
                db, binding=binding, tenant_id=tenant_id,
            )
            if not proof_present:
                return _unresolved(UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE)
            if not policy_ready:
                return _unresolved(UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE)

            handle = AuthoritativeA1SubjectHandle(binding.id)
            log_subject_read_event(
                status=READ_STATUS_RESOLVED,
                evidence_class=EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE,
            )
            return ConversationA1SubjectReadResult(
                status=READ_STATUS_RESOLVED,
                handle=handle,
                evidence_class=EVIDENCE_CLASS_AUTHORITATIVE_A1_POLICY_ELIGIBLE,
            )
    except Exception:  # noqa: BLE001  # Read contract must fail closed across ORM/proof implementations.
        # Database/proof errors are an unavailable read, without identifiers
        # or exception detail entering AI facts or telemetry.
        return _unresolved(UNRESOLVED_REASON_READ_UNAVAILABLE)


__all__ = ["resolve_authoritative_a1_subject_for_conversation"]
