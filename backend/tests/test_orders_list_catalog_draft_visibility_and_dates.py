"""Orders list visibility + stable created dates for WhatsApp catalog drafts."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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

from core.wa_catalog_order_immediate_draft import persist_catalog_order_immediate_draft  # noqa: E402
from core.wa_order_lifecycle import STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO  # noqa: E402
from models import Base, Conversation, Customer, Order, Tenant  # noqa: E402
from routers.orders import _read_created_at, _serialise_order  # noqa: E402
from services.nahla_order_bridge import (  # noqa: E402
    nahla_wa_catalog_external_id,
    nahla_wa_external_id,
)


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


def _seed_customer(db, tenant_id: int) -> Customer:
    customer = Customer(
        tenant_id=tenant_id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name="",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _seed_conversation(db, tenant_id: int, customer_id: int) -> Conversation:
    convo = Conversation(tenant_id=tenant_id, customer_id=customer_id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


def _catalog_meta(*, items: int = 2, total: float = 1614.0) -> dict:
    unit = total / items
    return {
        "source_type": "catalog_order",
        "catalog_id": "CAT-1",
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


@pytest.fixture(autouse=True)
def _enable_immediate_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "false")


def _persist(
    db,
    *,
    tenant_id: int,
    convo: Conversation,
    customer: Customer,
    meta: dict,
    message_event_id: int | None = None,
) -> Order | None:
    order = persist_catalog_order_immediate_draft(
        db,
        tenant_id=tenant_id,
        conversation=convo,
        inbound_metadata=meta,
        customer=customer,
        phone="+966500000001",
        message_event_id=message_event_id,
        source_message_key=f"wamid.{message_event_id or 'test'}",
    )
    db.commit()
    if order is not None:
        db.refresh(order)
    return order


def test_catalog_order_visible_in_orders_list_after_immediate_persist() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(),
        message_event_id=101,
    )
    assert order is not None

    rows = db.query(Order).filter_by(tenant_id=tenant.id).order_by(Order.id.desc()).all()
    assert any(r.id == order.id for r in rows)

    payload = _serialise_order(order, customer_lookup={}, now=datetime.now(timezone.utc))
    assert payload.get("items")
    assert payload.get("source") == "whatsapp"


def test_catalog_order_does_not_update_completed_order_same_conversation() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    old = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        external_order_number="NHL-OLD",
        status="completed",
        total="500.00",
        source="whatsapp",
        extra_metadata={
            "lifecycle": "paid",
            "created_at": "2025-01-01T10:00:00+00:00",
        },
    )
    db.add(old)
    db.commit()
    db.refresh(old)

    new_order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(total=999.0),
        message_event_id=202,
    )
    assert new_order is not None
    assert new_order.id != old.id
    assert new_order.external_id == nahla_wa_catalog_external_id(
        tenant.id, convo.id, message_event_id=202,
    )
    assert "999" in str(new_order.total or "")
    db.refresh(old)
    assert old.status == "completed"
    assert "999" not in str(old.total or "")


def test_catalog_order_updates_open_draft_same_conversation() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    first = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(items=1, total=100.0),
        message_event_id=1,
    )
    second = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(items=2, total=200.0),
        message_event_id=2,
    )
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(second.line_items or []) == 2
    assert db.query(Order).filter_by(tenant_id=tenant.id).count() == 1


def test_orders_list_display_date_uses_created_at_not_updated_at() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    created = datetime(2024, 6, 1, 8, 30, tzinfo=timezone.utc)
    updated = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    order = Order(
        tenant_id=tenant.id,
        external_id="test-date-order",
        status=STATUS_DRAFT,
        source="whatsapp",
        extra_metadata={
            "created_at": created.isoformat(),
            "draft_created_at": created.isoformat(),
            "last_updated_at": updated.isoformat(),
            "updated_at": updated.isoformat(),
            "last_synced_at": updated.isoformat(),
        },
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    read_created = _read_created_at(order, fallback=updated)
    assert read_created.date() == created.date()

    payload = _serialise_order(order, customer_lookup={}, now=updated)
    assert payload["createdAt"].startswith("2024-06-01")
    assert payload["display_created_at"].startswith("2024-06-01")
    assert payload["last_updated_at"].startswith("2026-06-24")
    assert payload["updated_at"].startswith("2026-06-24")


def test_order_list_includes_draft_pending_customer_info() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(),
        message_event_id=55,
    )
    assert order is not None
    assert order.status in {STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO}

    rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant.id)
        .order_by(Order.id.desc())
        .limit(400)
        .all()
    )
    assert any(r.id == order.id for r in rows)

    payload = _serialise_order(order, customer_lookup={}, now=datetime.now(timezone.utc))
    assert payload.get("status") in {"pending", "draft", STATUS_PENDING_CUSTOMER_INFO}


def test_order_serializer_exposes_created_and_updated_separately() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    created = datetime.now(timezone.utc) - timedelta(days=3)
    updated = datetime.now(timezone.utc)
    order = Order(
        tenant_id=tenant.id,
        external_id="serializer-fields",
        status="draft",
        source="whatsapp",
        extra_metadata={
            "draft_created_at": created.isoformat(),
            "last_updated_at": updated.isoformat(),
        },
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    payload = _serialise_order(order, customer_lookup={}, now=updated)
    assert "createdAt" in payload
    assert "display_created_at" in payload
    assert "last_updated_at" in payload
    assert "updated_at" in payload
    assert payload["display_created_at"] != payload["last_updated_at"]
