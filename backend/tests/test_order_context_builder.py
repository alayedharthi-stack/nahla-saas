"""Tests for read-only OrderContext builder (Phase A shadow)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import patch

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
)
from core.order_context_builder import (  # noqa: E402
    build_order_context,
    compute_shadow_missing_fields,
    log_order_context_shadow,
    maybe_log_order_context_shadow,
)
from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    MessageEvent,
    Order,
    Tenant,
)
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402


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


def _seed_conversation(db, tenant_id: int, customer_id: int, brain_state: dict) -> Conversation:
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=customer_id,
        status="open",
        extra_metadata={"brain_state": brain_state},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


def _row_snapshot(row: Any) -> dict:
    return {
        "name": getattr(row, "name", None),
        "extra_metadata": dict(getattr(row, "extra_metadata", None) or {}),
        "status": getattr(row, "status", None),
        "line_items": list(getattr(row, "line_items", None) or []),
        "total": getattr(row, "total", None),
        "customer_info": dict(getattr(row, "customer_info", None) or {}),
    }


def test_order_context_builder_reads_verified_customer_identity() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = Customer(
        tenant_id=tenant.id,
        phone="+966551234567",
        normalized_phone="966551234567",
        name="أحمد محمد",
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
            "manual_name_override": True,
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    before = _row_snapshot(customer)
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=None,
        customer=customer,
        phone="+966551234567",
        brain_state={},
    )
    after = _row_snapshot(db.query(Customer).filter_by(id=customer.id).one())

    assert before == after
    assert ctx.identity.operational_name == "أحمد محمد"
    assert ctx.identity.has_verified_name is True
    assert ctx.identity.locked_by_merchant is True
    assert ctx.identity.confidence == pytest.approx(0.95)
    assert ctx.identity.name_source == SOURCE_MERCHANT


def test_order_context_builder_reads_brain_order_prep_shipping() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    brain_state = {
        "order_prep": {
            "city": "الرياض",
            "district": "النرجس",
            "street": "شارع 10",
            "google_maps_url": "https://maps.google.com/?q=24.7,46.7",
            "short_address_code": "",
        }
    }
    convo = Conversation(
        tenant_id=tenant.id,
        status="open",
        extra_metadata={"brain_state": brain_state},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        phone="+966500000001",
    )

    assert ctx.shipping.city == "الرياض"
    assert ctx.shipping.district == "النرجس"
    assert ctx.shipping.maps_url.startswith("https://maps.google.com")
    assert ctx.shipping.accepted_delivery_address is True


def test_order_context_builder_reads_active_draft_by_conversation_external_id() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    external_id = nahla_wa_external_id(tenant.id, convo.id)
    order = Order(
        tenant_id=tenant.id,
        external_id=external_id,
        status="pending_customer_info",
        total="250.0",
        line_items=[{"name": "SKU-1", "quantity": 1, "price": 250.0}],
        customer_info={"phone": "+966500000001"},
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "currency": "SAR",
            "missing_fields": ["city", "delivery_address"],
        },
    )
    db.add(order)
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        phone="+966500000001",
        brain_state={"order_prep": {}},
    )

    assert ctx.active_draft is not None
    assert ctx.active_draft.external_id == external_id
    assert len(ctx.active_draft.line_items) == 1
    assert ctx.active_draft.total == pytest.approx(250.0)
    assert ctx.active_draft.missing_fields == ["city", "delivery_address"]


def test_order_context_builder_reads_catalog_order_snapshot_from_message_metadata() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    catalog_meta = {
        "source_type": "catalog_order",
        "product_items": [
            {"name": "Product A", "quantity": 2, "item_price": 100.0, "currency": "SAR"},
        ],
        "item_count": 2,
        "total_price": 200.0,
        "currency": "SAR",
    }
    db.add(
        MessageEvent(
            tenant_id=tenant.id,
            conversation_id=convo.id,
            direction="inbound",
            body="catalog order",
            extra_metadata={"normalized_inbound": catalog_meta},
        )
    )
    db.commit()

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        phone="+966500000001",
    )

    assert ctx.catalog_order.has_catalog_order is True
    assert ctx.catalog_order.item_count == 2
    assert ctx.catalog_order.total_price == pytest.approx(200.0)
    assert len(ctx.catalog_order.product_items) == 1


def test_order_context_shadow_missing_fields_detects_divergence() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    brain_state = {
        "order_prep": {
            "missing_fields": ["customer_first_name", "customer_last_name", "city"],
            "customer_first_name": "Ali",
            "line_items": [{"name": "P", "quantity": 1, "price": 50.0}],
            "total": 50.0,
        }
    }
    convo = Conversation(
        tenant_id=tenant.id,
        status="open",
        extra_metadata={"brain_state": brain_state},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        phone="+966500000001",
    )

    assert "customer_first_name" in ctx.legacy_missing_fields
    assert "city" in ctx.shadow_missing_fields
    assert "delivery_address" in ctx.shadow_missing_fields
    assert ctx.divergence_flags["missing_fields_differ"] is True
    assert ctx.divergence_flags["delivery_address_divergence"] is True
    assert ctx.legacy_missing_fields == brain_state["order_prep"]["missing_fields"]


def test_order_context_builder_does_not_mutate_db() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = Customer(
        tenant_id=tenant.id,
        phone="+966551111111",
        normalized_phone="966551111111",
        name="Sara",
        extra_metadata={"customer_name_status": STATUS_CUSTOMER_ENTERED},
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    brain_state = {
        "order_prep": {
            "city": "جدة",
            "customer_first_name": "Sara",
            "customer_last_name": "Ali",
        }
    }
    convo = _seed_conversation(db, tenant.id, customer.id, brain_state)

    external_id = nahla_wa_external_id(tenant.id, convo.id)
    order = Order(
        tenant_id=tenant.id,
        external_id=external_id,
        status="draft",
        total="99",
        line_items=[{"name": "X", "quantity": 1, "price": 99.0}],
        extra_metadata={"lifecycle": "whatsapp_draft", "missing_fields": ["delivery_address"]},
    )
    db.add(order)
    db.commit()

    cust_before = _row_snapshot(db.query(Customer).filter_by(id=customer.id).one())
    convo_before = _row_snapshot(db.query(Conversation).filter_by(id=convo.id).one())
    order_before = _row_snapshot(db.query(Order).filter_by(id=order.id).one())

    build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966551111111",
        brain_state=brain_state,
    )

    assert _row_snapshot(db.query(Customer).filter_by(id=customer.id).one()) == cust_before
    assert _row_snapshot(db.query(Conversation).filter_by(id=convo.id).one()) == convo_before
    assert _row_snapshot(db.query(Order).filter_by(id=order.id).one()) == order_before


def test_order_context_shadow_logging_masks_pii(caplog: pytest.LogCaptureFixture) -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    brain_state = {
        "order_prep": {
            "city": "الرياض",
            "address_line": "حي النرجس شارع 22 مبنى 5",
            "google_maps_url": "https://maps.google.com/?q=1",
        }
    }
    convo = Conversation(
        tenant_id=tenant.id,
        status="open",
        extra_metadata={"brain_state": brain_state},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        phone="+966551234567",
    )

    with caplog.at_level(logging.INFO):
        log_order_context_shadow(ctx)

    joined = "\n".join(r.message for r in caplog.records)
    assert "[ORDER_CONTEXT_SHADOW]" in joined
    assert "+966551234567" not in joined
    assert "4567" in joined
    assert "حي النرجس شارع 22" not in joined


def test_maybe_log_order_context_shadow_respects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open", extra_metadata={})
    db.add(convo)
    db.commit()
    db.refresh(convo)

    monkeypatch.setenv("ORDER_CONTEXT_SHADOW_ENABLED", "false")
    with patch("core.config.ORDER_CONTEXT_SHADOW_ENABLED", False):
        assert maybe_log_order_context_shadow(db, tenant_id=tenant.id, conversation=convo) is None

    shadow = compute_shadow_missing_fields(
        build_order_context(db, tenant_id=tenant.id, conversation=convo, phone="+966500000001")
    )
    assert isinstance(shadow, list)
