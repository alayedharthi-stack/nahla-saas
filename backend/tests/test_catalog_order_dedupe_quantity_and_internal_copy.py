"""Catalog order dedupe, quantity facts, internal-copy guard, and draft totals."""
from __future__ import annotations

import sys
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

from core.catalog_inbound_dedupe import dedupe_timeline_by_wa_message_id  # noqa: E402
from core.conversation_engine import StateManager  # noqa: E402
from core.outbound_leakage_firewall import firewall_outbound_text  # noqa: E402
from core.wa_catalog_order_immediate_draft import persist_catalog_order_immediate_draft  # noqa: E402
from core.wa_native_catalog_order import build_line_items_from_payload, parse_native_catalog_order  # noqa: E402
from models import Base, Conversation, Customer, MessageEvent, Order, Tenant  # noqa: E402
from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    maybe_enforce_catalog_order_continue_checkout,
)
from modules.ai.brain.commerce.catalog_order_facts import build_catalog_order_compose_facts  # noqa: E402
from modules.ai.brain.commerce.product_ordering_prompt import _next_missing_order_field  # noqa: E402
from modules.ai.brain.compose.templates import product_unsyncable  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    Decision,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.media.normalizer import _process_catalog_order  # noqa: E402


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


def _single_sku_meta(*, qty: int = 2, total: float = 319.0) -> dict:
    return {
        "source_type": "catalog_order",
        "line_items_count": 1,
        "total_quantity": qty,
        "total_price": total,
        "currency": "SAR",
        "wa_message_id": "test-wa-msg-catalog-001",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": qty,
                "item_price": total / qty if qty else total,
                "currency": "SAR",
                "name": "عسل",
            },
        ],
    }


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "false")


class TestCatalogOrderDedupe:
    def test_catalog_order_webhook_retry_does_not_duplicate_transcript(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        body = "[طلب كتالوج من العميل]\nإجمالي الكمية: 2"
        meta = {"wa_message_id": "test-wa-msg-retry-001", "source_type": "catalog_order"}

        StateManager.save_message(
            db, customer.phone, body, "inbound",
            conversation_id=convo.id, tenant_id=tenant.id, extra_metadata=meta,
        )
        StateManager.save_message(
            db, customer.phone, body, "inbound",
            conversation_id=convo.id, tenant_id=tenant.id, extra_metadata=meta,
        )

        rows = (
            db.query(MessageEvent)
            .filter_by(tenant_id=tenant.id, conversation_id=convo.id, direction="inbound")
            .all()
        )
        assert len(rows) == 1
        assert int((rows[0].extra_metadata or {}).get("inbound_retry_attempts") or 0) == 1

        timeline = [
            {"id": str(r.id), "direction": "in", "body": r.body or ""}
            for r in rows
        ]
        # Simulate historical duplicate rows still in DB for defense-in-depth.
        timeline.append({
            "id": "dup",
            "direction": "in",
            "body": body,
        })
        deduped = dedupe_timeline_by_wa_message_id(timeline, me_rows=rows)
        assert len([m for m in deduped if m["direction"] == "in"]) == 1

    def test_catalog_order_same_message_id_persist_is_idempotent(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        meta = _single_sku_meta()
        wamid = "test-wa-msg-idem-001"

        @staticmethod
        def _match(_db: Any, _t: int, _rid: str) -> MagicMock:
            m = MagicMock()
            m.matched = True
            m.match_field = "product.external_id"
            m.product_id = 100
            m.variant_id = None
            m.product_title = "عسل"
            m.catalog_price = 159.5
            return m

        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=_match):
            first = persist_catalog_order_immediate_draft(
                db,
                tenant_id=tenant.id,
                conversation=convo,
                inbound_metadata=meta,
                customer=customer,
                phone=customer.phone,
                message_event_id=101,
                source_message_key=wamid,
            )
            second = persist_catalog_order_immediate_draft(
                db,
                tenant_id=tenant.id,
                conversation=convo,
                inbound_metadata=meta,
                customer=customer,
                phone=customer.phone,
                message_event_id=202,
                source_message_key=wamid,
            )

        assert first is not None
        assert second is not None
        assert first.id == second.id
        order = db.query(Order).filter_by(tenant_id=tenant.id).one()
        items = list((order.extra_metadata or {}).get("line_items") or order.line_items or [])
        if not items:
            items = list(getattr(order, "line_items", None) or [])
        prep_items = list((order.extra_metadata or {}).get("order_prep", {}).get("line_items") or [])
        line_items = items or prep_items
        assert len(line_items) <= 1 or len([x for x in line_items if isinstance(x, dict)]) == 1


class TestCatalogOrderQuantity:
    @staticmethod
    def _match(_db: Any, _t: int, _rid: str) -> MagicMock:
        m = MagicMock()
        m.matched = True
        m.match_field = "product.external_id"
        m.product_id = 100
        m.variant_id = None
        m.product_title = "86bqzca62a"
        m.catalog_price = 159.5
        return m

    def test_catalog_order_single_sku_quantity_two_preserved(self):
        payload = parse_native_catalog_order(
            {"product_items": _single_sku_meta()["product_items"]},
        )
        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=self._match):
            resolution = build_line_items_from_payload(MagicMock(), 1, payload)
        assert len(resolution.line_items) == 1
        assert int(resolution.line_items[0]["quantity"]) == 2

        norm = _process_catalog_order(
            order_payload={"product_items": _single_sku_meta()["product_items"]},
            ts_raw=None,
            wa_msg_id="test-wa-msg-qty-001",
        )
        assert norm.metadata["line_items_count"] == 1
        assert norm.metadata["total_quantity"] == 2
        assert "319" in norm.text

    def test_catalog_order_quantity_not_missing_after_catalog_order(self):
        prep = OrderPreparationState(
            product_id="100",
            quantity=2,
            line_items=[{"product_retailer_id": "86bqzca62a", "quantity": 2, "unit_price": 159.5}],
        )
        state = MerchantConversationState(
            order_prep=prep,
            current_product_focus={"id": "100", "title": "عسل"},
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="[طلب كتالوج من العميل]",
            intent=None,
            facts={},
            state=state,
            profile={"inbound_metadata": _single_sku_meta()},
        )
        assert _next_missing_order_field(ctx) != "quantity"

    def test_catalog_order_facts_distinguish_line_count_and_total_quantity(self):
        meta = _single_sku_meta(qty=2, total=319.0)
        facts = build_catalog_order_compose_facts(inbound_metadata=meta)
        assert facts is not None
        assert facts["line_items_count"] == 1
        assert facts["total_quantity"] == 2
        assert facts["line_items"][0]["quantity"] == 2


