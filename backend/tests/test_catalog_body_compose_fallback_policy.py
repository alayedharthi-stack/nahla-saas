"""Catalog interactive body must never carry compose-failure fallback text."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.fallback_policy import (  # noqa: E402
    EMPTY_REPLY_OPERATIONAL_AR,
    empty_reply_fallback,
    is_compose_failure_fallback,
)
from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: E402
    MINIMAL_CATALOG_BODY,
    TECHNICAL_CATALOG_BODY,
    is_unsafe_catalog_body,
    resolve_catalog_body_text,
    resolve_native_catalog_body_text,
)
from modules.ai.brain.turn.final_turn_audit import (  # noqa: E402
    audit_final_turn_reply,
    detect_final_turn_violations,
)
from modules.ai.brain.turn.final_turn_contract import FinalTurnContract  # noqa: E402
from services.whatsapp_platform.catalog_sender import (  # noqa: E402
    build_catalog_message_payload,
)


COMPOSE_FALLBACK = empty_reply_fallback()
COMPOSE_FALLBACK_WITH_EMOJI = f"{COMPOSE_FALLBACK} ✨"
BROWSE_INPUT = "وش الانواع المتوفره؟"


class TestComposeFallbackDetection:
    def test_detects_operational_compose_fallback(self) -> None:
        assert is_compose_failure_fallback(COMPOSE_FALLBACK) is True
        assert is_compose_failure_fallback(COMPOSE_FALLBACK_WITH_EMOJI) is True

    def test_normal_reply_not_compose_fallback(self) -> None:
        assert is_compose_failure_fallback("المنتجات متاحة في الكتالوج") is False


class TestCatalogBodyMustNotUseComposeFallback:
    def test_catalog_body_rejects_compose_failure_fallback(self) -> None:
        assert is_unsafe_catalog_body(COMPOSE_FALLBACK) is True
        body = resolve_catalog_body_text("", context_reply=COMPOSE_FALLBACK)
        assert COMPOSE_FALLBACK not in body
        assert body == MINIMAL_CATALOG_BODY

    def test_native_catalog_body_rejects_compose_failure_with_emoji(self) -> None:
        body = resolve_native_catalog_body_text(
            context_reply=COMPOSE_FALLBACK_WITH_EMOJI,
            inbound_customer_message=BROWSE_INPUT,
        )
        assert "تعذّرت صياغة" not in body
        assert body == MINIMAL_CATALOG_BODY

    def test_catalog_message_payload_uses_neutral_body_not_fallback(self) -> None:
        payload = build_catalog_message_payload(
            to="966500000000",
            thumbnail_product_retailer_id="sku-1",
            body_text=COMPOSE_FALLBACK_WITH_EMOJI,
        )
        interactive_body = payload["interactive"]["body"]["text"]
        assert "تعذّرت صياغة" not in interactive_body
        assert interactive_body == MINIMAL_CATALOG_BODY

    def test_valid_context_reply_still_used(self) -> None:
        ctx = "المنتجات متاحة في الكتالوج"
        body = resolve_native_catalog_body_text(
            context_reply=ctx,
            inbound_customer_message=BROWSE_INPUT,
        )
        assert body == ctx


class TestFallbackAllowedWhenNotCatalogBody:
    def test_compose_fallback_not_blocked_as_general_text(self) -> None:
        # Policy only applies at catalog body resolution — not wire text suppression.
        assert COMPOSE_FALLBACK == EMPTY_REPLY_OPERATIONAL_AR

    def test_technical_catalog_body_not_used_as_general_wire_reply(self) -> None:
        # Case B: compose failure fallback stays the operational error line for plain text.
        assert COMPOSE_FALLBACK != TECHNICAL_CATALOG_BODY
        assert TECHNICAL_CATALOG_BODY not in COMPOSE_FALLBACK
        assert "تعذّرت صياغة" in COMPOSE_FALLBACK


class TestExplicitBrowseCatalogDispatchPath:
    def test_try_send_native_catalog_entry_resolves_body(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, patch

        from routers import whatsapp_webhook as wh

        mock_result = type("R", (), {"success": True, "reason": "ok", "error": None})()

        from types import SimpleNamespace

        with patch(
            "services.whatsapp_platform.catalog_sender.send_catalog_message",
            new=AsyncMock(return_value=mock_result),
        ) as send_mock, patch(
            "core.native_catalog_capability.load_whatsapp_connection",
            return_value=SimpleNamespace(meta_catalog_id="CAT-1"),
        ), patch(
            "core.meta_catalog_membership.load_meta_catalog_membership",
            return_value=SimpleNamespace(catalog_id="CAT-1", retailer_id="sku-1"),
        ):
            asyncio.run(
                wh._try_send_native_catalog_entry(
                    db=object(),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    entry={"thumbnail_product_retailer_id": "sku-1", "catalog_id": "CAT-1"},
                    fallback_body=COMPOSE_FALLBACK_WITH_EMOJI,
                )
            )
        send_mock.assert_awaited_once()
        sent_body = send_mock.await_args.kwargs.get("body_text") or send_mock.await_args[1].get("body_text")
        assert sent_body == MINIMAL_CATALOG_BODY
        assert "تعذّرت صياغة" not in str(sent_body)


    def test_try_send_native_catalog_entry_fails_closed_on_catalog_switch(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from routers import whatsapp_webhook as wh

        send_mock = AsyncMock()
        with patch(
            "services.whatsapp_platform.catalog_sender.send_catalog_message",
            new=send_mock,
        ), patch(
            "core.native_catalog_capability.load_whatsapp_connection",
            return_value=SimpleNamespace(meta_catalog_id="CAT-B"),
        ), patch(
            "core.meta_catalog_membership.load_meta_catalog_membership",
            return_value=SimpleNamespace(catalog_id="CAT-A", retailer_id="sku-1"),
        ):
            result = asyncio.run(
                wh._try_send_native_catalog_entry(
                    db=object(),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    entry={
                        "thumbnail_product_retailer_id": "sku-1",
                        "catalog_id": "CAT-A",
                    },
                    fallback_body="ok",
                )
            )
        assert result.success is False
        assert result.reason == "catalog_id_mismatch"
        send_mock.assert_not_awaited()

    def test_try_send_native_catalog_entry_fails_closed_without_membership(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from routers import whatsapp_webhook as wh

        send_mock = AsyncMock()
        with patch(
            "services.whatsapp_platform.catalog_sender.send_catalog_message",
            new=send_mock,
        ), patch(
            "core.native_catalog_capability.load_whatsapp_connection",
            return_value=SimpleNamespace(meta_catalog_id="CAT-A"),
        ), patch(
            "core.meta_catalog_membership.load_meta_catalog_membership",
            return_value=None,
        ):
            result = asyncio.run(
                wh._try_send_native_catalog_entry(
                    db=object(),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    entry={
                        "thumbnail_product_retailer_id": "sku-1",
                        "catalog_id": "CAT-A",
                    },
                    fallback_body="ok",
                )
            )
        assert result.success is False
        assert result.reason == "meta_catalog_unverified"
        send_mock.assert_not_awaited()

    def test_try_send_native_catalog_entry_fails_closed_without_bound_catalog(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from routers import whatsapp_webhook as wh

        send_mock = AsyncMock()
        with patch(
            "services.whatsapp_platform.catalog_sender.send_catalog_message",
            new=send_mock,
        ), patch(
            "core.native_catalog_capability.load_whatsapp_connection",
            return_value=SimpleNamespace(meta_catalog_id="CAT-A"),
        ), patch(
            "core.meta_catalog_membership.load_meta_catalog_membership",
            return_value=SimpleNamespace(catalog_id="CAT-A", retailer_id="sku-1"),
        ):
            result = asyncio.run(
                wh._try_send_native_catalog_entry(
                    db=object(),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    entry={
                        "thumbnail_product_retailer_id": "sku-1",
                    },
                    fallback_body="ok",
                )
            )
        assert result.success is False
        assert result.reason == "catalog_id_missing"
        send_mock.assert_not_awaited()

    def test_try_send_native_catalog_entry_invalidates_on_products_not_found(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from routers import whatsapp_webhook as wh

        from unittest.mock import Mock

        failed = SimpleNamespace(
            success=False,
            reason="meta_products_not_found",
            error=None,
        )
        invalidate = Mock(return_value=1)
        with patch(
            "services.whatsapp_platform.catalog_sender.send_catalog_message",
            new=AsyncMock(return_value=failed),
        ) as send_mock, patch(
            "core.native_catalog_capability.load_whatsapp_connection",
            return_value=SimpleNamespace(meta_catalog_id="CAT-A"),
        ), patch(
            "core.meta_catalog_membership.load_meta_catalog_membership",
            return_value=SimpleNamespace(catalog_id="CAT-A", retailer_id="sku-1"),
        ), patch(
            "core.native_catalog_capability.invalidate_meta_catalog_publish_for_retailer_id",
            invalidate,
        ):
            result = asyncio.run(
                wh._try_send_native_catalog_entry(
                    db=object(),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    entry={
                        "thumbnail_product_retailer_id": "sku-1",
                        "catalog_id": "CAT-A",
                    },
                    fallback_body="ok",
                )
            )
        assert result.success is False
        assert result.reason == "meta_products_not_found"
        send_mock.assert_awaited_once()
        invalidate.assert_called_once()
        kwargs = invalidate.call_args.kwargs
        args = invalidate.call_args.args
        assert args[1] == 33
        assert args[2] == "sku-1"
        assert kwargs.get("catalog_id") == "CAT-A"

    def test_try_send_catalog_product_invalidates_on_single_product_131009(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch

        from routers import whatsapp_webhook as wh
        from services.catalog_product_orchestrator import (
            ProductCardSendAction,
            ProductCardSendDecision,
        )

        failed = SimpleNamespace(
            success=False,
            reason="meta_products_not_found",
            error=None,
        )
        invalidate = Mock(return_value=1)
        decision = ProductCardSendDecision(
            action=ProductCardSendAction.SEND_CATALOG,
            reason="ok",
            retailer_id="sku-1",
            tenant_send_ready=True,
            product_ready=True,
        )
        with patch(
            "services.whatsapp_platform.catalog_sender.send_single_product_message",
            new=AsyncMock(return_value=failed),
        ) as send_mock, patch(
            "services.catalog_product_orchestrator.evaluate_product_card_send",
            return_value=decision,
        ), patch(
            "core.native_catalog_capability.invalidate_meta_catalog_publish_for_retailer_id",
            invalidate,
        ):
            handled = asyncio.run(
                wh._try_send_catalog_product(
                    db=None,
                    connection=SimpleNamespace(meta_catalog_id="CAT-A"),
                    tenant_id=33,
                    phone_id="pid",
                    to="966500000000",
                    attachment={
                        "kind": "product_card",
                        "id": 28,
                        "title": "Generic cotton shirt",
                        "external_id": "sku-1",
                    },
                    positive_commerce_intent=True,
                )
            )
        assert handled is False
        send_mock.assert_awaited_once()
        invalidate.assert_called_once()
        args = invalidate.call_args.args
        kwargs = invalidate.call_args.kwargs
        assert args[1] == 33
        assert args[2] == "sku-1"
        assert kwargs.get("catalog_id") == "CAT-A"


class TestPhase31ShadowOnlyUnchanged:

    def test_final_turn_audit_does_not_mutate_reply(self) -> None:
        contract = FinalTurnContract(
            response_purpose="discovery",
            turn_owner="discovery",
            decision_action="search_products",
            decision_topic="discovery",
            browse_allowed=True,
        )
        original = COMPOSE_FALLBACK
        audit_final_turn_reply(
            contract,
            original,
            phase="post_compose",
            tenant_id=33,
        )
        assert original == COMPOSE_FALLBACK


class TestCourierRoleStillProtected:
    COURIER = "معاك مندوب سمسا"
    BAD_REPLY = "متوفر معاك مندوب سمسا بعدة خيارات. وش خيار تبيه؟"

    def test_courier_logistics_forbidden_in_contract(self) -> None:
        contract = FinalTurnContract(
            inbound_text=self.COURIER,
            response_purpose="general",
            turn_owner="general",
            decision_action="llm_reply",
            decision_topic="general",
            forbidden_question_types=["product", "variant", "catalog_promise", "availability"],
            promises_forbidden=["catalog_promise", "product_availability"],
        )
        assert "product" in contract.forbidden_question_types
        violations = detect_final_turn_violations(contract, self.BAD_REPLY)
        assert "forbidden_variant_followup" in violations
        assert "unsafe_product_availability_claim" in violations
