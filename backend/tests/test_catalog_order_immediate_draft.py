"""Phase B — immediate WhatsApp catalog_order → visible Nahla draft order."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
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

from core.order_context_builder import build_order_context, compute_shadow_missing_fields  # noqa: E402
from core.wa_catalog_order_immediate_draft import (  # noqa: E402
    persist_catalog_order_immediate_draft,
)
from core.wa_order_lifecycle import STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO  # noqa: E402
from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    MessageEvent,
    Order,
    Tenant,
)
from routers.orders import _serialise_order  # noqa: E402
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
        source_message_key="wamid.test",
    )
    db.commit()
    if order is not None:
        db.refresh(order)
    return order


def test_catalog_order_creates_visible_incomplete_draft() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=_catalog_meta())
    assert order is not None
    meta = dict(order.extra_metadata or {})
    assert meta.get("lifecycle") == "whatsapp_draft"
    assert meta.get("catalog_order", {}).get("source") == "whatsapp_catalog_order"
    assert order.status in {STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO}
    assert order.source == "whatsapp"
    assert order.external_id == nahla_wa_external_id(tenant.id, convo.id)


def test_catalog_order_draft_preserves_two_line_items() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(items=2, total=200.0),
    )
    assert order is not None
    assert len(order.line_items or []) == 2
    names = {
        (li.get("product_name") or li.get("title") or li.get("name") or "")
        for li in (order.line_items or [])
    }
    assert "Product 1" in names
    assert "Product 2" in names


def test_catalog_order_draft_preserves_total_and_currency() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(total=1614.0),
    )
    assert order is not None
    assert "1614" in str(order.total or "")
    catalog_meta = dict(order.extra_metadata or {}).get("catalog_order") or {}
    assert catalog_meta.get("catalog_total_price") == pytest.approx(1614.0)
    assert catalog_meta.get("catalog_currency") == "SAR"


def test_catalog_order_draft_stores_raw_payload_reference() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)
    event = MessageEvent(
        tenant_id=tenant.id,
        conversation_id=convo.id,
        direction="inbound",
        body="catalog",
        extra_metadata={"normalized_inbound": _catalog_meta()},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    order = _persist(
        db,
        tenant_id=tenant.id,
        convo=convo,
        customer=customer,
        meta=_catalog_meta(),
        message_event_id=event.id,
    )
    assert order is not None
    catalog_meta = dict(order.extra_metadata or {}).get("catalog_order") or {}
    assert catalog_meta.get("source_message_id") == str(event.id)
    assert catalog_meta.get("raw_payload_ref") == {"message_event_id": event.id}


def test_catalog_order_draft_visible_in_order_detail_payload() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)
    order = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=_catalog_meta())
    assert order is not None

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant.id,
    )
    assert payload.get("amount_sar") is not None
    assert payload.get("items")
    assert len(payload.get("line_items") or payload.get("detailed_items") or []) >= 1 or payload.get("items")


def test_catalog_order_draft_visible_in_order_list_payload() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)
    order = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=_catalog_meta())
    assert order is not None

    from core.wa_order_dashboard import order_matches_lifecycle_filter  # noqa: PLC0415

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=False,
    )
    assert payload.get("items")
    assert order_matches_lifecycle_filter(order, "all") is True


def test_catalog_order_draft_idempotent_for_same_message() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)
    meta = _catalog_meta()

    first = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=meta, message_event_id=42)
    second = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=meta, message_event_id=42)
    assert first is not None and second is not None
    assert first.id == second.id
    orders = db.query(Order).filter_by(tenant_id=tenant.id).all()
    assert len(orders) == 1
    assert len(orders[0].line_items or []) == 2


def test_catalog_order_updates_existing_conversation_draft() -> None:
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
    assert "200" in str(second.total or "")


def test_order_context_reads_immediate_catalog_draft() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)
    _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=_catalog_meta())

    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        customer=customer,
        phone="+966500000001",
    )
    assert ctx.active_draft is not None
    assert len(ctx.active_draft.line_items) == 2
    assert ctx.active_draft.total is not None
    shadow = compute_shadow_missing_fields(ctx)
    assert "product" not in shadow
    assert "total" not in shadow


def test_catalog_order_draft_does_not_require_city_or_address() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    convo = _seed_conversation(db, tenant.id, customer.id)

    order = _persist(db, tenant_id=tenant.id, convo=convo, customer=customer, meta=_catalog_meta())
    assert order is not None
    missing = list((order.extra_metadata or {}).get("missing_fields") or [])
    assert "city" in missing
    assert "delivery_address" in missing
    assert order.id is not None
