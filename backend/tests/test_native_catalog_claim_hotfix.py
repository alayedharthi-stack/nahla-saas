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
    def test_native_catalog_entry_uses_minimal_body_not_customer_echo(self):
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
        body = result.data["native_catalog_entry"].get("body_text") or ""
        assert body != ctx.message
        assert body == "."
        assert "تفضّل، اختر من الكتالوج" not in body


def _fake_tool_execution_result(products: list):
    from modules.ai.commerce.runtime import ToolExecutionResult

    return ToolExecutionResult(
        ok=True,
        tool_name="search_products",
        payload={"products": products, "count": len(products), "query": ""},
    )


class TestNativeCatalogThumbnailUnavailableTextFallback:
    """Root-cause regression (2026-07-26): when the native/Meta catalog
    thumbnail can't be resolved (unpublished catalog, missing connection,
    synthetic/sku-only retailer ids...), the reply must be a real
    product-search text list — never the bare native-catalog body
    ("اختر المنتجات المناسبة من القائمة") with no list attached.

    ``CommerceToolRuntime.execute`` is patched rather than exercised via a
    real DB/query chain here — that executor's own correctness (FTS/ILIKE,
    ``get_top_products``, ``_filter_orderable``) already has dedicated
    coverage elsewhere; this test is scoped to *this* method's routing and
    payload-shaping contract only.
    """

    def test_no_thumbnail_with_orderable_products_returns_real_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_products = [
            {"id": 1, "title": "حذاء رياضي أبيض", "price": "199", "external_id": "ext-1"},
            {"id": 2, "title": "حذاء رياضي أسود", "price": "179", "external_id": "ext-2"},
        ]

        async def _fake_execute(_self, _tool_name, _payload):
            return _fake_tool_execution_result(fake_products)

        monkeypatch.setattr(
            "modules.ai.commerce.runtime.CommerceToolRuntime.execute",
            _fake_execute,
        )
        ctx = _browse_ctx(db=MagicMock())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "native_catalog_entry",
                # No thumbnail_product_retailer_id at all — mirrors the
                # production fallback decision built when native capability
                # is ineligible (e.g. meta_catalog_unpublished).
                "native_catalog_entry": {},
            },
        )
        result = asyncio.run(CatalogNavigateHandler().handle(decision, ctx))
        assert result.success is True
        assert result.data.get("chosen_path") == "catalog_navigation_top_products_fallback"
        # Trusted product facts are carried for the persona surface...
        products = result.data.get("products") or []
        assert len(products) == 2
        assert {p["title"] for p in products} == {"حذاء رياضي أبيض", "حذاء رياضي أسود"}
        assert result.data.get("discovery_output_kind") == "products"
        # ...and the executor authors NO customer-facing text of its own
        # (wording is owned by the catalog-navigation persona composer).
        assert (result.data.get("product_lines") or "") == ""
        assert (result.data.get("discovery_presentation_text") or "") == ""
        # And must not silently claim a native catalog was attached.
        assert not result.data.get("native_catalog_entry")

    def test_no_thumbnail_with_no_orderable_products_yields_empty_not_fake_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _fake_execute(_self, _tool_name, _payload):
            return _fake_tool_execution_result([])

        monkeypatch.setattr(
            "modules.ai.commerce.runtime.CommerceToolRuntime.execute",
            _fake_execute,
        )
        ctx = _browse_ctx(db=MagicMock())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={"navigator_step": "native_catalog_entry", "native_catalog_entry": {}},
        )
        result = asyncio.run(CatalogNavigateHandler().handle(decision, ctx))
        assert result.success is True
        assert (result.data.get("products") or []) == []
        assert not (result.data.get("product_lines") or "").strip()
        assert result.data.get("discovery_output_kind") == "empty"


# Literal production decision args for the native-catalog send path, as built by
# ``commerce_entry_catalog_delivery._resolve_send_catalog`` when native catalog
# capability is ineligible (e.g. meta_catalog_unpublished). Copied verbatim —
# the earlier regression tests used a shape without ``chosen_path``, which does
# not occur in production and hid a provenance defect.
PRODUCTION_SEND_CATALOG_ARGS = {
    "catalog_delivery_kind": "send_catalog",
    "commerce_entry_owner": "commerce_entry_catalog",
    "navigator_step": "native_catalog_entry",
    "turn_owner": "commerce_entry_catalog_delivery",
    "owner_locked": True,
    "chosen_path": "commerce_entry_send_catalog",
    "owner_step": "send_catalog",
}

