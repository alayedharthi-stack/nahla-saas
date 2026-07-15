"""Nahla WA order bridge → conversation A1 binding hook (sqlite)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
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
    Tenant,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_STATE_ACTIVE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(id=33, name="متجر تجريبي عام")
    session.add(tenant)
    customer = Customer(tenant_id=33, name="أحمد سالم", phone="966500000001")
    session.add(customer)
    session.flush()
    conv = Conversation(tenant_id=33, status="open", customer_id=customer.id)
    session.add(conv)
    session.commit()
    yield session
    session.close()


def test_sync_nahla_wa_order_writes_active_binding(db) -> None:
    conv = db.query(Conversation).filter_by(tenant_id=33).one()
    customer = db.query(Customer).filter_by(tenant_id=33).one()

    order = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=conv,
        brain_state={
            "stage": "ordering",
            "current_product_focus": {"title": "حذاء رياضي أبيض", "price": "199", "id": 1},
            "checkout_url": "",
        },
        order_prep={
            "product_id": "prod-1",
            "quantity": 1,
            "customer_first_name": "أحمد",
            "city": "الرياض",
            "payment_receipt_received": False,
            "awaiting_payment_receipt": False,
            "order_status": "",
        },
        trigger="binding_hook_test",
        customer=customer,
    )
    db.commit()

    assert order is not None
    binding = (
        db.query(ConversationA1SubjectBinding)
        .filter_by(
            tenant_id=33,
            conversation_id=conv.id,
            binding_state=BINDING_STATE_ACTIVE,
        )
        .one()
    )
    assert binding.subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER
    assert binding.internal_customer_id == customer.id


def test_sync_without_customer_writes_no_binding(db) -> None:
    conv = db.query(Conversation).filter_by(tenant_id=33).one()
    conv.customer_id = None
    db.commit()

    order = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=conv,
        brain_state={
            "stage": "ordering",
            "current_product_focus": {"title": "قميص قطني أزرق", "price": "89", "id": 2},
            "checkout_url": "",
        },
        order_prep={
            "product_id": "prod-2",
            "quantity": 1,
            "city": "الرياض",
            "payment_receipt_received": False,
            "awaiting_payment_receipt": False,
            "order_status": "",
        },
        trigger="binding_hook_no_customer",
        customer=None,
    )
    db.commit()

    assert order is not None
    assert db.query(ConversationA1SubjectBinding).count() == 0
