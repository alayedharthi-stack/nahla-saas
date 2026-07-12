"""
backend/tests/test_template_capability_filter.py
────────────────────────────────────────────────
PR1 — capability-aware Nahla library filtering (read-path only).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.merchant_capabilities import (
    MerchantCapabilities,
    capability_aware_templates_enabled,
    resolve_merchant_mode,
)
from services.whatsapp_templates.nahla_templates import get_all_templates, get_template_by_key
from services.whatsapp_templates.template_capability_filter import (
    filter_and_group_library_templates,
)
from services.whatsapp_templates.template_filter_metadata import (
    resolve_template_filter_meta,
    template_passes_capabilities,
)


def _caps(**kwargs) -> MerchantCapabilities:
    defaults = dict(
        has_external_store=False,
        supports_external_checkout=False,
        supports_external_coupons=False,
        supports_whatsapp_orders=False,
        supports_nahla_orders=False,
        supports_bank_transfer=False,
        supports_cod=False,
        has_whatsapp_catalog=False,
        has_external_tracking=False,
        has_nahla_tracking=False,
        has_payment_link=False,
    )
    defaults.update(kwargs)
    return MerchantCapabilities(**defaults)


WHATSAPP_ONLY = _caps(
    supports_whatsapp_orders=True,
    supports_nahla_orders=True,
    supports_bank_transfer=True,
    supports_cod=True,
)

EXTERNAL_STORE = _caps(
    has_external_store=True,
    supports_external_checkout=True,
    supports_external_coupons=True,
    has_external_tracking=True,
    has_payment_link=True,
    supports_cod=True,
)

HYBRID = _caps(
    has_external_store=True,
    supports_external_checkout=True,
    supports_external_coupons=True,
    has_external_tracking=True,
    has_payment_link=True,
    supports_whatsapp_orders=True,
    supports_nahla_orders=True,
    supports_cod=True,
)


class TestMerchantMode:
    def test_whatsapp_only(self):
        assert resolve_merchant_mode(WHATSAPP_ONLY) == "whatsapp_only"

    def test_external_store(self):
        assert resolve_merchant_mode(EXTERNAL_STORE) == "external_store"

    def test_hybrid(self):
        assert resolve_merchant_mode(HYBRID) == "hybrid"


class TestTemplateCapabilityGates:
    def test_checkout_template_hidden_without_checkout(self):
        tpl = get_template_by_key("abandoned_cart_reminder")
        assert tpl is not None
        meta = resolve_template_filter_meta(tpl)
        assert template_passes_capabilities(meta, EXTERNAL_STORE) is True
        assert template_passes_capabilities(meta, WHATSAPP_ONLY) is False

    def test_wa_draft_visible_for_whatsapp_only(self):
        tpl = get_template_by_key("wa_abandoned_order_draft")
        assert tpl is not None
        meta = resolve_template_filter_meta(tpl)
        assert template_passes_capabilities(meta, WHATSAPP_ONLY) is True
        assert template_passes_capabilities(meta, EXTERNAL_STORE) is False

    def test_tracking_template_hidden_without_tracking_capability(self):
        tpl = get_template_by_key("shipping_update")
        assert tpl is not None
        meta = resolve_template_filter_meta(tpl)
        assert template_passes_capabilities(meta, EXTERNAL_STORE) is True
        assert template_passes_capabilities(meta, WHATSAPP_ONLY) is False

    def test_cod_template_requires_cod_capability(self):
        tpl = get_template_by_key("cod_confirmation")
        assert tpl is not None
        meta = resolve_template_filter_meta(tpl)
        assert template_passes_capabilities(meta, _caps(supports_cod=True)) is True
        assert template_passes_capabilities(meta, _caps(supports_cod=False)) is False

    def test_coupon_template_hidden_without_coupon_support(self):
        tpl = get_template_by_key("seasonal_offer_template")
        assert tpl is not None
        meta = resolve_template_filter_meta(tpl)
        assert template_passes_capabilities(meta, EXTERNAL_STORE) is True
        assert template_passes_capabilities(meta, _caps(supports_external_checkout=True)) is False


class TestFilterAndGroup:
    def test_whatsapp_only_hides_external_cart_recovery(self):
        result = filter_and_group_library_templates(get_all_templates(), WHATSAPP_ONLY)
        keys = {t["key"] for t in result["templates"]}
        assert "abandoned_cart_reminder" not in keys
        assert "wa_abandoned_order_draft" in keys
        assert result["merchant_mode"] == "whatsapp_only"
        assert len(result["groups"]) == 1
        assert result["groups"][0]["channel"] == "whatsapp"

    def test_external_store_hides_whatsapp_draft_when_no_wa_orders(self):
        result = filter_and_group_library_templates(get_all_templates(), EXTERNAL_STORE)
        keys = {t["key"] for t in result["templates"]}
        assert "wa_abandoned_order_draft" not in keys
        assert "abandoned_cart_reminder" in keys

    def test_hybrid_returns_two_groups_without_duplicate_keys_in_flat_list(self):
        result = filter_and_group_library_templates(
            get_all_templates(),
            HYBRID,
            default_order_channel="adaptive",
        )
        assert result["merchant_mode"] == "hybrid"
        channels = {g["channel"] for g in result["groups"]}
        assert channels == {"external_store", "whatsapp"}
        flat_keys = [t["key"] for t in result["templates"]]
        assert len(flat_keys) == len(set(flat_keys))

    def test_default_order_channel_whatsapp_sorts_groups(self):
        result = filter_and_group_library_templates(
            get_all_templates(),
            HYBRID,
            default_order_channel="whatsapp",
        )
        assert result["groups"][0]["channel"] == "whatsapp"

    def test_bank_transfer_templates_not_shown_without_support(self):
        caps = _caps(supports_whatsapp_orders=True, supports_nahla_orders=True)
        result = filter_and_group_library_templates(get_all_templates(), caps)
        keys = {t["key"] for t in result["templates"]}
        assert "payment_reminder" not in keys


class TestKbUrlDoesNotActivateExternalCheckout:
    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    def test_kb_only_store_url_blocks_checkout_templates(
        self,
        mock_channels,
        mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from modules.ai.brain.commerce.sales_channel_capabilities import (
            MerchantSalesChannels,
            SalesChannelSlot,
        )
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities(
            provider="salla",
            has_active_commerce_integration=True,
        )
        mock_channels.return_value = MerchantSalesChannels(
            store_url="https://kb-only.example.com",
            store_url_source="kb_free_text",
            online_store=SalesChannelSlot(
                enabled=True,
                available=False,
                evidence="kb_free_text",
            ),
            whatsapp_quick_order=SalesChannelSlot(
                enabled=True,
                available=True,
                evidence="whatsapp_catalog_or_enabled",
            ),
        )
        db = MagicMock()
        caps = resolve_merchant_capabilities(db, 99)
        assert caps.supports_external_checkout is False
        assert caps.has_external_store is False

        result = filter_and_group_library_templates(get_all_templates(), caps)
        keys = {t["key"] for t in result["templates"]}
        assert "abandoned_cart_reminder" not in keys


class TestConservativeCapabilityDerivation:
    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    @patch("core.merchant_payment_methods.load_merchant_payment_methods")
    def test_checkout_without_coupon_adapter_capability_hides_coupons(
        self, mock_payment, mock_channels, mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from core.merchant_payment_methods import MerchantPaymentMethods
        from modules.ai.brain.commerce.sales_channel_capabilities import (
            MerchantSalesChannels,
            SalesChannelSlot,
        )
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities(
            provider="salla",
            has_active_commerce_integration=True,
            supports_coupon_redemption=False,
            supports_tracking_urls=True,
            supports_payment_link_generation=True,
        )
        mock_channels.return_value = MerchantSalesChannels(
            online_store=SalesChannelSlot(True, True, "store_url"),
        )
        mock_payment.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=[],
        )
        caps = resolve_merchant_capabilities(MagicMock(), 1)
        assert caps.supports_external_coupons is False
        tpl = get_template_by_key("seasonal_offer_template")
        assert tpl is not None
        assert template_passes_capabilities(resolve_template_filter_meta(tpl), caps) is False

    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    @patch("core.merchant_payment_methods.load_merchant_payment_methods")
    def test_checkout_without_tracking_adapter_capability_hides_tracking(
        self, mock_payment, mock_channels, mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from core.merchant_payment_methods import MerchantPaymentMethods
        from modules.ai.brain.commerce.sales_channel_capabilities import (
            MerchantSalesChannels,
            SalesChannelSlot,
        )
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities(
            provider="salla",
            has_active_commerce_integration=True,
            supports_tracking_urls=False,
            supports_payment_link_generation=True,
        )
        mock_channels.return_value = MerchantSalesChannels(
            online_store=SalesChannelSlot(True, True, "store_url"),
        )
        mock_payment.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=[],
        )
        caps = resolve_merchant_capabilities(MagicMock(), 1)
        assert caps.has_external_tracking is False
        tpl = get_template_by_key("shipping_update")
        assert tpl is not None
        assert template_passes_capabilities(resolve_template_filter_meta(tpl), caps) is False

    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    @patch("core.merchant_payment_methods.load_merchant_payment_methods")
    def test_checkout_alone_does_not_imply_payment_link_capability(
        self, mock_payment, mock_channels, mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from core.merchant_payment_methods import MerchantPaymentMethods
        from modules.ai.brain.commerce.sales_channel_capabilities import (
            MerchantSalesChannels,
            SalesChannelSlot,
        )
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities(
            provider="salla",
            has_active_commerce_integration=True,
            supports_payment_link_generation=False,
        )
        mock_channels.return_value = MerchantSalesChannels(
            online_store=SalesChannelSlot(True, True, "store_url"),
        )
        mock_payment.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=[],
        )
        caps = resolve_merchant_capabilities(MagicMock(), 1)
        assert caps.has_payment_link is False

    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    @patch("core.merchant_payment_methods.load_merchant_payment_methods")
    def test_moyasar_checkout_ready_enables_payment_link_capability(
        self, mock_payment, mock_channels, mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from core.merchant_payment_methods import MerchantPaymentMethods
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities()
        mock_channels.return_value = None
        mock_payment.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            moyasar_enabled=True,
            moyasar_checkout_ready=True,
            manual_payment_enabled=False,
            available_methods=["moyasar"],
        )
        caps = resolve_merchant_capabilities(MagicMock(), 1)
        assert caps.has_payment_link is True

    @patch("store_integration.adapter_capabilities.pick_active_commerce_integration")
    def test_non_commerce_integration_does_not_activate_external_store(
        self, mock_pick,
    ):
        from store_integration.adapter_capabilities import resolve_store_adapter_capabilities

        mock_pick.return_value = None
        caps = resolve_store_adapter_capabilities(MagicMock(), 1)
        assert caps.has_active_commerce_integration is False

    def test_adapter_declaring_capabilities_enables_coupon_and_tracking(self):
        from store_integration.adapter_capabilities import (
            StoreAdapterCapabilities,
            _declared_capabilities,
        )
        from store_adapters.salla_adapter import SallaAdapter

        declared = _declared_capabilities(SallaAdapter)
        assert declared.get("supports_coupon_redemption") is True
        assert declared.get("supports_tracking_urls") is True
        assert declared.get("supports_payment_link_generation") is True

        full = StoreAdapterCapabilities(
            provider="salla",
            has_active_commerce_integration=True,
            supports_coupon_redemption=True,
            supports_tracking_urls=True,
            supports_payment_link_generation=True,
        )
        tpl_coupon = get_template_by_key("seasonal_offer_template")
        tpl_track = get_template_by_key("shipping_update")
        assert tpl_coupon and tpl_track
        ext = _caps(
            has_external_store=True,
            supports_external_checkout=True,
            supports_external_coupons=True,
            has_external_tracking=True,
            has_payment_link=True,
        )
        assert template_passes_capabilities(resolve_template_filter_meta(tpl_coupon), ext)
        assert template_passes_capabilities(resolve_template_filter_meta(tpl_track), ext)

    @patch("store_integration.adapter_capabilities.resolve_store_adapter_capabilities")
    @patch("modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels")
    @patch("core.merchant_payment_methods.load_merchant_payment_methods")
    def test_missing_adapter_evidence_defaults_false(
        self, mock_payment, mock_channels, mock_store_caps,
    ):
        from core.merchant_capabilities import resolve_merchant_capabilities
        from core.merchant_payment_methods import MerchantPaymentMethods
        from store_integration.adapter_capabilities import StoreAdapterCapabilities

        mock_store_caps.return_value = StoreAdapterCapabilities(
            provider="unknown",
            has_active_commerce_integration=True,
        )
        mock_channels.return_value = None
        mock_payment.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=[],
        )
        caps = resolve_merchant_capabilities(MagicMock(), 1)
        assert caps.supports_external_coupons is False
        assert caps.has_external_tracking is False
        assert caps.has_payment_link is False
        assert caps.supports_external_checkout is False
        assert caps.supports_whatsapp_orders is False


class TestFeatureFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("CAPABILITY_AWARE_TEMPLATES", raising=False)
        assert capability_aware_templates_enabled() is False

    def test_enabled_when_env_set(self, monkeypatch):
        monkeypatch.setenv("CAPABILITY_AWARE_TEMPLATES", "true")
        assert capability_aware_templates_enabled() is True
