from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import is_active_catalog_checkout  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.discovery.entry import resolve_discovery_entry, route_discovery_entry  # noqa: E402
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    has_explicit_product_browse_intent,
    product_discovery_block_reason,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


FORBIDDEN_PRODUCT_TEXT = (
    "المتوفر حاليًا",
    "المتوفر حاليا",
    "تحب أعرض لك الأسعار",
    "الأحجام المتوفرة",
)


def _ctx(message: str, *, intent: str = "general", state: MerchantConversationState | None = None) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        raw_message=message,
        intent=Intent(name=intent, confidence=0.8, raw_message=message),
        state=state or MerchantConversationState(stage="discovery", greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True, product_count=8, in_stock_count=8),
        profile={"inbound_metadata": {}},
    )


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("معك مندوب سمسا", "logistics_context"),
        ("I am a SMSA Express courier tracking number 123 PIN delivery code 456", "logistics_context"),
        ("ارسل الأرقام لاهنت", "contact_context"),
    ],
)
def test_non_product_contexts_do_not_browse(message: str, reason: str) -> None:
    ctx = _ctx(message)

    assert has_explicit_product_browse_intent(ctx) is False
    assert product_discovery_block_reason(ctx) == reason
    assert resolve_discovery_entry(ctx).matched is False


def test_social_and_unclear_honey_message_does_not_auto_browse() -> None:
    ctx = _ctx("اخبار العسل")
    dua = _ctx("اللهم صل وسلم على نبينا محمد")

    assert has_explicit_product_browse_intent(ctx) is False
    assert resolve_discovery_entry(ctx).matched is False
    assert product_discovery_block_reason(dua) == "social_context"


def test_unknown_fallback_does_not_browse() -> None:
    ctx = _ctx("تمام")

    assert has_explicit_product_browse_intent(ctx) is False
    assert resolve_discovery_entry(ctx).matched is False


def test_active_catalog_checkout_city_does_not_browse() -> None:
    state = MerchantConversationState(stage="ordering", greeted=True)
    state.order_prep = OrderPreparationState(
        catalog_line_items_authoritative=True,
        catalog_checkout_total=319.0,
        line_items=[{"product_name": "عسل سمر", "quantity": 1, "from_native_catalog_order": True}],
        missing_fields=["city", "delivery_address"],
    )
    ctx = _ctx("مكة بطحاء قريش", state=state)

    assert is_active_catalog_checkout(ctx) is True
    assert product_discovery_block_reason(ctx) == "active_catalog_checkout"
    assert resolve_discovery_entry(ctx).matched is False


def test_explicit_browse_prefers_native_catalog_action_when_available() -> None:
    ctx = _ctx("وش الأنواع المتوفرة؟")
    ctx._db = MagicMock()  # type: ignore[attr-defined]

    with (
        patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=[]),
        patch(
            "modules.ai.brain.catalog.navigation.evaluate_catalog_navigation_signals",
            return_value=SimpleNamespace(
                catalog_browse_intent=True,
                specific_product_target=False,
                product_information_question=False,
                shipping_or_order_status=False,
                support_or_staff_contact=False,
                advisory_or_comparison=False,
                navigation_state=False,
                hard_blocked=False,
                block_reason="",
                confidence=0.95,
                catalog_browse_score=0.95,
                exit_reason="",
                evidence={},
            ),
        ),
        patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=SimpleNamespace(
                eligible=True,
                reason="eligible",
                thumbnail_retailer_id="sku-1",
                matchable_product_count=5,
            ),
        ),
    ):
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        decision = route_discovery_entry(
            ctx,
            entry,
            facts=ctx.facts,
            product_discovery_blocked=lambda source: product_discovery_block_reason(ctx, source=source) is not None,
            fulfillment_locked_fallback=lambda: None,
            block_stale_resume=lambda _reason: False,
            is_commerce_blocked=lambda _ctx: False,
        )

    assert decision is not None
    assert decision.action == ACTION_CATALOG_NAVIGATE
    assert decision.args["navigator_step"] == "native_catalog_entry"


def test_specific_product_request_still_routes_to_product_search() -> None:
    ctx = _ctx("أبغى عسل سمر")
    entry = resolve_discovery_entry(ctx)
    decision = route_discovery_entry(
        ctx,
        entry,
        facts=ctx.facts,
        product_discovery_blocked=lambda source: product_discovery_block_reason(ctx, source=source) is not None,
        fulfillment_locked_fallback=lambda: None,
        block_stale_resume=lambda _reason: False,
        is_commerce_blocked=lambda _ctx: False,
    )

    assert has_explicit_product_browse_intent(ctx) is True
    assert decision is not None
    assert decision.action == ACTION_SEARCH_PRODUCTS
    assert decision.args.get("query") or decision.args.get("product_query")


def test_catalog_grounding_no_long_textual_availability_list() -> None:
    from modules.ai.brain.commerce.catalog_product_grounding import (  # noqa: PLC0415
        build_catalog_grounded_list_reply,
    )

    reply = build_catalog_grounded_list_reply(["عسل سمر", "عسل سدر"], category_hint="العسل")

    assert not any(marker in reply for marker in FORBIDDEN_PRODUCT_TEXT)
    assert "الكتالوج" in reply