class TestCatalogOrderReplyGuards:
    def test_customer_reply_blocks_internal_catalog_sync_phrase(self):
        leaky = (
            "تمام 🌷\n"
            "إذا استمرت المشكلة فقد يحتاج المتجر إلى مزامنة المنتجات من لوحة التحكم."
        )
        cleaned, scrubbed = firewall_outbound_text(leaky)
        assert scrubbed is True
        assert "لوحة التحكم" not in cleaned
        assert "مزامنة المنتجات" not in cleaned

        template = product_unsyncable({"title": "عسل"})
        assert "لوحة التحكم" not in template
        assert "مزامنة" not in template

    def test_catalog_order_reply_uses_missing_fields_not_browse_fallback(self):
        prep = OrderPreparationState(
            product_id="100",
            quantity=2,
            line_items=[{"product_retailer_id": "86bqzca62a", "quantity": 2}],
            missing_fields=["city"],
        )
        state = MerchantConversationState(
            order_prep=prep,
            current_product_focus={
                "id": "100",
                "title": "عسل",
                "from_catalog_order": True,
                "line_items_count": 1,
            },
            cart_items=prep.line_items,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="[طلب كتالوج من العميل]",
            intent=None,
            facts={},
            state=state,
            profile={"inbound_metadata": _single_sku_meta()},
        )
        browse = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "عسل"},
            reason="browse",
            confidence=0.9,
        )
        enforced = maybe_enforce_catalog_order_continue_checkout(ctx, browse)
        assert enforced.action != ACTION_SEARCH_PRODUCTS
        assert enforced.args.get("catalog_order_submitted") is True
        assert _next_missing_order_field(ctx) == "address"

    def test_catalog_order_draft_total_matches_quantity_line_total(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        convo = _seed_conversation(db, tenant.id, customer.id)
        meta = _single_sku_meta(qty=2, total=319.0)

        def _match(_db: Any, _t: int, _rid: str) -> MagicMock:
            m = MagicMock()
            m.matched = True
            m.match_field = "product.external_id"
            m.product_id = 100
            m.variant_id = None
            m.product_title = "عسل"
            m.catalog_price = 159.5
            return m

        _fixture_source_msg = "not-a-secret-fixture"
        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=_match):
            order = persist_catalog_order_immediate_draft(
                db,
                tenant_id=tenant.id,
                conversation=convo,
                inbound_metadata=meta,
                customer=customer,
                phone=customer.phone,
                message_event_id=55,
                source_message_key=_fixture_source_msg,
            )

        assert order is not None
        extra = dict(order.extra_metadata or {})
        catalog = dict(extra.get("catalog_order") or {})
        assert catalog.get("catalog_currency") == "SAR"
        assert float(catalog.get("catalog_total_price") or 0) == pytest.approx(319.0)
