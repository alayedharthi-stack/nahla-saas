"""
Authoritative internal conversation → A1-subject binding writer (PR1).

Writes platform-owned bindings only from verified order identity links and an
explicit conversation association. Does not read phone, inbound metadata, or
``conversation.customer_id`` for subject derivation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.conversation_a1_subject_binding_contract import (
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_SUPERSEDED,
    BINDING_WRITE_OUTCOME_CREATED,
    BINDING_WRITE_OUTCOME_NO_OP,
    BINDING_WRITE_OUTCOME_SKIPPED,
    BINDING_WRITE_OUTCOME_SUPERSEDED,
    EVIDENCE_AUTHORITATIVE,
    PROVENANCE_KIND_ORDER,
    SKIP_REASON_CONVERSATION_NOT_FOUND,
    SKIP_REASON_CONVERSATION_TENANT_MISMATCH,
    SKIP_REASON_CUSTOMER_TENANT_MISMATCH,
    SKIP_REASON_MISSING_CONVERSATION_ID,
    SKIP_REASON_MISSING_TENANT,
    SKIP_REASON_ORDER_LINK_NOT_VERIFIED,
    SKIP_REASON_SUBJECT_ROW_MISSING,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
    order_has_verified_authoritative_internal_link,
)
from services.conversation_a1_subject_binding_logging import log_binding_write_event
from services.order_customer_identity_contract import NAHLA_INTERNAL_ORDER_V1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BindingWriteResult:
    outcome: str
    reason: Optional[str] = None


def _load_conversation_for_tenant(
    db: Session,
    *,
    tenant_id: int,
    conversation_id: int,
) -> Any | None:
    from models import Conversation  # noqa: PLC0415

    return (
        db.query(Conversation)
        .filter(
            Conversation.id == int(conversation_id),
            Conversation.tenant_id == int(tenant_id),
        )
        # Serialize all binding writers for this concrete tenant conversation.
        # PostgreSQL honors this row lock; SQLite test runs safely ignore it.
        .with_for_update()
        .first()
    )


def _load_internal_customer_for_tenant(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> Any | None:
    from models import Customer  # noqa: PLC0415

    return (
        db.query(Customer)
        .filter(
            Customer.id == int(customer_id),
            Customer.tenant_id == int(tenant_id),
        )
        .first()
    )


def _active_binding_for_conversation(
    db: Session,
    *,
    tenant_id: int,
    conversation_id: int,
) -> Any | None:
    from models import ConversationA1SubjectBinding  # noqa: PLC0415

    return (
        db.query(ConversationA1SubjectBinding)
        .filter(
            ConversationA1SubjectBinding.tenant_id == int(tenant_id),
            ConversationA1SubjectBinding.conversation_id == int(conversation_id),
            ConversationA1SubjectBinding.binding_state == BINDING_STATE_ACTIVE,
        )
        .first()
    )


def _same_active_binding(binding: Any, *, customer_id: int) -> bool:
    return (
        str(getattr(binding, "subject_kind", "") or "")
        == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER
        and int(getattr(binding, "internal_customer_id", 0) or 0) == int(customer_id)
        and str(getattr(binding, "identity_namespace", "") or "")
        == NAHLA_INTERNAL_ORDER_V1
        and str(getattr(binding, "binding_source", "") or "")
        == BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL
        and str(getattr(binding, "evidence_class", "") or "") == EVIDENCE_AUTHORITATIVE
    )


def _supersede_binding(binding: Any, *, now: datetime) -> None:
    binding.binding_state = BINDING_STATE_SUPERSEDED
    binding.revoked_at = now
    binding.updated_at = now


def _is_active_binding_unique_conflict(exc: IntegrityError) -> bool:
    """Only recover the expected partial-active unique conflict in a savepoint."""
    original = getattr(exc, "orig", None)
    constraint_name = getattr(getattr(original, "diag", None), "constraint_name", None)
    if constraint_name == "uq_casb_tenant_conversation_active":
        return True
    return "conversation_a1_subject_bindings.tenant_id" in str(original or exc)


def _new_active_binding(
    *,
    tenant_id: int,
    conversation_id: int,
    customer_id: int,
    provenance_id: str,
    now: datetime,
) -> Any:
    from models import ConversationA1SubjectBinding  # noqa: PLC0415

    return ConversationA1SubjectBinding(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer_id,
        external_customer_profile_id=None,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind=PROVENANCE_KIND_ORDER,
        provenance_id=provenance_id,
        bound_at=now,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )


def write_authoritative_internal_binding_from_verified_order(
    db: Session,
    *,
    tenant_id: int,
    conversation_id: int,
    order: Any,
) -> BindingWriteResult:
    """
    Idempotently bind ``conversation_id`` to the order's verified internal A1 subject.

    Subject is derived exclusively from the order's authoritative internal link —
    never from ``conversation.customer_id``, phone, or inbound metadata.
    """
    if not tenant_id:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=0,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_MISSING_TENANT,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_MISSING_TENANT,
        )

    tid = int(tenant_id)
    if not conversation_id:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_MISSING_CONVERSATION_ID,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_MISSING_CONVERSATION_ID,
        )

    if not order_has_verified_authoritative_internal_link(order):
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_ORDER_LINK_NOT_VERIFIED,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_ORDER_LINK_NOT_VERIFIED,
        )

    conv_id = int(conversation_id)
    customer_id = int(order.customer_id)

    conversation = _load_conversation_for_tenant(
        db, tenant_id=tid, conversation_id=conv_id,
    )
    if conversation is None:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CONVERSATION_NOT_FOUND,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CONVERSATION_NOT_FOUND,
        )

    conv_tid = getattr(conversation, "tenant_id", None)
    if conv_tid is None or int(conv_tid) != tid:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CONVERSATION_TENANT_MISMATCH,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CONVERSATION_TENANT_MISMATCH,
        )

    customer = _load_internal_customer_for_tenant(
        db, tenant_id=tid, customer_id=customer_id,
    )
    if customer is None:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_SUBJECT_ROW_MISSING,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_SUBJECT_ROW_MISSING,
        )

    cust_tid = getattr(customer, "tenant_id", None)
    if cust_tid is None or int(cust_tid) != tid:
        log_binding_write_event(
            event="binding_write_skipped",
            tenant_id=tid,
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CUSTOMER_TENANT_MISMATCH,
        )
        return BindingWriteResult(
            outcome=BINDING_WRITE_OUTCOME_SKIPPED,
            reason=SKIP_REASON_CUSTOMER_TENANT_MISMATCH,
        )

    order_pk = getattr(order, "id", None)
    if order_pk is None:
        db.flush()
        order_pk = getattr(order, "id", None)
    provenance_id = str(order_pk) if order_pk is not None else "pending"

    # The conversation row above is locked before this read. The nested
    # savepoint is a defensive recovery path for a writer that bypassed that
    # lock: its expected unique conflict cannot poison the caller transaction.
    for attempt in range(2):
        now = _utcnow()
        active = _active_binding_for_conversation(
            db, tenant_id=tid, conversation_id=conv_id,
        )
        if active is not None and _same_active_binding(active, customer_id=customer_id):
            log_binding_write_event(
                event="binding_write_no_op",
                tenant_id=tid,
                outcome=BINDING_WRITE_OUTCOME_NO_OP,
                binding_state=BINDING_STATE_ACTIVE,
                subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
                binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
                evidence_class=EVIDENCE_AUTHORITATIVE,
                provenance_kind=PROVENANCE_KIND_ORDER,
            )
            return BindingWriteResult(outcome=BINDING_WRITE_OUTCOME_NO_OP)

        outcome = BINDING_WRITE_OUTCOME_CREATED
        if active is not None:
            _supersede_binding(active, now=now)
            db.add(active)
            # Update first so the partial unique slot is released before insert.
            db.flush()
            outcome = BINDING_WRITE_OUTCOME_SUPERSEDED

        row = _new_active_binding(
            tenant_id=tid,
            conversation_id=conv_id,
            customer_id=customer_id,
            provenance_id=provenance_id,
            now=now,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError as exc:
            if attempt == 0 and _is_active_binding_unique_conflict(exc):
                # Savepoint rollback preserved the enclosing transaction; reread
                # under the held conversation lock and resolve deterministically.
                continue
            raise
        break
    else:
        raise RuntimeError("binding_active_conflict_retry_exhausted")

    log_binding_write_event(
        event="binding_write_committed",
        tenant_id=tid,
        outcome=outcome,
        binding_state=BINDING_STATE_ACTIVE,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        provenance_kind=PROVENANCE_KIND_ORDER,
    )
    return BindingWriteResult(outcome=outcome)


__all__ = [
    "BindingWriteResult",
    "write_authoritative_internal_binding_from_verified_order",
]
