"""Catalog multi-item persistence, AI facts, body copy, and order list activity sort."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

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

from core.order_context_builder import build_order_context  # noqa: E402
from core.order_flow import _focus_summary  # noqa: E402
from core.wa_cart_line_items import build_line_items_from_order_prep  # noqa: E402
from core.wa_catalog_order_immediate_draft import persist_catalog_order_immediate_draft  # noqa: E402
from core.wa_native_catalog_order import build_line_items_from_payload, parse_native_catalog_order  # noqa: E402
from models import Base, Conversation, Customer, Order, Tenant  # noqa: E402
from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: E402
    FORBIDDEN_CATALOG_INTRO_MARKERS,
    TECHNICAL_CATALOG_BODY,
    is_unsafe_catalog_body,
    resolve_native_catalog_body_text,
)
from modules.ai.brain.commerce.catalog_order_facts import build_catalog_order_compose_facts  # noqa: E402
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: E402
from routers.orders import _read_created_at, _read_last_updated_at, _serialise_order  # noqa: E402
from services.nahla_order_bridge import nahla_wa_catalog_external_id, nahla_wa_external_id  # noqa: E402


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


def _two_item_meta(*, total: float = 365.5) -> dict:
    return {
        "source_type": "catalog_order",
        "item_count": 2,
        "total_price": total,
        "currency": "SAR",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": 1,
                "item_price": 239.5,
                "currency": "SAR",
                "name": "500 جرام العسل الصيفي",
            },
            {
                "product_retailer_id": "sku-2",
                "quantity": 1,
                "item_price": 126.0,
                "currency": "SAR",
                "name": "250 جرام سدر",
            },
        ],
    }


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "false")


class TestNativeCatalogBody:
    def test_native_catalog_body_not_dot_when_safe_catalog_copy_enabled(self):
        body = resolve_native_catalog_body_text(
            context_reply="",
            inbound_customer_message="وش عندكم منتجات؟",
        )
        assert body == TECHNICAL_CATALOG_BODY
        assert body != "."
        assert body != "وش عندكم منتجات؟"
        for marker in FORBIDDEN_CATALOG_INTRO_MARKERS:
            assert marker not in body
        assert not is_unsafe_catalog_body(body)

    def test_native_catalog_body_rejects_unsafe_llm_context(self):
        unsafe = "التوفر قيد التحقق — أي نوع تبيه؟"
        body = resolve_native_catalog_body_text(
            context_reply=unsafe,
            inbound_customer_message="مرحبا",
        )
        assert body == TECHNICAL_CATALOG_BODY


class TestCatalogMultiItem:
    @staticmethod
    def _match(_db: Any, _t: int, rid: str) -> MagicMock:
        m = MagicMock()
        m.matched = True
        m.match_field = "product.external_id"
        m.product_id = 100 if rid == "86bqzca62a" else 101
        m.variant_id = None
        m.product_title = rid
        m.catalog_price = 239.5 if rid == "86bqzca62a" else 126.0
        return m

    def test_catalog_order_two_items_persisted_to_order_line_items(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        meta = _two_item_meta()

        order = persist_catalog_order_immediate_draft(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            inbound_metadata=meta,
            customer=customer,
            phone="+966500000001",
            message_event_id=501,
        )
        db.commit()
        assert order is not None
        assert len(order.line_items or []) == 2

    def test_catalog_order_two_items_visible_in_order_detail(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        order = persist_catalog_order_immediate_draft(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            inbound_metadata=_two_item_meta(),
            customer=customer,
            phone="+966500000001",
            message_event_id=502,
        )
        db.commit()
        assert order is not None
        payload = _serialise_order(
            order,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
            db=db,
            tenant_id=tenant.id,
        )
        detailed = payload.get("line_items") or payload.get("detailed_items") or []
        assert len(detailed) == 2

    def test_catalog_order_two_items_order_context_reads_all(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        persist_catalog_order_immediate_draft(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            inbound_metadata=_two_item_meta(),
            customer=customer,
            phone="+966500000001",
            message_event_id=503,
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
        assert len(ctx.active_draft.line_items) == 2

    def test_catalog_order_two_items_ai_facts_include_all_items(self):
        state = MerchantConversationState(stage="ordering")
        prep = OrderPreparationState(
            line_items=[
                {"product_name": "A", "quantity": 1, "unit_price": 239.5},
                {"product_name": "B", "quantity": 1, "unit_price": 126.0},
            ],
            catalog_checkout_total=365.5,
            catalog_checkout_currency="SAR",
        )
        state.order_prep = prep
        state.cart_items = list(prep.line_items)
        facts = build_catalog_order_compose_facts(
            state=state,
            inbound_metadata=_two_item_meta(),
        )
        assert facts is not None
        assert facts["line_items_count"] == 2
        assert len(facts["line_items"]) == 2
        assert facts["is_multi_item"] is True
        assert facts["total_amount"] == pytest.approx(365.5)

    def test_catalog_order_multi_item_does_not_use_single_product_focus_as_truth(self):
        meta = _two_item_meta()
        payload = parse_native_catalog_order({"product_items": meta["product_items"]})
        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=self._match):
            resolution = build_line_items_from_payload(MagicMock(), 1, payload)
        order_prep = {
            "line_items": resolution.line_items,
            "catalog_line_items_authoritative": True,
            "product_id": "100",
            "product_name": "500 جرام العسل الصيفي",
        }
        brain_state = {
            "cart_items": resolution.line_items,
            "current_product_focus": {
                "id": "100",
                "title": "500 جرام العسل الصيفي",
                "price": 239.5,
            },
        }
        items, _, _ = build_line_items_from_order_prep(
            order_prep=order_prep,
            brain_state=brain_state,
        )
        assert len(items) == 2

    def test_focus_summary_uses_all_line_items_not_single_price(self):
        summary = _focus_summary({
            "current_product_focus": {
                "title": "500 جرام العسل الصيفي",
                "price": 239.5,
                "currency": "SAR",
            },
            "order_prep": {
                "line_items": [
                    {"product_name": "500 جرام العسل الصيفي", "unit_price": 239.5, "quantity": 1},
                    {"product_name": "250 جرام سدر", "unit_price": 126.0, "quantity": 1},
                ],
                "catalog_checkout_total": 365.5,
                "catalog_checkout_currency": "SAR",
            },
        })
        assert summary["line_items_count"] == 2
        assert summary["is_multi_item"] is True
        assert "250 جرام سدر" in summary["selected_product"]
        assert summary["price"] == pytest.approx(365.5)


class TestOrderListActivitySort:
    def test_updated_open_draft_sorts_to_top_but_displays_created_at(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        created_old = (now - timedelta(days=10)).isoformat()
        updated_new = now.isoformat()

        old_updated = Order(
            tenant_id=tenant.id,
            external_id="sort-old",
            status="draft",
            source="whatsapp",
            extra_metadata={
                "created_at": created_old,
                "draft_created_at": created_old,
                "last_updated_at": (now - timedelta(hours=1)).isoformat(),
                "lifecycle": "whatsapp_draft",
            },
        )
        fresh = Order(
            tenant_id=tenant.id,
            external_id="sort-fresh",
            status="draft",
            source="whatsapp",
            extra_metadata={
                "created_at": created_old,
                "draft_created_at": created_old,
                "last_updated_at": updated_new,
                "lifecycle": "whatsapp_draft",
            },
        )
        db.add_all([old_updated, fresh])
        db.commit()

        rows = db.query(Order).filter_by(tenant_id=tenant.id).all()
        rows.sort(
            key=lambda o: (
                _read_last_updated_at(o, created_at=_read_created_at(o, fallback=now)),
                int(getattr(o, "id", 0) or 0),
            ),
            reverse=True,
        )
        assert rows[0].external_id == "sort-fresh"
        payload = _serialise_order(fresh, customer_lookup={}, now=now)
        assert payload["display_created_at"].startswith(created_old[:10])
        assert payload["last_updated_at"].startswith(updated_new[:10])

    def test_closed_order_new_catalog_creates_new_draft_and_sorts_top(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        now = datetime.now(timezone.utc)

        closed = Order(
            tenant_id=tenant.id,
            external_id=nahla_wa_external_id(tenant.id, convo.id),
            external_order_number="NHL-CLOSED",
            status="completed",
            total="100.00",
            source="whatsapp",
            extra_metadata={
                "lifecycle": "paid",
                "created_at": (now - timedelta(days=30)).isoformat(),
                "last_updated_at": (now - timedelta(days=30)).isoformat(),
            },
        )
        db.add(closed)
        db.commit()

        new_order = persist_catalog_order_immediate_draft(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            inbound_metadata=_two_item_meta(),
            customer=customer,
            phone="+966500000001",
            message_event_id=900,
        )
        db.commit()
        assert new_order is not None
        assert new_order.id != closed.id
        assert new_order.external_id == nahla_wa_catalog_external_id(
            tenant.id, convo.id, message_event_id=900,
        )

        rows = db.query(Order).filter_by(tenant_id=tenant.id).all()
        rows.sort(
            key=lambda o: (
                _read_last_updated_at(o, created_at=_read_created_at(o, fallback=now)),
                int(getattr(o, "id", 0) or 0),
            ),
            reverse=True,
        )
        assert rows[0].id == new_order.id
