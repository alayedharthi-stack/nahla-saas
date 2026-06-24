"""Phase C — OrderContext identity/shipping prefill modes."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
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

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_MERCHANT,
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
)
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    MODE_ASK,
    MODE_CONFIRM,
    MODE_EDIT_REQUESTED,
    MODE_SKIP,
    apply_order_prep_prefill_patch,
    build_order_context_api_payload,
    build_order_prep_prefill_patch,
    detect_edit_intent_facts,
)
from core.wa_catalog_order_immediate_draft import persist_catalog_order_immediate_draft  # noqa: E402
from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: E402
from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    CustomerAddress,
    Order,
    Tenant,
)
from routers.orders import _serialise_order  # noqa: E402


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


def _verified_customer(db, tenant_id: int, *, name: str = "أحمد محمد", locked: bool = False) -> Customer:
    meta = {
        "customer_name_source": SOURCE_MERCHANT,
        "customer_name_status": STATUS_CUSTOMER_ENTERED,
        "customer_name_confidence": 0.95,
    }
    if locked:
        meta["manual_name_override"] = True
    customer = Customer(
        tenant_id=tenant_id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name=name,
        extra_metadata=meta,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _catalog_meta(*, items: int = 2, total: float = 200.0) -> dict:
    unit = total / items
    return {
        "source_type": "catalog_order",
        "item_count": items,
        "total_price": total,
        "currency": "SAR",
        "product_items": [
            {
                "product_retailer_id": f"sku-{i}",
                "quantity": 1,
                "item_price": unit,
                "currency": "SAR",
                "name": f"Product {i}",
            }
            for i in range(1, items + 1)
        ],
    }


def test_verified_customer_identity_missing_name_mode_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
    )
    assert ctx.identity.missing_mode == MODE_SKIP
    assert "name" not in ctx.shadow_missing_fields


def test_manual_name_override_blocks_ai_overwrite() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id, locked=True)
    prep = {"customer_first_name": "", "customer_last_name": ""}

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        brain_state={"order_prep": prep},
    )
    assert ctx.identity.locked_by_merchant is True
    patch = build_order_prep_prefill_patch(ctx, prep=prep)
    merged = apply_order_prep_prefill_patch(prep, patch)
    assert merged.get("customer_first_name", "") == ""


def test_proposed_name_requires_confirm_not_operational_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = Customer(
        tenant_id=tenant.id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name="",
        extra_metadata={
            "proposed_name": "WhatsApp User",
            "customer_name_status": STATUS_PROPOSED,
            "customer_name_confidence": 0.4,
        },
    )
    db.add(customer)
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
    )
    assert ctx.identity.missing_mode == MODE_CONFIRM
    assert ctx.identity.has_verified_name is False


def test_order_context_prefills_verified_name_when_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "true")
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    prep: dict = {}

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        brain_state={"order_prep": prep},
    )
    patch = build_order_prep_prefill_patch(ctx, prep=prep)
    merged = apply_order_prep_prefill_patch(prep, patch)
    assert merged["customer_first_name"] == "أحمد"
    assert merged["customer_last_name"] == "محمد"
    assert merged["identity_prefill_source"] == SOURCE_MERCHANT


def test_prefill_does_not_mutate_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "false")
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    prep: dict = {}

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        brain_state={"order_prep": prep},
    )
    assert build_order_prep_prefill_patch(ctx, prep=prep) == {}


def test_previous_address_mode_confirm_not_auto_apply() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="جدة",
            google_maps_link="https://maps.google.com/?q=21.5,39.2",
        )
    )
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
    )
    assert ctx.known_previous_address is not None
    assert ctx.prefill.shipping_delivery_mode == MODE_CONFIRM
    assert ctx.shipping.accepted_delivery_address is False
    assert not has_accepted_delivery_address(ctx.brain_order_prep)


def test_customer_confirmed_previous_address_promotes_to_shipping_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORDER_CONTEXT_SHIPPING_CONFIRM_ENABLED", "true")
    monkeypatch.setenv("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "true")
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="الرياض",
            saudi_national_address="RRRD2929",
        )
    )
    db.commit()

    prep = {"customer_confirmed_previous_address": True}
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        brain_state={"order_prep": prep},
        message="نفس العنوان السابق",
    )
    patch = build_order_prep_prefill_patch(ctx, prep=prep)
    merged = apply_order_prep_prefill_patch(prep, patch)
    assert merged["city"] == "الرياض"
    assert merged["short_address_code"] == "RRRD2929"
    assert merged["shipping_source"] == "customer_confirmed_previous_address"


def test_customer_requested_shipping_edit_sets_edit_mode() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        message="أبغى أغير العنوان",
    )
    assert ctx.prefill.shipping_delivery_mode == MODE_EDIT_REQUESTED
    assert ctx.prefill.customer_requested_edit is True


def test_customer_edit_locked_shipping_requires_merchant_review() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402

    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="pending_customer_info",
        source="whatsapp",
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "merchant_edit_locked": True,
            "city": "الرياض",
            "google_maps_url": "https://maps.google.com/?q=1",
        },
        customer_info={"city": "الرياض"},
    )
    db.add(order)
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
        message="أبغى أغير العنوان",
        brain_state={"order_prep": {"city": "الرياض", "google_maps_url": "https://maps.google.com/?q=1"}},
    )
    assert ctx.shipping.locked_by_merchant is True
    assert ctx.prefill.requires_merchant_review is True
    assert ctx.prefill.locked_field_edit_requested is True


def test_merchant_address_edit_lock_prevents_ai_overwrite() -> None:
    prep = {
        "city": "الرياض",
        "google_maps_url": "https://maps.google.com/?q=locked",
        "merchant_shipping_locked": True,
    }
    patch = {"city": "جدة", "google_maps_url": "https://maps.google.com/?q=new"}
    merged = apply_order_prep_prefill_patch(prep, patch)
    assert merged["city"] == "الرياض"
    assert merged["google_maps_url"] == "https://maps.google.com/?q=locked"


def test_order_detail_payload_includes_identity_shipping_modes() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402

    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
        customer_info={"phone": "+966500000001"},
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant.id,
    )
    prefill = payload.get("order_context_prefill") or {}
    assert prefill.get("identity", {}).get("missing_mode") == MODE_SKIP
    assert "shadow_missing_modes" in prefill


def test_known_customer_catalog_order_does_not_ask_known_name_when_prefill_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")
    monkeypatch.setenv("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "true")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "false")
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    persist_catalog_order_immediate_draft(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        inbound_metadata=_catalog_meta(),
        customer=customer,
        phone="+966500000001",
    )
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
    )
    assert ctx.active_draft is not None
    assert ctx.identity.missing_mode == MODE_SKIP
    assert "name" not in ctx.shadow_missing_fields
    assert "product" not in ctx.shadow_missing_fields
    assert "total" not in ctx.shadow_missing_fields

    patch = build_order_prep_prefill_patch(ctx, prep={})
    assert patch.get("customer_first_name") == "أحمد"


def test_detect_edit_intent_facts_name() -> None:
    facts = detect_edit_intent_facts("الاسم غلط")
    assert facts.name_edit_requested is True
