"""Unit tests for conversation A1-subject binding writer (sqlite / no PostgreSQL)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from models import (  # noqa: E402
    Base,
    Conversation,
    ConversationA1SubjectBinding,
    Customer,
    Order,
    Tenant,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_SUPERSEDED,
    BINDING_WRITE_OUTCOME_CREATED,
    BINDING_WRITE_OUTCOME_NO_OP,
    BINDING_WRITE_OUTCOME_SKIPPED,
    BINDING_WRITE_OUTCOME_SUPERSEDED,
    EVIDENCE_AUTHORITATIVE,
    SKIP_REASON_MISSING_CONVERSATION_ID,
    SKIP_REASON_ORDER_LINK_NOT_VERIFIED,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
    order_has_verified_authoritative_internal_link,
)
from services.conversation_a1_subject_binding_logging import (  # noqa: E402
    log_binding_write_event,
)
from services.conversation_a1_subject_binding_service import (  # noqa: E402
    write_authoritative_internal_binding_from_verified_order,
)
from services.order_customer_identity_contract import (  # noqa: E402
    NAHLA_INTERNAL_ORDER_V1,
)
from services.order_customer_identity_service import (  # noqa: E402
    apply_nahla_internal_order_identity,
    apply_whatsapp_order_identity_unlinked,
)


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(id=1, name="متجر تجريبي عام")
    session.add(tenant)
    session.commit()
    yield session
    session.close()


def _seed_conversation_customer(
    db,
    *,
    tenant_id: int = 1,
    customer_name: str = "أحمد سالم",
) -> tuple[Conversation, Customer]:
    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.add(customer)
    db.flush()
    conv = Conversation(
        tenant_id=tenant_id,
        status="open",
        customer_id=customer.id,
    )
    db.add(conv)
    db.flush()
    return conv, customer


def _verified_internal_order(
    db,
    *,
    tenant_id: int,
    customer_id: int,
    external_id: str = "nahla-wa-1-1",
) -> Order:
    order = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        status="pending_payment",
        total="120",
        source="whatsapp",
    )
    db.add(order)
    db.flush()
    apply_nahla_internal_order_identity(
        order, db=db, tenant_id=tenant_id, customer_id=customer_id,
    )
    return order


def test_verified_internal_order_creates_active_binding(db) -> None:
    conv, customer = _seed_conversation_customer(db)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)

    result = write_authoritative_internal_binding_from_verified_order(
        db,
        tenant_id=1,
        conversation_id=conv.id,
        order=order,
    )
    db.commit()

    assert result.outcome == BINDING_WRITE_OUTCOME_CREATED
    row = (
        db.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=1, conversation_id=conv.id, binding_state=BINDING_STATE_ACTIVE)
        .one()
    )
    assert row.subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER
    assert row.internal_customer_id == customer.id
    assert row.identity_namespace == NAHLA_INTERNAL_ORDER_V1
    assert row.binding_source == BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL
    assert row.evidence_class == EVIDENCE_AUTHORITATIVE


def test_unverified_order_skips_binding(db) -> None:
    conv, customer = _seed_conversation_customer(db)
    order = Order(
        tenant_id=1,
        external_id="nahla-wa-1-2",
        status="pending_payment",
        total="120",
        source="whatsapp",
    )
    db.add(order)
    db.flush()
    apply_whatsapp_order_identity_unlinked(order)

    result = write_authoritative_internal_binding_from_verified_order(
        db,
        tenant_id=1,
        conversation_id=conv.id,
        order=order,
    )

    assert result.outcome == BINDING_WRITE_OUTCOME_SKIPPED
    assert result.reason == SKIP_REASON_ORDER_LINK_NOT_VERIFIED
    assert db.query(ConversationA1SubjectBinding).count() == 0


def test_missing_internal_namespace_skips_binding(db) -> None:
    conv, customer = _seed_conversation_customer(db)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)
    order.identity_namespace = None

    assert order_has_verified_authoritative_internal_link(order) is False
    result = write_authoritative_internal_binding_from_verified_order(
        db,
        tenant_id=1,
        conversation_id=conv.id,
        order=order,
    )

    assert result.outcome == BINDING_WRITE_OUTCOME_SKIPPED
    assert result.reason == SKIP_REASON_ORDER_LINK_NOT_VERIFIED
    assert db.query(ConversationA1SubjectBinding).count() == 0


def test_internal_identity_apply_preserves_required_namespace(db) -> None:
    _, customer = _seed_conversation_customer(db)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)

    assert order.identity_namespace == NAHLA_INTERNAL_ORDER_V1
    assert order_has_verified_authoritative_internal_link(order) is True


def test_missing_conversation_id_skips(db) -> None:
    _, customer = _seed_conversation_customer(db)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)

    result = write_authoritative_internal_binding_from_verified_order(
        db,
        tenant_id=1,
        conversation_id=0,
        order=order,
    )

    assert result.outcome == BINDING_WRITE_OUTCOME_SKIPPED
    assert result.reason == SKIP_REASON_MISSING_CONVERSATION_ID


def test_idempotent_repeat_is_no_op(db) -> None:
    conv, customer = _seed_conversation_customer(db)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)

    first = write_authoritative_internal_binding_from_verified_order(
        db, tenant_id=1, conversation_id=conv.id, order=order,
    )
    second = write_authoritative_internal_binding_from_verified_order(
        db, tenant_id=1, conversation_id=conv.id, order=order,
    )
    db.commit()

    assert first.outcome == BINDING_WRITE_OUTCOME_CREATED
    assert second.outcome == BINDING_WRITE_OUTCOME_NO_OP
    assert (
        db.query(ConversationA1SubjectBinding)
        .filter_by(binding_state=BINDING_STATE_ACTIVE)
        .count()
        == 1
    )


def test_conflicting_rebind_supersedes_old_binding(db) -> None:
    conv, customer_a = _seed_conversation_customer(db)
    customer_b = Customer(tenant_id=1, name="نورة عبدالله")
    db.add(customer_b)
    db.flush()

    order_a = _verified_internal_order(
        db, tenant_id=1, customer_id=customer_a.id, external_id="ord-a",
    )
    write_authoritative_internal_binding_from_verified_order(
        db, tenant_id=1, conversation_id=conv.id, order=order_a,
    )
    db.commit()

    order_b = _verified_internal_order(
        db, tenant_id=1, customer_id=customer_b.id, external_id="ord-b",
    )
    result = write_authoritative_internal_binding_from_verified_order(
        db, tenant_id=1, conversation_id=conv.id, order=order_b,
    )
    db.commit()

    assert result.outcome == BINDING_WRITE_OUTCOME_SUPERSEDED
    active = (
        db.query(ConversationA1SubjectBinding)
        .filter_by(conversation_id=conv.id, binding_state=BINDING_STATE_ACTIVE)
        .one()
    )
    assert active.internal_customer_id == customer_b.id
    superseded = (
        db.query(ConversationA1SubjectBinding)
        .filter_by(conversation_id=conv.id, binding_state=BINDING_STATE_SUPERSEDED)
        .one()
    )
    assert superseded.internal_customer_id == customer_a.id
    assert superseded.revoked_at is not None


def test_active_state_cannot_have_revocation_timestamp(db) -> None:
    conv, customer = _seed_conversation_customer(db)
    row = ConversationA1SubjectBinding(
        tenant_id=1,
        conversation_id=conv.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer.id,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order",
        provenance_id="opaque",
        bound_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc),
    )
    db.add(row)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_cross_tenant_conversation_forbidden(db) -> None:
    conv, customer = _seed_conversation_customer(db, tenant_id=1)
    order = _verified_internal_order(db, tenant_id=1, customer_id=customer.id)

    result = write_authoritative_internal_binding_from_verified_order(
        db,
        tenant_id=2,
        conversation_id=conv.id,
        order=order,
    )

    assert result.outcome == BINDING_WRITE_OUTCOME_SKIPPED
    assert db.query(ConversationA1SubjectBinding).count() == 0


def test_subject_derived_from_order_not_conversation_customer_id(db) -> None:
    conv, linked_customer = _seed_conversation_customer(db)
    other_customer = Customer(tenant_id=1, name="عميل آخر")
    db.add(other_customer)
    db.flush()
    conv.customer_id = other_customer.id
    db.flush()

    order = _verified_internal_order(
        db, tenant_id=1, customer_id=linked_customer.id, external_id="ord-x",
    )
    write_authoritative_internal_binding_from_verified_order(
        db, tenant_id=1, conversation_id=conv.id, order=order,
    )
    db.commit()

    active = (
        db.query(ConversationA1SubjectBinding)
        .filter_by(binding_state=BINDING_STATE_ACTIVE)
        .one()
    )
    assert active.internal_customer_id == linked_customer.id
    assert active.internal_customer_id != other_customer.id


def test_binding_logging_has_no_entity_ids(caplog) -> None:
    caplog.set_level("INFO")
    log_binding_write_event(
        event="binding_write_committed",
        tenant_id=42,
        outcome=BINDING_WRITE_OUTCOME_CREATED,
        binding_state=BINDING_STATE_ACTIVE,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        provenance_kind="order",
    )
    record = caplog.records[-1]
    assert "conversation_id" not in record.message
    assert "customer_id" not in record.message
    assert "order_id" not in record.message
    assert "966" not in record.message
