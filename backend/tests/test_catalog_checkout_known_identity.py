"""Catalog checkout — known identity/phone/shipping facts for LLM compose."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_identity_resolver import SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED  # noqa: E402
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    MODE_CONFIRM,
    MODE_SKIP,
    build_checkout_compose_facts,
    derive_checkout_next_goal,
    detect_customer_identity_inquiry,
)
from core.order_missing_fields_engine import (  # noqa: E402
    MissingFieldState,
    MissingFieldsResult,
    READINESS_CONFIRMING_SHIPPING,
    compute_missing_fields,
)
from core.order_shipping_snapshot import build_order_shipping_snapshot  # noqa: E402
from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: E402
from models import Base, Conversation, Customer, CustomerAddress, Tenant  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _verified_customer(db, tenant_id: int, *, name: str = "عايد بن حسين") -> Customer:
    customer = Customer(
        tenant_id=tenant_id,
        phone="+966551234567",
        normalized_phone="966551234567",
        name=name,
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _catalog_prep(*, city: str = "", short_code: str = "", maps: str = "") -> dict:
    prep = {
        "line_items": [
            {
                "product_retailer_id": "sku-1",
                "product_id": 1,
                "quantity": 1,
                "from_native_catalog_order": True,
                "from_catalog_order": True,
            }
        ],
        "catalog_checkout_total": 200.0,
    }
    if city:
        prep["city"] = city
    if short_code:
        prep["short_address_code"] = short_code
    if maps:
        prep["google_maps_url"] = maps
    return prep


def test_whatsapp_order_does_not_ask_phone_when_sender_phone_known() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep()},
    )
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["phone_mode"] == MODE_SKIP
    assert facts["known_phone"] == "+966551234567"
    assert facts["phone_source"] == "whatsapp_sender"
    assert "phone" not in facts["missing_fields"]
    assert "customer_phone" not in facts["missing_fields"]


def test_customer_asks_known_phone_gets_phone_fact() -> None:
    inquiry = detect_customer_identity_inquiry("طيب جوالي كم؟")
    assert inquiry.get("customer_asks_known_phone") is True

    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep()},
    )
    facts = build_checkout_compose_facts(
        ctx,
        message="طيب جوالي كم؟",
        phone="+966551234567",
    )
    assert facts["customer_asks_known_phone"] is True
    assert facts["known_phone"] == "+966551234567"


def test_customer_asks_known_name_gets_known_name_fact() -> None:
    inquiry = detect_customer_identity_inquiry("وش اسمي؟")
    assert inquiry.get("customer_asks_known_name") is True

    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep()},
    )
    facts = build_checkout_compose_facts(
        ctx,
        message="وش اسمي؟",
        phone="+966551234567",
    )
    assert facts["customer_asks_known_name"] is True
    assert facts["name_mode"] == MODE_SKIP
    assert facts["known_name"] == "عايد بن حسين"


def test_catalog_order_after_address_does_not_ask_name_or_phone_when_known() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    prep = _catalog_prep(
        city="الطائف",
        short_code="TAPB7402",
    )
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": prep},
    )
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["name_mode"] == MODE_SKIP
    assert facts["phone_mode"] == MODE_SKIP
    assert "customer_first_name" not in facts["missing_fields"]
    assert "customer_last_name" not in facts["missing_fields"]


def test_city_from_current_flow_is_reused_not_asked_again() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep(city="الطائف")},
        message="الطايف",
    )
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["known_city"] == "الطائف"
    assert facts["city_mode"] == MODE_SKIP
    assert "city" not in facts["missing_fields"]


def test_short_national_address_updates_shipping_snapshot() -> None:
    prep = _catalog_prep(
        city="الطائف",
        short_code="TAPB7402",
    )
    prep["address_line"] = "7402 شارع العدل الواعظ، حي الحلقة الغربية"
    snapshot = build_order_shipping_snapshot(order_prep=prep)
    assert snapshot["short_address_code"] == "TAPB7402"
    assert snapshot["city"] == "الطائف"
    assert has_accepted_delivery_address(prep)


def test_known_previous_address_sets_delivery_address_confirm_not_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="جدة",
            saudi_national_address="JEDA1234",
        )
    )
    db.commit()
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep()},
    )
    assert ctx.prefill.shipping_delivery_mode == MODE_CONFIRM
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["delivery_address_mode"] == "confirm"
    assert facts["known_previous_address"]["city"] == "جدة"


def test_shipping_details_are_confirmed_in_one_message_goal() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={"order_prep": _catalog_prep(city="الطائف", short_code="TAPB7402")},
    )
    result = compute_missing_fields(ctx)
    goal = derive_checkout_next_goal(result, ctx.prefill)
    assert goal == "confirm_customer_and_shipping_details_once"
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["ask_confirmation_once"] is True


def test_no_multi_step_identity_address_confirmations_when_all_fields_known() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966551234567",
        brain_state={
            "order_prep": _catalog_prep(
                city="الطائف",
                short_code="TAPB7402",
            )
        },
    )
    result = compute_missing_fields(ctx)
    assert result.readiness_state in {
        READINESS_CONFIRMING_SHIPPING,
        "ready_for_payment",
        "ready_for_confirmation",
    }
    facts = build_checkout_compose_facts(ctx, phone="+966551234567")
    assert facts["missing_fields"] == []
    assert facts["next_goal"] == "confirm_customer_and_shipping_details_once"
    assert facts["do_not_repeat_field_confirmations"] is True
