"""Catalog checkout — continue after national address without salla_retry."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

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
from core.order_context_prefill import build_checkout_compose_facts  # noqa: E402
from core.order_missing_fields_engine import compute_missing_fields  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    DraftOrderHandler,
    _checkout_continue_after_address_result,
    _should_continue_saved_open_checkout,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    build_product_claim_grounding_evidence,
    collect_saved_open_draft_grounded_prices,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    Decision,
    MerchantConversationState,
    OrderPreparationState,
)
from models import Base, Conversation, Customer, Order, Tenant  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402

_PRICE_GUARD_SNIPPET = "ما ظهر عندي سعر مؤكد من الكتالوج"
_SALLA_RETRY_SNIPPET = "أرسل أي رسالة وسأتابع"


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


def _seed_open_draft(
    db,
    *,
    total: str = "319.0",
    line_items: list | None = None,
) -> Tuple[Tenant, Conversation, Order]:
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    customer = Customer(
        tenant_id=tenant.id,
        phone="+966551234567",
        normalized_phone="966551234567",
        name="هشام",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    convo = Conversation(
        tenant_id=tenant.id,
        customer_id=customer.id,
        status="open",
        extra_metadata={},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)

    external_id = nahla_wa_external_id(tenant.id, convo.id)
    order = Order(
        tenant_id=tenant.id,
        external_id=external_id,
        status="pending_payment",
        source="whatsapp",
        total=total,
        line_items=line_items
        or [
            {
                "name": "عسل طلح",
                "quantity": 1,
                "price": 319.0,
                "product_retailer_id": "geuiu4knwm",
                "source": "whatsapp_native_catalog_order",
            }
        ],
        customer_info={
            "phone": "+966551234567",
            "name": "هشام",
            "city": "مكة المكرمة",
            "short_address_code": "MDQA5061",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "currency": "SAR",
            "missing_fields": [],
        },
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return tenant, convo, order


def _catalog_prep(*, short_code: str = "MDQA5061", city: str = "مكة المكرمة") -> OrderPreparationState:
    item = {
        "product_id": "140",
        "product_name": "عسل طلح",
        "quantity": 1,
        "unit_price": 319.0,
        "currency": "SAR",
        "product_retailer_id": "geuiu4knwm",
        "source": "whatsapp_native_catalog_order",
        "from_catalog_order": True,
    }
    return OrderPreparationState(
        line_items=[dict(item)],
        catalog_checkout_total=319.0,
        catalog_line_items_authoritative=True,
        customer_first_name="هشام",
        city=city,
        short_address_code=short_code,
        order_status="collecting_address",
        missing_fields=[],
    )


class TestShortAddressDoesNotReturnSallaRetry:
    def test_short_address_checkout_does_not_return_salla_retry_message(self) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        prep = _catalog_prep()
        ctx = SimpleNamespace(
            tenant_id=tenant.id,
            conversation_id=convo.id,
            _db=db,
        )
        draft = _should_continue_saved_open_checkout(
            ctx,
            prep,
            catalog_authoritative=True,
            order_context_update=True,
        )
        assert draft is not None
        result = _checkout_continue_after_address_result(
            product_info={"title": "عسل طلح"},
            prep=prep,
            draft=draft,
        )
        assert result.data.get("checkout_continue_after_address") is True
        assert "salla_retry" not in result.data

        retry_text = T.salla_retry_message(product={"title": "عسل"}, code="MDQA5061")
        assert _SALLA_RETRY_SNIPPET in retry_text

    def test_responder_routes_checkout_continue_to_llm_not_salla_retry(self) -> None:
        composer = DefaultComposer()
        ctx = MagicMock(spec=BrainContext)
        ctx.message = "MDQA5061"
        ctx.history = []
        ctx.tenant_id = 1
        ctx.customer_phone = "+966551234567"
        ctx.state = MerchantConversationState(order_prep=_catalog_prep())
        ctx.reply_state = SimpleNamespace(response_goal="confirm")
        ctx.intent = SimpleNamespace(name="ordering", slots={})
        ctx.facts = SimpleNamespace(store_url="", maps_url="")
        ctx.profile = {}

        result = ActionResult(
            success=True,
            data={
                "checkout_continue_after_address": True,
                "product": {"title": "عسل طلح"},
                "order_prep": _catalog_prep().to_dict(),
            },
        )
        decision = Decision(action="order_context_update", args={})

        with patch.object(
            DefaultComposer,
            "_llm_compose",
            new=AsyncMock(return_value="تمام هشام، إجمالي طلبك 319 ريال"),
        ) as mock_llm:
            text = asyncio.run(composer._compose_impl(decision, result, ctx))

        mock_llm.assert_awaited_once()
        assert _SALLA_RETRY_SNIPPET not in (text or "")


class TestHydrateSavedOpenOrder:
    def test_after_short_address_hydrates_saved_open_order_for_confirmation(self) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        prep = _catalog_prep()
        ctx = build_order_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            phone="+966551234567",
            brain_state={"order_prep": prep.to_dict()},
            message="MDQA5061",
        )
        facts = build_checkout_compose_facts(ctx, phone="+966551234567")
        assert facts.get("order_total_known") is True
        assert facts.get("order_total") == pytest.approx(319.0)
        assert facts.get("line_items_known") is True
        assert facts.get("known_city") == "مكة المكرمة"
        assert facts.get("known_short_address_code") == "MDQA5061"
        assert facts.get("known_phone") == "+966551234567"


class TestSavedDraftPriceGrounding:
    def test_saved_open_order_total_is_grounded_price_evidence(self) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        prices = collect_saved_open_draft_grounded_prices(
            db,
            tenant_id=tenant.id,
            conversation_id=convo.id,
        )
        assert 319 in prices

        evidence = build_product_claim_grounding_evidence(
            db,
            tenant.id,
            conversation_id=convo.id,
        )
        assert 319 in evidence.grounded_prices
        assert evidence.whatsapp_catalog_trusted is True

    def test_resume_after_address_does_not_say_no_grounded_catalog_price(self) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        reply = (
            "تمام هشام، إجمالي طلبك 319 ريال شامل الشحن. "
            "المدينة مكة المكرمة والرمز MDQA5061 — نكمل؟"
        )
        result = apply_product_claim_grounding_guard(
            reply=reply,
            db=db,
            tenant_id=tenant.id,
            conversation_id=convo.id,
            chosen_path="order_context_update",
        )
        assert not result.replaced
        assert _PRICE_GUARD_SNIPPET not in result.reply


class TestCheckoutConfirmationGoal:
    def test_checkout_after_address_sets_confirm_customer_order_and_shipping_details_once(
        self,
    ) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        prep = _catalog_prep()
        ctx = build_order_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            phone="+966551234567",
            brain_state={"order_prep": prep.to_dict()},
            message="MDQA5061",
        )
        result = compute_missing_fields(ctx)
        facts = build_checkout_compose_facts(ctx, phone="+966551234567")
        assert facts["next_goal"] == "confirm_customer_order_and_shipping_details_once"
        assert facts.get("ask_confirmation_once") is True
        assert facts.get("order_total_known") is True


class TestCatalogLineItemsSurviveUpdates:
    def test_catalog_line_items_and_total_survive_city_and_address_updates(self) -> None:
        db, _ = _make_db()
        tenant, convo, order = _seed_open_draft(db)
        prep = _catalog_prep(city="", short_code="")
        prep.city = "مكة المكرمة"
        ctx_city = build_order_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            phone="+966551234567",
            brain_state={"order_prep": prep.to_dict()},
            message="مكة المكرمة",
        )
        assert ctx_city.active_draft is not None
        assert ctx_city.active_draft.total == pytest.approx(319.0)
        assert len(ctx_city.active_draft.line_items) == 1

        prep.short_address_code = "MDQA5061"
        ctx_addr = build_order_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            phone="+966551234567",
            brain_state={"order_prep": prep.to_dict()},
            message="MDQA5061",
        )
        assert ctx_addr.active_draft is not None
        assert ctx_addr.active_draft.total == pytest.approx(319.0)
        assert len(ctx_addr.active_draft.line_items) == 1

        db.refresh(order)
        assert float(order.total) == pytest.approx(319.0)
        assert len(order.line_items or []) == 1


class TestNoFreeTextProductCapture:
    def test_no_free_text_product_capture_on_checkout_resume(self) -> None:
        db, _ = _make_db()
        tenant, convo, _order = _seed_open_draft(db)
        prep = _catalog_prep()
        state = MerchantConversationState(order_prep=prep, stage="ordering")
        ctx = BrainContext(
            tenant_id=tenant.id,
            conversation_id=convo.id,
            customer_id=1,
            customer_phone="+966551234567",
            message="MDQA5061",
            history=[],
            state=state,
            intent=SimpleNamespace(name="ordering", slots={"short_address_code": "MDQA5061"}),
            facts=SimpleNamespace(store_url="", maps_url=""),
            profile={"name": "هشام"},
        )
        ctx._db = db  # type: ignore[attr-defined]

        decision = Decision(
            action="propose_draft_order",
            args={
                "order_context_update": True,
                "product": {"title": "عسل طلح", "external_id": "140"},
            },
        )

        runtime = MagicMock()
        runtime.execute = AsyncMock(
            return_value=SimpleNamespace(
                ok=False,
                payload={},
                error="salla unavailable",
            )
        )

        with patch(
            "modules.ai.commerce.runtime.CommerceToolRuntime",
            return_value=runtime,
        ), patch(
            "modules.ai.brain.execution.orders._ensure_product_options_loaded",
            new=AsyncMock(),
        ), patch(
            "modules.ai.brain.execution.orders._resolve_checkout_address",
            new=AsyncMock(),
        ), patch(
            "modules.ai.brain.execution.orders._is_saudi_customer",
            return_value=True,
        ), patch(
            "modules.ai.brain.execution.orders._missing_checkout_fields",
            return_value=[],
        ), patch(
            "modules.ai.brain.execution.orders._missing_product_options",
            return_value=[],
        ), patch(
            "modules.ai.brain.execution.orders._resolve_options_payload",
            return_value=[],
        ), patch(
            "modules.ai.brain.execution.orders._seed_checkout_state",
        ), patch(
            "modules.ai.brain.commerce.cart_state.maybe_apply_cart_message",
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        assert result.success is True
        assert result.data.get("checkout_continue_after_address") is True
        assert result.data.get("needs_collection") is not True
        assert result.data.get("salla_retry") is not True