PATH_TOP_FALLBACK_VALUE = "catalog_navigation_top_products_fallback"
PERSONA_COMPOSED_REPLY = "PERSONA_COMPOSED_BROWSE_REPLY"
LEGACY_NO_SECTIONS_SENTENCE = "ما ظهر عندي أقسام واضحة حالياً."


class TestNativeCatalogFallbackFinalTextProvenance:
    """AGENTS.md 'final customer text provenance' — with the *production*
    decision shape, the reply for a native-catalog-unavailable browse turn
    must be composed by the catalog-navigation persona surface, never by a
    deterministic string built inside the executor."""

    def _patch_persona(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        seen: dict = {}

        async def _fake_compose(**kwargs):
            seen.update(kwargs)
            return (
                PERSONA_COMPOSED_REPLY,
                None,
                {"compose_source": "persona_llm", "chosen_path": kwargs.get("chosen_path")},
            )

        monkeypatch.setattr(
            "modules.ai.brain.persona.catalog_product_answer"
            ".try_compose_catalog_navigation_browse_answer",
            _fake_compose,
        )
        return seen

    def _run_production_turn(self, products: list, monkeypatch: pytest.MonkeyPatch):
        async def _fake_execute(_self, _tool_name, _payload):
            return _fake_tool_execution_result(products)

        monkeypatch.setattr(
            "modules.ai.commerce.runtime.CommerceToolRuntime.execute",
            _fake_execute,
        )
        ctx = _browse_ctx(db=MagicMock())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args=dict(PRODUCTION_SEND_CATALOG_ARGS),
        )

        async def _run():
            executed = await CatalogNavigateHandler().handle(decision, ctx)
            reply = await DefaultComposer().compose(decision, executed, ctx)
            return executed, reply

        return asyncio.run(_run())

    def test_production_shape_with_products_reply_is_persona_composed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = self._patch_persona(monkeypatch)
        products = [
            {"id": 1, "title": "حذاء رياضي أبيض", "price": "199", "external_id": "e1", "can_checkout": True},
            {"id": 2, "title": "حذاء رياضي أسود", "price": "179", "external_id": "e2", "can_checkout": True},
        ]
        executed, reply = self._run_production_turn(products, monkeypatch)

        # Provenance actually reaches compose as the top-products fallback.
        assert executed.data.get("chosen_path") == PATH_TOP_FALLBACK_VALUE
        # Executor contributed facts only — no customer-facing text.
        assert (executed.data.get("product_lines") or "") == ""
        assert (executed.data.get("discovery_presentation_text") or "") == ""
        assert len(executed.data.get("products") or []) == 2
        # Persona surface received the trusted product facts and owns wording.
        assert len(seen.get("products") or []) == 2
        assert reply == PERSONA_COMPOSED_REPLY
        assert executed.data.get("compose_source") == "persona_llm"
        # No deterministic executor-authored list leaks to the customer.
        assert "•" not in reply
        assert "ريال" not in reply
        assert "199" not in reply and "179" not in reply

    def test_production_shape_without_products_is_persona_not_legacy_sentence(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_persona(monkeypatch)
        executed, reply = self._run_production_turn([], monkeypatch)

        assert executed.data.get("chosen_path") == PATH_TOP_FALLBACK_VALUE
        assert (executed.data.get("products") or []) == []
        assert (executed.data.get("product_lines") or "") == ""
        # Empty catalog is explained by the persona surface, not by the legacy
        # deterministic "no clear sections" sentence.
        assert reply == PERSONA_COMPOSED_REPLY
        assert LEGACY_NO_SECTIONS_SENTENCE not in reply
        assert executed.data.get("compose_source") == "persona_llm"

    def test_production_shape_does_not_claim_native_catalog_was_sent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_persona(monkeypatch)
        executed, _reply = self._run_production_turn(
            [{"id": 1, "title": "قميص قطني أزرق", "price": "120", "external_id": "e9", "can_checkout": True}],
            monkeypatch,
        )
        # No interactive-catalog payload, and the audit flag records why.
        assert not executed.data.get("native_catalog_entry")
        assert executed.data.get("native_catalog_thumbnail_unavailable") is True
        assert executed.data.get("discovery_output_kind") == "products"


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
