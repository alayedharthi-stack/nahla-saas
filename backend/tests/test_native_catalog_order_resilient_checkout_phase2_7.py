"""Phase 2.7 — resilient native catalog checkout path."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_native_catalog_order import RetailerMatchResult  # noqa: E402
from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: E402
    enrich_catalog_product_with_store_ids,
    is_catalog_checkout_product_question_forbidden,
    reply_contains_forbidden_catalog_product_question,
    resolve_store_external_id,
    safe_line_item_quantity,
    sanitize_forbidden_catalog_product_question,
    try_catalog_order_pre_brain_safe_reply,
)
from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: E402
    build_checkout_slot_fallback_reply,
)
from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.execution.orders import DraftOrderHandler  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.replies import build_catalog_order_start_reply  # noqa: E402

_LIVE_FALLBACK_TEXT = (
    "[طلب كتالوج من العميل]\n"
    "عدد أسطر الطلب: 2\n"
    "إجمالي الكمية: 2\n"
    "الإجمالي: 319 SAR\n"
    "رمز المنتج (SKU): ctv068l2de"
)

_FORBIDDEN_MARKERS = (
    "وش المنتج",
    "وش العدد",
    "وش الوزن",
    "باقي تحدد المنتج أو الكمية",
    "وش خيار يناسبك",
)


def _catalog_meta(*, skus: tuple[str, ...] = ("ctv068l2de", "lm70d804u8")) -> dict:
    items = [
        {
            "product_retailer_id": sku,
            "quantity": 1,
            "item_price": 126 if sku == skus[0] else 193,
            "currency": "SAR",
        }
        for sku in skus
    ]
    return {
        "source_type": "catalog_order",
        "normalized_type": "catalog_order",
        "product_items": items,
        "order": {"product_items": items},
        "total_price": 319.0,
        "currency": "SAR",
    }


def _assert_no_forbidden_product_prompt(text: str) -> None:
    blob = str(text or "")
    assert blob.strip()
    for marker in _FORBIDDEN_MARKERS:
        assert marker not in blob, f"forbidden marker {marker!r} in {blob!r}"


class TestSafeQuantityAndPromptDetection:
    def test_safe_line_item_quantity_accepts_decimal_strings(self) -> None:
        assert safe_line_item_quantity("2.5") == 2.5
        assert safe_line_item_quantity("2") == 2.0

    def test_decimal_quantity_display_in_catalog_reply(self) -> None:
        from modules.ai.brain.commerce.catalog_order_resilience import format_line_item_quantity  # noqa: PLC0415

        prep = {
            "line_items": [{"quantity": "2.5", "title": "عسل", "item_price": 100}],
            "order_flow_v2_trusted_price": True,
            "order_total": 250,
        }
        reply = build_catalog_order_start_reply(
            order_prep=prep,
            brain_state={},
            missing_fields=["customer_name"],
        )
        assert format_line_item_quantity(2.5) == "2.5"
        assert "× 2.5" in reply
        _assert_no_forbidden_product_prompt(reply)


class TestCatalogCheckoutForbiddenGuard:
    def test_forbidden_when_catalog_order_with_line_items(self) -> None:
        meta = _catalog_meta()
        assert is_catalog_checkout_product_question_forbidden(
            inbound_metadata=meta,
            message=_LIVE_FALLBACK_TEXT,
        )

    def test_allowed_for_normal_browse(self) -> None:
        assert not is_catalog_checkout_product_question_forbidden(
            inbound_metadata={},
            message="وش عندكم من منتجات",
        )


class TestOrderFlowV2CrashSafePath:
    def test_catalog_path_exception_returns_error_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
        monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")

        def _boom(*_a, **_k):
            raise RuntimeError("simulated catalog patch failure")

        with patch("modules.ai.order_flow_v2.owner._catalog_order_patch", side_effect=_boom):
            with patch("modules.ai.order_flow_v2.owner._load_brain_state", return_value=(None, {})):
                result = try_handle_order_flow_v2(
                    MagicMock(),
                    tenant_id=33,
                    customer_phone="966542980511",
                    message=_LIVE_FALLBACK_TEXT,
                    inbound_metadata=_catalog_meta(),
                )
        assert result.reason == "catalog_order_v2_error"
        assert result.handled is False


class TestDraftOrderHandlerCatalogSafe:
    def _ctx(self, *, meta: dict | None = None) -> BrainContext:
        prep = OrderPreparationState()
        state = MerchantConversationState(order_prep=prep)
        return BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message=_LIVE_FALLBACK_TEXT,
            intent=Intent(name="ask_product", confidence=0.8, raw_message=_LIVE_FALLBACK_TEXT),
            state=state,
            facts=SimpleNamespace(has_products=True),
            history=[],
            profile={"inbound_metadata": meta or _catalog_meta()},
        )

    def test_invalid_external_id_does_not_product_unsyncable(self) -> None:
        """Test 2 — native catalog line items without store external_id stay in checkout."""
        ctx = self._ctx()
        product = {
            "id": "ctv068l2de",
            "title": "ctv068l2de",
            "from_native_catalog_order": True,
            "line_items": _catalog_meta()["product_items"],
            "product_retailer_id": "ctv068l2de",
        }
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": product,
                "forced_product": product,
                "source": "catalog_order_submitted",
                "catalog_order_submitted": True,
            },
            reason="catalog_order_submitted → continue_checkout",
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        with patch(
            "modules.ai.brain.commerce.catalog_order_resilience.enrich_catalog_product_with_store_ids",
            return_value=product,
        ), patch(
            "modules.ai.brain.execution.orders._ensure_product_options_loaded",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._missing_checkout_fields",
            return_value=["customer_first_name"],
        ), patch(
            "modules.ai.brain.execution.orders._filter_missing_phone_if_known",
            side_effect=lambda missing, phone: missing,
        ), patch(
            "modules.ai.brain.execution.orders._resolve_checkout_address",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._seed_checkout_state",
        ), patch(
            "modules.ai.brain.commerce.cart_state.maybe_apply_cart_message",
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        assert result.success is True
        assert result.data.get("product_unsyncable") is not True
        assert result.data.get("catalog_checkout_safe") is not True

    def test_resolved_external_id_builds_valid_line_item(self) -> None:
        """Test 3 — retailer_id maps to catalog product and continues checkout."""
        ctx = self._ctx()
        product = {
            "id": "ctv068l2de",
            "title": "عسل",
            "external_id": "salla-123",
            "from_native_catalog_order": True,
            "line_items": _catalog_meta()["product_items"],
            "product_retailer_id": "ctv068l2de",
        }
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={"product": product, "source": "catalog_order_submitted"},
            reason="catalog_order_submitted → continue_checkout",
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        with patch(
            "modules.ai.brain.commerce.catalog_order_resilience.enrich_catalog_product_with_store_ids",
            return_value=product,
        ), patch(
            "modules.ai.brain.execution.orders._ensure_product_options_loaded",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._missing_checkout_fields",
            return_value=["customer_first_name"],
        ), patch(
            "modules.ai.brain.execution.orders._filter_missing_phone_if_known",
            side_effect=lambda missing, phone: missing,
        ), patch(
            "modules.ai.brain.execution.orders._resolve_checkout_address",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._seed_checkout_state",
        ), patch(
            "modules.ai.brain.commerce.cart_state.maybe_apply_cart_message",
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        assert result.data.get("product_unsyncable") is not True
        order_prep = result.data.get("order_prep") or {}
        assert order_prep.get("line_items")


class TestRetailerIdMapping:
    def test_resolve_store_external_id_from_match(self) -> None:
        db = MagicMock()
        product = SimpleNamespace(external_id="salla-999")
        db.query.return_value.filter.return_value.first.return_value = product
        with patch(
            "core.wa_native_catalog_order.match_retailer_id",
            return_value=RetailerMatchResult(matched=True, product_id=7),
        ):
            ext = resolve_store_external_id(db, 33, "ctv068l2de")
        assert ext == "salla-999"

    def test_enrich_catalog_product_adds_external_id(self) -> None:
        db = MagicMock()
        product = SimpleNamespace(external_id="salla-888")
        db.query.return_value.filter.return_value.first.return_value = product
        with patch(
            "core.wa_native_catalog_order.match_retailer_id",
            return_value=RetailerMatchResult(matched=True, product_id=3, product_title="عسل"),
        ):
            out = enrich_catalog_product_with_store_ids(
                db,
                33,
                {
                    "product_retailer_id": "ctv068l2de",
                    "line_items": [{"product_retailer_id": "ctv068l2de"}],
                },
            )
        assert out.get("external_id") == "salla-888"


class TestPreBrainSafeReply:
    def test_live_fallback_text_safe_reply(self) -> None:
        db = MagicMock()
        line_item = {
            "product_id": "12",
            "product_retailer_id": "ctv068l2de",
            "quantity": 1,
            "from_native_catalog_order": True,
            "external_id": "salla-12",
        }
        with patch("core.order_flow._load_brain_state", return_value=(None, {})):
            with patch(
                "core.wa_native_catalog_order.build_line_items_from_payload",
                return_value=SimpleNamespace(
                    line_items=[line_item],
                    unmatched_count=0,
                ),
            ):
                reply = try_catalog_order_pre_brain_safe_reply(
                    db,
                    tenant_id=33,
                    customer_phone="966542980511",
                    message=_LIVE_FALLBACK_TEXT,
                    inbound_metadata=_catalog_meta(),
                )
        _assert_no_forbidden_product_prompt(reply)

    def test_unresolved_sku_uses_extraction_fallback(self) -> None:
        db = MagicMock()
        with patch("core.order_flow._load_brain_state", return_value=(None, {})):
            with patch(
                "core.wa_native_catalog_order.build_line_items_from_payload",
                return_value=SimpleNamespace(line_items=[], unmatched_count=2),
            ):
                reply = try_catalog_order_pre_brain_safe_reply(
                    db,
                    tenant_id=33,
                    customer_phone="966542980511",
                    message=_LIVE_FALLBACK_TEXT,
                    inbound_metadata=_catalog_meta(),
                )
        _assert_no_forbidden_product_prompt(reply)
        assert "كتالوج" in reply


class TestSanitizeGuard:
    def test_sanitize_replaces_forbidden_prompt(self) -> None:
        meta = _catalog_meta()
        state = MerchantConversationState(
            order_prep=OrderPreparationState(line_items=list(meta["product_items"])),
        )
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message=_LIVE_FALLBACK_TEXT,
            intent=Intent(name="ask_product", confidence=0.8, raw_message=_LIVE_FALLBACK_TEXT),
            state=state,
            facts=SimpleNamespace(),
            history=[],
            profile={"inbound_metadata": meta},
        )
        safe = sanitize_forbidden_catalog_product_question(
            "حاضر، باقي تحدد المنتج أو الكمية عشان نكمل الطلب.",
            ctx=ctx,
        )
        _assert_no_forbidden_product_prompt(safe)


class TestBrowseUnchanged:
    def test_checkout_slot_fallback_still_prompts_product_outside_catalog(self) -> None:
        """Test 6 — normal browse/checkout without catalog_order is unchanged."""
        state = MerchantConversationState(
            stage="ordering",
            order_prep=OrderPreparationState(
                order_status="awaiting_product",
                missing_fields=["product"],
            ),
        )
        reply = build_checkout_slot_fallback_reply(state=state, inbound_text="ابي اطلب")
        assert reply is not None
        assert "باقي تحدد المنتج أو الكمية" in reply
