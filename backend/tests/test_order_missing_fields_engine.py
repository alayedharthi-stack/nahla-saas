"""Phase D — unified MissingFieldsEngine from OrderContext."""
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

from core.customer_identity_resolver import SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED  # noqa: E402
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import MODE_CONFIRM, MODE_EDIT_REQUESTED, MODE_SKIP  # noqa: E402
from core.order_missing_fields_engine import (  # noqa: E402
    MODE_ASK,
    MODE_COMPUTE_PENDING,
    MODE_REVIEW,
    READINESS_COLLECTING_SHIPPING,
    READINESS_DRAFT_INCOMPLETE,
    READINESS_READY_FOR_PAYMENT,
    apply_missing_fields_engine_to_metadata,
    compute_missing_fields,
    missing_fields_result_to_api_dict,
    to_legacy_missing_fields,
)
from core.wa_catalog_order_immediate_draft import persist_catalog_order_immediate_draft  # noqa: E402
from models import Base, Conversation, Customer, CustomerAddress, Order, Tenant  # noqa: E402
from routers.orders import _serialise_order  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id, sync_nahla_wa_order  # noqa: E402


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


def _customer(db, tenant_id: int, *, name: str = "Ali Ahmed", verified: bool = True) -> Customer:
    meta = {}
    if verified:
        meta = {
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        }
    else:
        meta = {"proposed_name": "WA User", "customer_name_status": "proposed", "customer_name_confidence": 0.4}
    c = Customer(
        tenant_id=tenant_id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name=name if verified else "",
        extra_metadata=meta,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _ctx(db, **kwargs):
    return build_order_context(db, tenant_id=kwargs.pop("tenant_id"), **kwargs)


def test_missing_engine_skips_product_when_active_draft_has_line_items() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        line_items=[{"name": "P", "quantity": 1, "price": 10.0}],
        extra_metadata={"lifecycle": "whatsapp_draft"},
    )
    db.add(order)
    db.commit()

    ctx = _ctx(db, tenant_id=tenant.id, conversation=convo, phone="+966500000001")
    result = ctx.missing_fields_result
    assert result is not None
    assert result.field_states["product"].mode == MODE_SKIP


def test_missing_engine_skips_total_when_catalog_total_exists() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        phone="+966500000001",
        inbound_metadata={
            "source_type": "catalog_order",
            "total_price": 500.0,
            "product_items": [{"name": "P", "quantity": 1, "item_price": 500.0}],
        },
    )
    result = ctx.missing_fields_result
    assert result.field_states["total"].mode == MODE_SKIP


def test_missing_engine_verified_name_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    ctx = _ctx(db, tenant_id=tenant.id, customer=customer, phone="+966500000001")
    assert ctx.missing_fields_result.field_states["name"].mode == MODE_SKIP


def test_missing_engine_proposed_name_confirm() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id, verified=False)
    ctx = _ctx(db, tenant_id=tenant.id, customer=customer, phone="+966500000001")
    assert ctx.missing_fields_result.field_states["name"].mode == MODE_CONFIRM


def test_missing_engine_previous_address_confirm_not_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="Jeddah",
            google_maps_link="https://maps.google.com/?q=21,39",
        )
    )
    db.commit()
    ctx = _ctx(db, tenant_id=tenant.id, customer=customer, phone="+966500000001")
    delivery = ctx.missing_fields_result.field_states["delivery_address"]
    assert delivery.mode == MODE_CONFIRM
    assert ctx.shipping.accepted_delivery_address is False


def test_missing_engine_current_maps_address_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        phone="+966500000001",
        brain_state={
            "order_prep": {
                "google_maps_url": "https://maps.google.com/?q=24,46",
                "city": "Riyadh",
            }
        },
    )
    assert ctx.missing_fields_result.field_states["delivery_address"].mode == MODE_SKIP


