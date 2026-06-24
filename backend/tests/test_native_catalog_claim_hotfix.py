"""
Hotfix tests — never expose catalog-claim text before Meta accepts catalog_message.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.native_catalog_capability import invalidate_meta_catalog_publish_for_retailer_id  # noqa: E402
from core.native_catalog_fallback import (  # noqa: E402
    NATIVE_CATALOG_SUCCESS_BODY_AR,
    compose_native_catalog_failure_decision,
    defer_native_catalog_customer_reply,
    is_native_catalog_claim_text,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    evaluate_checkout_route_owner,
    is_catalog_send_request,
    is_catalog_visibility_question,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE  # noqa: E402
from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from services.whatsapp_platform import catalog_sender as cs  # noqa: E402


@dataclass
class _Conn:
    meta_catalog_id: Optional[str] = "CAT-777"
    catalog_enabled: bool = True
    phone_number_id: str = "PHONE1"
    status: str = "connected"
    sending_enabled: bool = True


@dataclass
class _Product:
    id: int
    title: str
    tenant_id: int = 7
    meta_retailer_id: Optional[str] = "rid-1"
    meta_catalog_published_at: Any = field(
        default_factory=lambda: datetime(2026, 5, 30, tzinfo=timezone.utc)
    )


def _browse_ctx(*, db: Any = None) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery", turn=2)
    facts = CommerceFacts(has_products=True, product_count=5)
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message="وش عندكم منتجات؟",
        intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم منتجات؟"),
        state=state,
        facts=facts,
        history=[],
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


class TestCatalogNavigateDoesNotPreclaim:
    def test_native_catalog_entry_defers_customer_text(self):
        ctx = _browse_ctx(db=MagicMock())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "native_catalog_entry",
                "native_catalog_entry": {"thumbnail_product_retailer_id": "rid-1"},
            },
        )
        result = asyncio.run(CatalogNavigateHandler().handle(decision, ctx))
        assert result.success is True
        assert result.data["discovery_presentation_text"] == ""
        assert result.data["product_lines"] == ""
        assert result.data["native_catalog_entry"]["body_text"] != ""
        assert "تفضّل، اختر من الكتالوج" not in (
            result.data["native_catalog_entry"].get("body_text") or ""
        )


class TestResponderDoesNotPreclaim:
    def test_native_catalog_compose_returns_empty_reply(self):
        responder = DefaultComposer()
        ctx = _browse_ctx()
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={"chosen_path": "catalog_navigation_native_catalog"})
        result = ActionResult(
            success=True,
            data={
                "discovery_output_kind": "native_catalog",
                "discovery_presentation_text": "",
                "native_catalog_entry": {
                    "thumbnail_product_retailer_id": "rid-1",
                    "body_text": NATIVE_CATALOG_SUCCESS_BODY_AR,
                },
                "chosen_path": "catalog_navigation_native_catalog",
                "owner_locked": True,
            },
        )
        reply = asyncio.run(responder.compose(decision, result, ctx))
        assert reply == ""
        assert "تفضّل، اختر من الكتالوج" not in (reply or "")


class TestDeferHelper:
    def test_defer_strips_claim_when_native_catalog_pending(self):
        entry = {"thumbnail_product_retailer_id": "rid-1"}
        assert defer_native_catalog_customer_reply(NATIVE_CATALOG_SUCCESS_BODY_AR, native_catalog_entry=entry) == ""
        assert defer_native_catalog_customer_reply("مرحبا", native_catalog_entry=entry) == "مرحبا"

    def test_success_body_is_claim_text(self):
        assert is_native_catalog_claim_text("تفضّل، اختر من الكتالوج 👇")
        assert not is_native_catalog_claim_text(NATIVE_CATALOG_SUCCESS_BODY_AR)


class TestMetaFailureUsesHonestFallbackOnly:
    def test_compose_failure_has_no_catalog_claim(self):
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(whatsapp_fast=True),
        ):
            decision = compose_native_catalog_failure_decision(
                MagicMock(),
                7,
                failure_reason="meta_products_not_found",
            )
        assert "تفضّل، اختر من الكتالوج" not in decision.text
        assert "ما ظهر الكتالوج" in decision.text

    def test_publish_stamp_cleared_still_works(self):
        product = _Product(id=9, title="Rejected", meta_retailer_id="bad-rid")
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [product]
        cleared = invalidate_meta_catalog_publish_for_retailer_id(db, 7, "bad-rid")
        assert cleared == 1
        assert product.meta_catalog_published_at is None


class TestCheckoutRouteCatalogSendRequest:
    def test_send_catalog_is_not_visibility_help(self):
        assert is_catalog_send_request("ارسل كتالوج") is True
        assert is_catalog_visibility_question("ارسل كتالوج") is False

    def test_prior_catalog_failure_does_not_prebrain_send_catalog_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_checkout_route_context",
            return_value=("discovery", {"_native_catalog_send_failed": True}),
        ):
            decision = evaluate_checkout_route_owner(
                MagicMock(),
                tenant_id=7,
                customer_phone="966500000001",
                message="ارسل كتالوج",
            )
        assert decision is None

    def test_prior_catalog_visibility_uses_honest_fallback_not_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_checkout_route_context",
            return_value=("discovery", {"_native_catalog_send_failed": True}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(whatsapp_fast=True),
        ):
            decision = evaluate_checkout_route_owner(
                MagicMock(),
                tenant_id=7,
                customer_phone="966500000001",
                message="ما ظهر",
            )
        assert decision is not None
        assert decision.reason == "catalog_visibility_help_prior_catalog"
        assert "تفضّل، اختر من الكتالوج" not in decision.reply_text
        assert "ما ظهر الكتالوج" in decision.reply_text


class TestCatalogSenderSuccessBody:
    def test_success_catalog_message_uses_context_body_not_hardcoded_intro(
        self, monkeypatch,
    ):
        async def _fake_send(*_a, **_k):
            return {"messages": [{"id": "wamid.TEST123"}]}, {}

        monkeypatch.setattr(cs, "provider_send_message", _fake_send)
        body = "المنتجات متاحة في الكتالوج"
        result = asyncio.run(
            cs.send_catalog_message(
                MagicMock(),
                _Conn(),
                tenant_id=7,
                to="966500000000",
                phone_id="PHONE1",
                thumbnail_product_retailer_id="good-rid",
                body_text=body,
            )
        )
        assert result.success is True
        assert "تفضّل، اختر من الكتالوج" not in body
