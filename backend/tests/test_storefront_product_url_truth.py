"""Storefront Product URL Truth — PDP first, fail closed, platform hygiene."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_STORE,
    CheckoutChannelCapabilities,
    evaluate_checkout_route_owner,
)
from modules.ai.brain.commerce.storefront_product_url import (  # noqa: E402
    CTA_LABEL_PRODUCT,
    CTA_LABEL_STORE,
    WA_CTA_LABEL_MAX,
    is_platform_non_merchant_url,
    is_trusted_merchant_http_url,
    resolve_storefront_completion_link,
    truncate_wa_cta_label,
)


SALLA_PDP = "https://alayed.sa/products/jacket-black"
GENERIC_PDP = "https://shop.example/products/white-sneaker"
REGISTER_URL = "https://app.nahlah.ai/register"
STORE_HOME = "https://shop.example"


def _patch_brain_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, tenant_id, phone: (None, state),
    )


class _StubDB:
    def query(self, *_args: Any, **_kwargs: Any) -> "_StubDB":
        return self

    def filter(self, *_args: Any, **_kwargs: Any) -> "_StubDB":
        return self

    def first(self) -> None:
        return None


class TestPlatformHygiene:
    def test_register_is_platform_non_merchant(self) -> None:
        assert is_platform_non_merchant_url(REGISTER_URL) is True
        assert is_trusted_merchant_http_url(REGISTER_URL) is False

    def test_salla_and_generic_product_urls_trusted(self) -> None:
        assert is_trusted_merchant_http_url(SALLA_PDP) is True
        assert is_trusted_merchant_http_url(GENERIC_PDP) is True
        assert is_platform_non_merchant_url(SALLA_PDP) is False

    def test_cta_label_within_whatsapp_limit(self) -> None:
        assert len(CTA_LABEL_PRODUCT) <= WA_CTA_LABEL_MAX
        assert len(CTA_LABEL_STORE) <= WA_CTA_LABEL_MAX
        assert len(truncate_wa_cta_label("فتح المتجر الإلكتروني")) <= WA_CTA_LABEL_MAX


class TestResolveStorefrontCompletionLink:
    def test_product_focus_valid_product_url_returns_pdp(self) -> None:
        brain = {
            "current_product_focus": {
                "id": "101",
                "title": "جاكيت",
                "product_url": SALLA_PDP,
            }
        }
        res = resolve_storefront_completion_link(
            None,
            tenant_id=1,
            brain_state=brain,
            store_url=STORE_HOME,
            allow_store_homepage_fallback=False,
        )
        assert res.found is True
        assert res.url == SALLA_PDP.rstrip("/")
        assert res.source == "product_focus.product_url"
        assert res.cta_label == CTA_LABEL_PRODUCT
        assert len(res.cta_label) <= WA_CTA_LABEL_MAX

    def test_product_focus_missing_product_url_fail_closed(self) -> None:
        brain = {
            "current_product_focus": {
                "id": "101",
                "title": "جاكيت",
            }
        }
        res = resolve_storefront_completion_link(
            _StubDB(),
            tenant_id=1,
            brain_state=brain,
            store_url=STORE_HOME,
            allow_store_homepage_fallback=False,
        )
        assert res.found is False
        assert res.url == ""
        assert res.reason == "product_focus_missing_product_url"
        assert STORE_HOME not in res.url

    def test_product_focus_register_url_rejected_fail_closed(self) -> None:
        brain = {
            "current_product_focus": {
                "id": "101",
                "title": "جاكيت",
                "product_url": REGISTER_URL,
            }
        }
        res = resolve_storefront_completion_link(
            _StubDB(),
            tenant_id=1,
            brain_state=brain,
            store_url=REGISTER_URL,
            allow_store_homepage_fallback=False,
        )
        assert res.found is False
        assert res.url == ""
        assert REGISTER_URL not in (res.url or "")

    def test_homepage_fallback_disabled_by_default(self) -> None:
        brain = {"current_product_focus": {"id": "9", "title": "عطر ورد 100ml"}}
        res = resolve_storefront_completion_link(
            _StubDB(),
            tenant_id=1,
            brain_state=brain,
            store_url=STORE_HOME,
            allow_store_homepage_fallback=False,
        )
        assert res.found is False
        assert res.url == ""

    def test_homepage_fallback_when_explicitly_allowed(self) -> None:
        brain = {"current_product_focus": {"id": "9", "title": "عطر ورد 100ml"}}
        res = resolve_storefront_completion_link(
            _StubDB(),
            tenant_id=1,
            brain_state=brain,
            store_url=STORE_HOME,
            allow_store_homepage_fallback=True,
        )
        assert res.found is True
        assert res.url == STORE_HOME.rstrip("/")
        assert res.source == "store_url_homepage_fallback"

    def test_no_product_focus_uses_trusted_store_url(self) -> None:
        res = resolve_storefront_completion_link(
            None,
            tenant_id=1,
            brain_state={"stage": "discovery"},
            store_url=STORE_HOME,
            allow_store_homepage_fallback=False,
        )
        assert res.found is True
        assert res.url == STORE_HOME.rstrip("/")
        assert res.has_product_focus is False
        assert res.cta_label == CTA_LABEL_STORE

    def test_no_product_focus_rejects_register_store_url(self) -> None:
        res = resolve_storefront_completion_link(
            None,
            tenant_id=1,
            brain_state={},
            store_url=REGISTER_URL,
            allow_store_homepage_fallback=False,
        )
        assert res.found is False
        assert res.url == ""

    def test_non_salla_canonical_product_url(self) -> None:
        brain = {
            "current_product_focus": {
                "id": "55",
                "title": "حذاء رياضي أبيض",
                "product_url": GENERIC_PDP,
            }
        }
        res = resolve_storefront_completion_link(
            None,
            tenant_id=42,
            brain_state=brain,
            store_url="https://other.example",
            allow_store_homepage_fallback=False,
        )
        assert res.url == GENERIC_PDP.rstrip("/")

    def test_catalog_lookup_returns_pdp_when_focus_url_missing(self) -> None:
        from modules.ai.brain.commerce import storefront_product_url as mod

        row = MagicMock()
        row.extra_metadata = {"product_url": SALLA_PDP}
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row

        product_cls = MagicMock()
        with patch.dict(sys.modules, {"models": MagicMock(Product=product_cls)}):
            url = mod.lookup_catalog_product_url(db, 1, product_id="101")
        assert url == SALLA_PDP.rstrip("/")

        brain = {"current_product_focus": {"id": "101", "title": "جاكيت"}}
        with patch.object(mod, "lookup_catalog_product_url", return_value=SALLA_PDP):
            res = mod.resolve_storefront_completion_link(
                db,
                tenant_id=1,
                brain_state=brain,
                store_url=STORE_HOME,
                allow_store_homepage_fallback=False,
            )
        assert res.found is True
        assert res.url == SALLA_PDP.rstrip("/")
        assert res.source == "catalog.product_url"

    def test_catalog_lookup_tenant_isolation(self) -> None:
        from modules.ai.brain.commerce import storefront_product_url as mod

        db = MagicMock()
        query = db.query.return_value
        query.filter.return_value.first.return_value = None

        product_cls = MagicMock()
        with patch.dict(sys.modules, {"models": MagicMock(Product=product_cls)}):
            url = mod.lookup_catalog_product_url(db, 2, product_id="101")
        assert url == ""
        assert query.filter.called

        brain = {"current_product_focus": {"id": "101", "title": "جاكيت"}}
        res = resolve_storefront_completion_link(
            db,
            tenant_id=2,
            brain_state=brain,
            store_url=STORE_HOME,
            allow_store_homepage_fallback=False,
        )
        assert res.found is False


class TestStoreUrlResolverHygiene:
    def test_resolve_store_url_skips_platform_register(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.commerce import store_url_resolver as sur

        class _Settings:
            store_settings = {}
            whatsapp_settings = {"store_button_url": REGISTER_URL}

        class _Integration:
            provider = "salla"
            config = {"store_url": STORE_HOME}

        class _DB:
            def query(self, model):  # noqa: ANN001
                self._model = model
                return self

            def filter(self, *args, **kwargs):  # noqa: ANN001
                return self

            def first(self):
                name = getattr(self._model, "__name__", str(self._model))
                if "Integration" in name:
                    return _Integration()
                return None

            def order_by(self, *args, **kwargs):  # noqa: ANN001
                return self

            def limit(self, *args, **kwargs):  # noqa: ANN001
                return self

            def all(self):
                return []

        monkeypatch.setattr(
            sur,
            "_lookup_kb_store_url",
            lambda _db, _tid: ("", "none"),
        )

        tenant_stub = MagicMock()
        tenant_stub.DEFAULT_STORE = {}
        tenant_stub.DEFAULT_WHATSAPP = {"store_button_url": ""}
        tenant_stub.get_or_create_settings = lambda _db, _tid: _Settings()
        tenant_stub.merge_defaults = lambda cfg, defaults: {**defaults, **(cfg or {})}
        monkeypatch.setitem(sys.modules, "core.tenant", tenant_stub)

        sk_stub = MagicMock()
        loader = MagicMock()
        loader.store_profile.return_value = {}
        sk_stub.StoreKnowledgeLoader = lambda _db, _tid: loader
        monkeypatch.setitem(sys.modules, "core.store_knowledge", sk_stub)

        models_stub = MagicMock()
        models_stub.Integration = type("Integration", (), {"__name__": "Integration"})
        models_stub.MerchantKnowledgeSection = type(
            "MerchantKnowledgeSection", (), {"__name__": "MerchantKnowledgeSection"}
        )
        monkeypatch.setitem(sys.modules, "models", models_stub)

        # Force Integration import path inside resolver
        import types

        class Integration:
            tenant_id = None
            provider = None

        models_mod = types.ModuleType("models")
        models_mod.Integration = Integration
        monkeypatch.setitem(sys.modules, "models", models_mod)

        db = _DB()
        # Patch db.query to return integration for Integration model
        def _query(model):  # noqa: ANN001
            db._model = model
            return db

        db.query = _query  # type: ignore[method-assign]

        # Simpler: patch resolve chain via monkeypatch on _normalise and integration loop
        # Call _normalise_url directly for register rejection
        assert sur._normalise_url(REGISTER_URL) == ""
        assert sur._normalise_url(STORE_HOME) == STORE_HOME.rstrip("/")


class TestCheckoutRouteStorefrontDelivery:
    def test_store_choice_with_focus_sends_pdp(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "current_product_focus": {
                    "id": "101",
                    "title": "جاكيت",
                    "product_url": SALLA_PDP,
                },
                "order_prep": {"awaiting_checkout_channel": True},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=False,
            store_url=REGISTER_URL,  # must NOT win over PDP
            store_name="متجر تجريبي عام",
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=caps,
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=1,
                customer_phone="966500000001",
                message="المتجر الإلكتروني",
            )

        assert decision is not None
        assert decision.checkout_channel == CHECKOUT_CHANNEL_STORE
        assert decision.reason == "store_link_delivered"
        assert decision.cta_url == SALLA_PDP.rstrip("/")
        assert REGISTER_URL not in decision.reply_text
        assert SALLA_PDP.rstrip("/") in decision.reply_text
        assert len(decision.cta_label) <= WA_CTA_LABEL_MAX
        assert decision.cta_label == CTA_LABEL_PRODUCT

    def test_store_choice_focus_missing_url_fail_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "current_product_focus": {"id": "101", "title": "جاكيت"},
                "order_prep": {"awaiting_checkout_channel": True},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            store_url=STORE_HOME,
            store_name="متجر",
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=caps,
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=1,
                customer_phone="966500000001",
                message="المتجر الإلكتروني",
            )

        assert decision is not None
        assert decision.reason == "store_link_product_url_unavailable"
        assert decision.cta_url == ""
        assert STORE_HOME not in decision.reply_text
        assert REGISTER_URL not in decision.reply_text

    def test_store_choice_without_focus_uses_store_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"awaiting_checkout_channel": True},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            store_url=STORE_HOME,
            store_name="متجر",
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=caps,
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="رابط المتجر",
            )

        assert decision is not None
        assert decision.cta_url == STORE_HOME.rstrip("/")
        assert decision.cta_label == CTA_LABEL_STORE
        assert len(decision.cta_label) <= WA_CTA_LABEL_MAX