def test_missing_engine_customer_requested_edit_sets_edit_requested() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        phone="+966500000001",
        message="أبغى أغير العنوان",
        brain_state={"order_prep": {"line_items": [{"name": "P", "quantity": 1, "price": 1.0}]}},
    )
    assert ctx.missing_fields_result.field_states["city"].mode == MODE_EDIT_REQUESTED
    assert ctx.missing_fields_result.field_states["delivery_address"].mode == MODE_EDIT_REQUESTED


def test_missing_engine_merchant_locked_field_not_overwritten() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    db.add(
        Order(
            tenant_id=tenant.id,
            external_id=nahla_wa_external_id(tenant.id, convo.id),
            status="pending_customer_info",
            source="whatsapp",
            line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
            extra_metadata={
                "lifecycle": "whatsapp_draft",
                "merchant_edit_locked": True,
                "google_maps_url": "https://maps.google.com/?q=1",
                "city": "Riyadh",
            },
            customer_info={"city": "Riyadh"},
        )
    )
    db.commit()
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
        message="أبغى أغير العنوان",
        brain_state={"order_prep": {"city": "Riyadh", "google_maps_url": "https://maps.google.com/?q=1"}},
    )
    city = ctx.missing_fields_result.field_states["city"]
    assert city.mode in {MODE_SKIP, MODE_REVIEW}
    assert city.evidence.get("locked") is True


def test_missing_engine_readiness_ready_for_payment_after_required_fields() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone="+966500000001",
        brain_state={
            "order_prep": {
                "line_items": [{"name": "P", "quantity": 1, "price": 100.0}],
                "total_price": 100.0,
                "city": "Riyadh",
                "google_maps_url": "https://maps.google.com/?q=24,46",
                "customer_first_name": "Ali",
                "customer_last_name": "Ahmed",
            }
        },
    )
    assert ctx.missing_fields_result.readiness_state == READINESS_READY_FOR_PAYMENT


def test_missing_engine_shadow_divergence_detected() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        phone="+966500000001",
        brain_state={
            "order_prep": {
                "missing_fields": ["customer_first_name", "customer_last_name", "city"],
                "customer_first_name": "Ali",
                "line_items": [{"name": "P", "quantity": 1, "price": 50.0}],
                "total": 50.0,
            }
        },
    )
    result = ctx.missing_fields_result
    assert result.divergence_flags["missing_fields_differ"] is True


def test_order_detail_payload_includes_missing_engine_result() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order)
    db.commit()
    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant.id,
    )
    assert payload.get("missing_fields_engine") is not None
    assert "readiness_state" in payload["missing_fields_engine"]


def test_bridge_can_use_engine_missing_fields_when_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("ORDER_MISSING_FIELDS_ENGINE_ENABLED", "true")
    monkeypatch.setenv("ORDER_MISSING_FIELDS_ENGINE_SHADOW_ENABLED", "true")

    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)

    order_prep = {
        "line_items": [{"name": "P", "quantity": 1, "price": 100.0}],
        "total_price": 100.0,
        "city": "Riyadh",
        "google_maps_url": "https://maps.google.com/?q=1",
        "customer_first_name": "Ali",
        "customer_last_name": "Ahmed",
    }
    brain_state = {"stage": "ordering", "order_prep": order_prep}

    order = sync_nahla_wa_order(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        brain_state=brain_state,
        order_prep=order_prep,
        trigger="test_engine",
        customer=customer,
    )
    db.commit()
    assert order is not None
    meta = dict(order.extra_metadata or {})
    assert meta.get("missing_fields_engine") is not None
    assert meta.get("missing_fields_source") == "missing_fields_engine"
    assert "city" not in (meta.get("missing_fields") or [])


def test_missing_engine_product_without_total_review_mode() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    ctx = _ctx(
        db,
        tenant_id=tenant.id,
        phone="+966500000001",
        brain_state={"order_prep": {"line_items": [{"name": "P", "quantity": 1}]}},
    )
    assert ctx.missing_fields_result.field_states["total"].mode == MODE_COMPUTE_PENDING
    assert ctx.missing_fields_result.readiness_state == READINESS_DRAFT_INCOMPLETE
