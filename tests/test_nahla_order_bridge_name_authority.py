"""Order bridge honours the customer-name authority contract.

``Order.customer_name`` / ``customer_info["name"]`` reach shipping
synchronisation, so they are OPERATIONAL identity. Only a checkout name
obtained through the accepted checkout semantics, or a customer canonical
name with ``can_use_name_for_operations() == True``, may populate them.
A WhatsApp profile / display string never does.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.customer_identity_resolver import (
    STATUS_PROPOSED,
    apply_customer_name,
    can_use_name_for_operations,
    display_name_for_customer,
    read_customer_identity,
)
from core.customer_name_authority import NameAuthority
from core.customer_name_provenance import read_name_authority
from models import Base, Customer, Order, Tenant
from modules.ai.brain.execution.orders import (
    _expects_checkout_name_answer,
    _merge_message_details,
    _persist_expected_checkout_name_answer,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from services.nahla_order_bridge import (
    _customer_payload,
    _resolve_customer_name,
    sync_nahla_wa_order,
)

PHONE = "+966500000000"
DIGITS = "966500000000"


def _db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    db = sessionmaker(bind=engine)()
    tenant = Tenant(id=33, name="متجر تجريبي", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(tenant_id=tenant.id, phone=PHONE, normalized_phone=PHONE,
                    name=None, extra_metadata={}, acquisition_channel="whatsapp_inbound")
    db.add(cust)
    db.commit()
    return db, tenant, cust


def _conv(cust, **conv_meta):
    return SimpleNamespace(id=9063, tenant_id=cust.tenant_id, customer_id=cust.id,
                           customer=cust, extra_metadata=dict(conv_meta))


def _prep_via_direct_answer(db, tenant_id, answer):
    """Produce an order_prep whose name came from the REAL direct-answer flow."""
    prep = OrderPreparationState(
        quantity=1, product_id="generic-1", order_status="awaiting_address",
        missing_fields=["customer_first_name", "customer_last_name", "city"],
        line_items=[{"product_id": "generic-1", "product_name": "منتج", "quantity": 1, "match_status": "matched"}],
        product_options_loaded=True, checkout_channel="whatsapp",
    )
    state = MerchantConversationState(
        stage="ordering", turn=2, order_prep=prep,
        current_product_focus={"id": "generic-1", "external_id": "generic-1", "title": "منتج",
                               "price": 10.0, "orderable": True, "can_checkout": True, "in_stock": True},
    )
    parts = answer.split()
    ctx = BrainContext(
        tenant_id=tenant_id, customer_phone=PHONE, message=answer, raw_message=answer,
        intent=Intent(name="checkout_continuation", confidence=0.9, raw_message=answer, extraction_method="hybrid",
                      slots={"customer_name": answer, "customer_first_name": parts[0],
                             "customer_last_name": " ".join(parts[1:])}),
        state=state, facts=CommerceFacts(), history=[], profile={}, conversation_id=9063,
    )
    ctx._db = db
    expected = _expects_checkout_name_answer(prep)
    _merge_message_details(prep, ctx.intent.slots, ctx.raw_message)
    assert _persist_expected_checkout_name_answer(ctx, prep, expected=expected, previous_name="") is True
    db.commit()
    return {"customer_first_name": prep.customer_first_name,
            "customer_last_name": prep.customer_last_name, "customer_phone": DIGITS}


# ── CASE 1: WhatsApp-authority canonical name, prep has no name ──────

def test_case1_whatsapp_canonical_name_does_not_become_order_name():
    db, tenant, cust = _db()
    apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound")
    db.commit()
    assert cust.name == "محمد أحمد"
    assert read_name_authority(cust) is NameAuthority.WHATSAPP_PROFILE
    assert read_customer_identity(cust).customer_name_status == STATUS_PROPOSED
    assert display_name_for_customer(cust, phone_fallback=PHONE) == "محمد أحمد"  # display stays

    name, info = _customer_payload(_conv(cust), {"customer_phone": DIGITS})
    assert name is None
    assert info["name"] is None
    assert info["phone"] and info["shipping_phone"] == info["phone"]  # phone auto-fill untouched


# ── CASE 2: raw devotional profile metadata never leaks ───────────────

def test_case2_raw_wa_profile_metadata_never_reaches_order_identity():
    db, tenant, cust = _db()
    cust.extra_metadata = {"wa_profile_name": "الحمد لله", "profile_name": "الحمد لله",
                           "whatsapp_name": "الحمد لله", "display_name": "الحمد لله"}
    db.commit()
    conv = _conv(cust, contact_name="الحمد لله", wa_profile_name="الحمد لله", customer_name="الحمد لله")
    assert _resolve_customer_name(conv, {}) is None
    name, info = _customer_payload(conv, {"customer_phone": DIGITS})
    assert name is None and info["name"] is None
    assert "الحمد لله" not in str(info)


# ── CASE 3: verified Salla canonical name is operational ─────────────

def test_case3_verified_store_name_is_the_order_name():
    db, tenant, cust = _db()
    apply_customer_name(cust, "محمد أحمد الحارثي", source="salla_sync")
    db.commit()
    assert can_use_name_for_operations(cust) is True
    name, info = _customer_payload(_conv(cust), {"customer_phone": DIGITS})
    assert name == "محمد أحمد الحارثي"
    assert info["name"] == "محمد أحمد الحارثي"


# ── CASE 4: checkout prep name from the real direct-answer flow ──────

def test_case4_direct_answer_checkout_name_is_the_order_name():
    db, tenant, cust = _db()
    prep = _prep_via_direct_answer(db, tenant.id, "محمد أحمد")
    assert prep == {"customer_first_name": "محمد", "customer_last_name": "أحمد", "customer_phone": DIGITS}
    db.expire_all()
    cust = db.get(Customer, cust.id)
    assert can_use_name_for_operations(cust) is True
    name, info = _customer_payload(_conv(cust), prep)
    assert name == "محمد أحمد" and info["name"] == "محمد أحمد"


# ── CASE 5: WA display name AND trusted checkout name → checkout wins ─

def test_case5_trusted_checkout_name_wins_over_wa_display_name():
    db, tenant, cust = _db()
    apply_customer_name(cust, "خالد محمد", source="whatsapp_inbound")
    cust.extra_metadata = {**cust.extra_metadata, "wa_profile_name": "خالد محمد"}
    db.commit()
    name, info = _customer_payload(
        _conv(cust, wa_profile_name="خالد محمد"),
        {"customer_first_name": "محمد", "customer_last_name": "أحمد", "customer_phone": DIGITS},
    )
    assert name == "محمد أحمد" and info["name"] == "محمد أحمد"


# ── CASE 6: no trusted name → order proceeds with empty name ─────────

def test_case6_no_trusted_name_leaves_operational_name_missing(monkeypatch):
    """Order creation proceeds (existing lifecycle untouched) with the
    operational customer name left empty — no WhatsApp display fallback."""
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", "33")
    db, tenant, cust = _db()
    apply_customer_name(cust, "خالد محمد", source="whatsapp_inbound")
    cust.extra_metadata = {**cust.extra_metadata, "wa_profile_name": "خالد محمد"}
    db.commit()
    conv = SimpleNamespace(id=9063, tenant_id=33, customer_id=cust.id, customer=cust,
                           extra_metadata={"wa_profile_name": "خالد محمد"})

    result = sync_nahla_wa_order(
        db, tenant_id=33, conversation=conv,
        brain_state={"stage": "ordering", "current_product_focus": {"title": "Honey", "price": "320", "id": 9}, "checkout_url": ""},
        order_prep={"product_id": "prod-99", "product_name": "Honey", "quantity": 1, "price": "320",
                    "city": "Riyadh", "customer_phone": DIGITS,
                    "payment_receipt_received": False, "awaiting_payment_receipt": False, "order_status": ""},
        trigger="test",
    )
    db.commit()
    orders = db.query(Order).filter(Order.tenant_id == 33).all()
    assert result is not None and orders, "order creation should still proceed"
    order = orders[-1]
    assert not order.customer_name
    assert not (order.customer_info or {}).get("name")
    assert (order.customer_info or {}).get("phone")
    assert "خالد محمد" not in str(order.customer_info) and "خالد محمد" not in str(order.customer_name)
