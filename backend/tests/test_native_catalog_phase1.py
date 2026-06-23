"""
backend/tests/test_native_catalog_phase1.py
──────────────────────────────────────────
Phase 1 — WhatsApp Native Catalog Entry + Order Payload Parser tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.native_catalog_capability import (  # noqa: E402
    NativeCatalogCapability,
    REASON_META_CATALOG_UNPUBLISHED,
    REASON_SKU_ONLY_RETAILER_ID,
    REASON_SYNTHETIC_RETAILER_ID,
    _ineligibility_reason_from_inventory,
    _inventory_from_products,
    evaluate_native_catalog_capability,
    pick_thumbnail_retailer_id,
)
from core.wa_native_catalog_order import (  # noqa: E402
    NativeCatalogOrderPayload,
    apply_native_order_to_state,
    build_line_items_from_payload,
    match_retailer_id,
    parse_native_catalog_order,
)
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    STEP_NATIVE_CATALOG_ENTRY,
    STEP_SHOW_GROUPS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    maybe_enforce_catalog_order_continue_checkout,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_HANDOFF,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402
from services.whatsapp_platform import catalog_sender as cs  # noqa: E402


@dataclass
class _Conn:
    meta_catalog_id: Optional[str] = "CAT-1"
    catalog_enabled: bool = True
    phone_number_id: str = "PHONE1"
    status: str = "connected"
    sending_enabled: bool = True


@dataclass
class _Product:
    id: int
    title: str
    tenant_id: int = 1
    external_id: Optional[str] = None
    meta_retailer_id: Optional[str] = None
    sku: Optional[str] = None
    price: str = "69"
    in_stock: bool = True
    catalog_status: str = "active"
    default_variant_id: Optional[int] = None
    meta_catalog_published_at: Any = field(
        default_factory=lambda: datetime(2026, 5, 30, tzinfo=timezone.utc)
    )


@dataclass
class _Variant:
    id: int
    product_id: int
    tenant_id: int = 1
    retailer_id: Optional[str] = None
    price: str = "69"
    product: Optional[_Product] = None


def _run(coro):
    return asyncio.run(coro)


def _browse_ctx(*, db: Any = None) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery", turn=2)
    facts = CommerceFacts(has_products=True, product_count=5)
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966542980511",
        message="وش عندكم",
        intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم"),
        state=state,
        facts=facts,
        history=[],
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


class TestNativeCatalogCapability:
    def test_eligible_merchant(self):
        db = MagicMock()
        product = _Product(id=10, title="Item", external_id="sku-a")
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            product,
        ]
        db.query.return_value.filter.return_value.limit.return_value.first.return_value = None
        cap = evaluate_native_catalog_capability(db, 1, connection=_Conn())
        assert cap.eligible is True
        assert cap.reason == "ok"
        assert cap.thumbnail_retailer_id == "sku-a"

    def test_ineligible_when_catalog_disabled(self):
        cap = evaluate_native_catalog_capability(
            MagicMock(),
            1,
            connection=_Conn(catalog_enabled=False),
        )
        assert cap.eligible is False
        assert cap.reason == "catalog_disabled"

    def test_ineligible_when_only_synthetic_retailer_id(self):
        inv = _inventory_from_products([
            _Product(id=10, title="Item", meta_retailer_id="nahla_p_10"),
        ])
        assert _ineligibility_reason_from_inventory(inv) == REASON_SYNTHETIC_RETAILER_ID

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            _Product(id=10, title="Item", meta_retailer_id="nahla_p_10"),
        ]
        variant_q = db.query.return_value.join.return_value.filter.return_value
        variant_q.order_by.return_value.limit.return_value.all.return_value = []
        cap = evaluate_native_catalog_capability(db, 1, connection=_Conn())
        assert cap.eligible is False
        assert cap.reason == REASON_SYNTHETIC_RETAILER_ID

    def test_ineligible_when_sku_only_retailer_id(self):
        inv = _inventory_from_products([
            _Product(id=11, title="Item", sku="local-sku-only"),
        ])
        assert _ineligibility_reason_from_inventory(inv) == REASON_SKU_ONLY_RETAILER_ID

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            _Product(id=11, title="Item", sku="local-sku-only"),
        ]
        variant_q = db.query.return_value.join.return_value.filter.return_value
        variant_q.order_by.return_value.limit.return_value.all.return_value = []
        cap = evaluate_native_catalog_capability(db, 1, connection=_Conn())
        assert cap.eligible is False
        assert cap.reason == REASON_SKU_ONLY_RETAILER_ID

    def test_pick_thumbnail_skips_synthetic_variant_before_valid_variant(self):
        db = MagicMock()
        good_parent = _Product(id=11, title="Good", catalog_status="active")
        good_variant = _Variant(
            id=2,
            product_id=11,
            retailer_id="real-rid-11",
            product=good_parent,
        )
        variant_q = db.query.return_value.join.return_value.filter.return_value
        variant_q.order_by.return_value.limit.return_value.all.return_value = [good_variant]
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        rid = pick_thumbnail_retailer_id(db, 1)
        assert rid == "real-rid-11"


class TestNativeCatalogNavigation:
    def test_eligible_browse_routes_native_entry(self):
        ctx = _browse_ctx(db=MagicMock())
        cap = NativeCatalogCapability(
            eligible=True,
            reason="ok",
            thumbnail_retailer_id="rid-1",
            matchable_product_count=3,
        )
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=cap,
        ), patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=[{"group_name": "A"}],
        ), patch(
            "modules.ai.brain.catalog.navigation.evaluate_catalog_navigation_signals",
        ) as sig:
            sig.return_value = MagicMock(
                hard_blocked=False,
                advisory_or_comparison=False,
                catalog_browse_score=0.9,
                catalog_browse_intent=True,
                confidence=0.92,
                evidence={},
            )
            decision = try_catalog_navigation_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("navigator_step") == STEP_NATIVE_CATALOG_ENTRY

    def test_ineligible_browse_falls_back_to_groups(self):
        ctx = _browse_ctx(db=MagicMock())
        cap = NativeCatalogCapability(eligible=False, reason="catalog_disabled")
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=cap,
        ), patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=[{"group_name": "A"}],
        ), patch(
            "modules.ai.brain.catalog.navigation.evaluate_catalog_navigation_signals",
        ) as sig:
            sig.return_value = MagicMock(
                hard_blocked=False,
                advisory_or_comparison=False,
                catalog_browse_score=0.9,
                catalog_browse_intent=True,
                confidence=0.92,
                evidence={},
            )
            decision = try_catalog_navigation_decision(ctx)
        assert decision is not None
        assert decision.args.get("navigator_step") == STEP_SHOW_GROUPS

    def test_prior_native_failure_routes_top_fallback_not_native(self):
        ctx = _browse_ctx(db=MagicMock())
        ctx.state.native_catalog_send_failed = True
        cap = NativeCatalogCapability(
            eligible=True,
            reason="ok",
            thumbnail_retailer_id="rid-1",
            matchable_product_count=3,
        )
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=cap,
        ), patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=[{"group_name": "A"}],
        ), patch(
            "modules.ai.brain.catalog.navigation.evaluate_catalog_navigation_signals",
        ) as sig:
            sig.return_value = MagicMock(
                hard_blocked=False,
                advisory_or_comparison=False,
                catalog_browse_score=0.9,
                catalog_browse_intent=True,
                confidence=0.92,
                evidence={},
            )
            decision = try_catalog_navigation_decision(ctx)
        assert decision is not None
        from modules.ai.brain.catalog.navigation import STEP_TOP_FALLBACK  # noqa: PLC0415

        assert decision.args.get("navigator_step") == STEP_TOP_FALLBACK


class TestNativeCatalogDoesNotStealOtherFlows:
    """Phase 1 adds a qualified path only — Salla links and product info stay intact."""

    def _eligible_cap(self) -> NativeCatalogCapability:
        return NativeCatalogCapability(
            eligible=True,
            reason="ok",
            thumbnail_retailer_id="rid-1",
            matchable_product_count=3,
        )

    def _decision_for(self, message: str):
        ctx = _browse_ctx(db=MagicMock())
        ctx.message = message
        ctx.intent.raw_message = message
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=self._eligible_cap(),
        ), patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=[{"group_name": "A"}],
        ):
            return try_catalog_navigation_decision(ctx)

    def test_product_information_question_not_native_catalog(self):
        decision = self._decision_for("هل هو خام")
        assert decision is None

    def test_store_link_request_not_native_catalog(self):
        decision = self._decision_for("ابي رابط المتجر")
        assert decision is None

    def test_product_link_request_not_native_catalog(self):
        decision = self._decision_for("رابط المنتج")
        assert decision is None

    def test_general_browse_still_native_when_eligible(self):
        decision = self._decision_for("وش عندكم")
        assert decision is not None
        assert decision.args.get("navigator_step") == STEP_NATIVE_CATALOG_ENTRY


class TestNativeCatalogOrderParser:
    def test_single_item_payload(self):
        payload = parse_native_catalog_order(
            {
                "catalog_id": "CAT-1",
                "text": "بدون بصل",
                "product_items": [
                    {
                        "product_retailer_id": "var-rid",
                        "quantity": 1,
                        "item_price": 69.0,
                        "currency": "SAR",
                    }
                ],
            }
        )
        assert isinstance(payload, NativeCatalogOrderPayload)
        assert len(payload.items) == 1
        assert payload.items[0].product_retailer_id == "var-rid"
        assert payload.customer_note == "بدون بصل"

    def test_multi_item_payload(self):
        payload = parse_native_catalog_order(
            {
                "catalog_id": "CAT-1",
                "product_items": [
                    {"product_retailer_id": "a", "quantity": 1, "item_price": 10, "currency": "SAR"},
                    {"product_retailer_id": "b", "quantity": 2, "item_price": 20, "currency": "SAR"},
                ],
            }
        )
        assert len(payload.items) == 2

    def test_variant_match_priority(self):
        db = MagicMock()
        variant = _Variant(
            id=5,
            product_id=10,
            retailer_id="variant-rid",
            product=_Product(id=10, title="Variant product"),
        )
        db.query.return_value.join.return_value.filter.return_value.first.return_value = variant
        match = match_retailer_id(db, 1, "variant-rid")
        assert match.matched is True
        assert match.match_field == "variant.retailer_id"
        assert match.variant_id == 5

    def test_unmatched_item_needs_review(self):
        db = MagicMock()
        db.query.return_value.join.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.side_effect = [None, None, None]
        payload = parse_native_catalog_order(
            {
                "product_items": [
                    {"product_retailer_id": "missing", "quantity": 1, "item_price": 10, "currency": "SAR"},
                ],
            }
        )
        result = build_line_items_from_payload(db, 1, payload)
        assert len(result.line_items) == 1
        assert result.unmatched_count == 1
        assert result.line_items[0]["match_status"] == "needs_review"
        assert not result.line_items[0].get("product_id")

    def test_price_mismatch_flags_review(self):
        db = MagicMock()
        product = _Product(id=10, title="P", meta_retailer_id="rid-10", price="50")
        db.query.return_value.join.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = product
        payload = parse_native_catalog_order(
            {
                "product_items": [
                    {"product_retailer_id": "rid-10", "quantity": 1, "item_price": 99, "currency": "SAR"},
                ],
            }
        )
        result = build_line_items_from_payload(db, 1, payload)
        assert result.price_mismatch_count == 1
        assert result.line_items[0]["match_status"] == "needs_review"
        assert result.line_items[0]["price_mismatch"] is True

    def test_apply_native_order_sets_line_items_and_stage(self):
        db = MagicMock()
        product = _Product(id=10, title="P", external_id="ext-10", price="69")
        db.query.return_value.join.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = product
        state = MerchantConversationState()
        payload = parse_native_catalog_order(
            {
                "product_items": [
                    {"product_retailer_id": "ext-10", "quantity": 2, "item_price": 69, "currency": "SAR"},
                ],
            }
        )
        apply_native_order_to_state(db=db, tenant_id=1, state=state, payload=payload)
        assert len(state.order_prep.line_items) == 1
        assert state.order_prep.product_id == "10"
        assert state.stage == "ordering"
        assert state.current_product_focus is not None


class TestCatalogMessageSender:
    def test_build_catalog_message_payload_shape(self):
        payload = cs.build_catalog_message_payload(
            to="966500000000",
            thumbnail_product_retailer_id="thumb-rid",
            body_text="تفضّل",
        )
        assert payload["type"] == "interactive"
        assert payload["interactive"]["type"] == "catalog_message"
        assert payload["interactive"]["action"]["parameters"]["thumbnail_product_retailer_id"] == "thumb-rid"

    def test_send_catalog_message_success(self, monkeypatch):
        async def _fake_send(*args, **kwargs):
            return {"messages": [{"id": "wamid.TEST123"}]}, {}

        monkeypatch.setattr(cs, "provider_send_message", _fake_send)
        result = _run(
            cs.send_catalog_message(
                MagicMock(),
                _Conn(),
                tenant_id=1,
                to="966500000000",
                phone_id="PHONE1",
                thumbnail_product_retailer_id="thumb-rid",
                body_text="تفضّل",
            )
        )
        assert result.success is True
        assert result.reason == "sent"

    def test_send_catalog_message_meta_products_not_found(self, monkeypatch):
        async def _fake_send(*args, **kwargs):
            return {
                "error": {
                    "message": "(#131009) Parameter value is not valid",
                    "type": "OAuthException",
                    "code": 131009,
                    "error_data": {
                        "details": "Products not found in FB Catalog",
                    },
                }
            }, {}

        monkeypatch.setattr(cs, "provider_send_message", _fake_send)
        result = _run(
            cs.send_catalog_message(
                MagicMock(),
                _Conn(),
                tenant_id=1,
                to="966500000000",
                phone_id="PHONE1",
                thumbnail_product_retailer_id="missing-rid",
                body_text="تفضّل",
            )
        )
        assert result.success is False
        assert result.reason == "meta_products_not_found"
        assert result.fallback_recommended is True


class TestNativeOrderDoesNotReaskProduct:
    def test_pipeline_helper_wires_metadata(self):
        from modules.ai.brain.pipeline import _maybe_apply_native_catalog_order  # noqa: E402

        db = MagicMock()
        product = _Product(id=10, title="P", external_id="ext-10", price="69")
        db.query.return_value.join.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = product
        state = MerchantConversationState()
        _maybe_apply_native_catalog_order(
            db=db,
            tenant_id=1,
            message="[طلب كتالوج من العميل]\nعدد المنتجات: 1",
            state=state,
            inbound_metadata={
                "source_type": "catalog_order",
                "product_items": [
                    {"product_retailer_id": "ext-10", "quantity": 1, "item_price": 69, "currency": "SAR"},
                ],
            },
        )
        assert state.order_prep.line_items
        assert state.order_prep.product_id == "10"
        assert state.current_product_focus is not None
        assert state.order_prep.order_status == "awaiting_address"

    def test_catalog_order_submitted_overrides_catalog_navigation(self, monkeypatch):
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        state = MerchantConversationState()
        state.stage = "ordering"
        state.current_product_focus = {
            "id": 10,
            "external_id": "ext-10",
            "title": "عسل طلح",
            "from_native_catalog_order": True,
        }
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966551308005",
            message="[طلب كتالوج من العميل]",
            history=[],
            state=state,
            intent=Intent(name="start_order", confidence=0.9),
            facts=CommerceFacts(orderable=True),
            profile={
                "inbound_metadata": {
                    "source_type": "catalog_order",
                    "product_items": [
                        {
                            "product_retailer_id": "ext-10",
                            "quantity": 1,
                            "item_price": 69,
                            "currency": "SAR",
                        },
                    ],
                },
            },
        )
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={"chosen_path": "global_browse"},
            reason="legacy browse drift",
        )

        enforced = maybe_enforce_catalog_order_continue_checkout(ctx, decision)

        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert enforced.args["source"] == "catalog_order_submitted"
        assert enforced.args["continue_checkout"] is True
        assert enforced.args["native_catalog_order"]["event_type"] == "catalog_order_submitted"
        assert enforced.args["native_catalog_order"]["phone_source"] == "whatsapp"
        assert enforced.args["product"]["external_id"] == "ext-10"

    def test_catalog_order_submitted_overrides_product_search(self, monkeypatch):
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        state = MerchantConversationState()
        state.current_product_focus = {
            "id": 10,
            "external_id": "rid-10",
            "title": "عسل سدر",
            "from_native_catalog_order": True,
        }
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966551308005",
            message="[طلب كتالوج من العميل]",
            history=[],
            state=state,
            intent=Intent(name="ask_product", confidence=0.9),
            facts=CommerceFacts(orderable=True),
            profile={
                "inbound_metadata": {
                    "source_type": "catalog_order",
                    "product_items": [
                        {"product_retailer_id": "rid-10", "quantity": 2},
                    ],
                },
            },
        )

        enforced = maybe_enforce_catalog_order_continue_checkout(
            ctx,
            Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "المنتجات"}),
        )

        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert enforced.reason == "catalog_order_submitted → continue_checkout"

    def test_catalog_order_enforce_does_not_inject_canned_reply_or_sections(self, monkeypatch):
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        state = MerchantConversationState()
        state.current_product_focus = {
            "id": 10,
            "external_id": "rid-10",
            "title": "عسل طلح",
            "from_native_catalog_order": True,
        }
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966551308005",
            message="[طلب كتالوج من العميل]",
            history=[],
            state=state,
            intent=Intent(name="ask_product", confidence=0.9),
            facts=CommerceFacts(orderable=True),
            profile={
                "inbound_metadata": {
                    "source_type": "catalog_order",
                    "product_items": [{"product_retailer_id": "rid-10"}],
                },
            },
        )

        enforced = maybe_enforce_catalog_order_continue_checkout(
            ctx,
            Decision(action=ACTION_CATALOG_NAVIGATE, args={"chosen_path": "show_groups"}),
        )

        serialized_args = str(enforced.args)
        assert "reply_text" not in enforced.args
        assert "template" not in enforced.args
        assert "الأكثر مبيع" not in serialized_args
        assert "العسل بالكيلو" not in serialized_args
        assert "شنو" not in serialized_args
        assert "عايز" not in serialized_args

    def test_catalog_order_enforce_does_not_touch_staff_handoff(self, monkeypatch):
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        state = MerchantConversationState()
        state.current_product_focus = {"external_id": "rid-10", "title": "عسل"}
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966551308005",
            message="[طلب كتالوج من العميل]",
            history=[],
            state=state,
            intent=Intent(name="talk_to_human", confidence=0.9),
            facts=CommerceFacts(orderable=True),
            profile={
                "inbound_metadata": {
                    "source_type": "catalog_order",
                    "product_items": [{"product_retailer_id": "rid-10"}],
                },
            },
        )
        handoff = Decision(action=ACTION_HANDOFF, args={"reason": "explicit_staff"})

        assert maybe_enforce_catalog_order_continue_checkout(ctx, handoff) is handoff

    def test_catalog_order_flag_can_disable_enforce(self, monkeypatch):
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "false")
        state = MerchantConversationState()
        state.current_product_focus = {"external_id": "rid-10", "title": "عسل"}
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966551308005",
            message="[طلب كتالوج من العميل]",
            history=[],
            state=state,
            intent=Intent(name="ask_product", confidence=0.9),
            facts=CommerceFacts(orderable=True),
            profile={
                "inbound_metadata": {
                    "source_type": "catalog_order",
                    "product_items": [{"product_retailer_id": "rid-10"}],
                },
            },
        )
        browse = Decision(action=ACTION_CATALOG_NAVIGATE)

        assert maybe_enforce_catalog_order_continue_checkout(ctx, browse) is browse

    def test_repeated_catalog_order_keeps_same_nahla_draft_external_id(self):
        first = nahla_wa_external_id(33, 9063)
        second = nahla_wa_external_id(33, 9063)

        assert first == second == "nahla-wa-33-9063"
