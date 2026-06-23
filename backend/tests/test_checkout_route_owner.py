"""Checkout route owner + P0 address hijack regression tests."""
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

from core.wa_address_ingestion import (  # noqa: E402
    build_short_address_patch,
    is_bare_short_address_code,
    resolve_address_state_patch,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_INQUIRY,
    CHECKOUT_CHANNEL_SHOWROOM,
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    CheckoutChannelCapabilities,
    available_channels,
    build_channel_choice_buttons,
    build_channel_choice_prompt,
    evaluate_checkout_route_owner,
    has_checkout_entry_intent,
    has_checkout_route_intent,
    is_catalog_visibility_question,
    parse_checkout_channel_choice,
    should_defer_staff_location_for_checkout_route,
)
from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: E402
    should_yield_prebrain_to_order_flow,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    classify_staff_contact_request,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)


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


class TestCheckoutRouteIntent:
    def test_price_question_is_checkout_intent(self) -> None:
        assert has_checkout_route_intent("كم سعر العسل؟")
        assert not has_checkout_entry_intent("كم سعر العسل؟")

    def test_payment_question_is_checkout_intent(self) -> None:
        assert has_checkout_route_intent("وش طرق الدفع؟")
        assert not has_checkout_entry_intent("وش طرق الدفع؟")

    def test_start_order_is_checkout_entry_intent(self) -> None:
        assert has_checkout_entry_intent("ابي اطلب")
        assert has_checkout_entry_intent("أبغى أطلب")
        assert has_checkout_entry_intent("كيف أطلب")

    def test_greeting_is_not_checkout_intent(self) -> None:
        assert not has_checkout_route_intent("مرحبا")
        assert not has_checkout_entry_intent("مرحبا")

    def test_catalog_visibility_questions_are_detected(self) -> None:
        assert is_catalog_visibility_question("وين هي")
        assert is_catalog_visibility_question("؟")
        assert is_catalog_visibility_question("ما ظهر")


class TestChannelChoicePrompt:
    def test_prompt_includes_checkout_entry_options_when_configured(self) -> None:
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=True,
            store_url="https://shop.example",
        )
        prompt = build_channel_choice_prompt(caps)
        assert "طلب سريع عبر واتساب" in prompt
        assert "الطلب من المتجر الإلكتروني" in prompt
        assert "لدي استفسار" in prompt
        assert "زيارة المعرض" not in prompt

        buttons = build_channel_choice_buttons(caps)
        assert [b["reply"]["title"] for b in buttons] == [
            "طلب سريع واتساب",
            "فتح المتجر",
            "عندي استفسار",
        ]

    def test_prompt_omits_showroom_when_not_configured(self) -> None:
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=False,
            store_url="https://shop.example",
        )
        prompt = build_channel_choice_prompt(caps)
        assert "زيارة المعرض" not in prompt


class TestCheckoutRouteOwnerPreBrain:
    def test_start_order_prompts_channel_choice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(monkeypatch, {"stage": "discovery", "order_prep": {}})
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")

        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                showroom_visit=False,
                store_url="https://shop.example",
                store_name="متجر",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ) as persist:
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="ابي اطلب",
            )

        assert decision is not None
        assert decision.reason == "ask_checkout_channel"
        assert "كيف تحب تكمل؟" in decision.reply_text
        assert "طلب سريع عبر واتساب" in decision.reply_text
        assert "الطلب من المتجر الإلكتروني" in decision.reply_text
        assert "لدي استفسار" in decision.reply_text
        assert len(decision.buttons) == 3
        persist.assert_called_once()

    def test_price_ask_defers_to_brain_not_channel_choice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(monkeypatch, {"stage": "discovery", "order_prep": {}})
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")

        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                showroom_visit=False,
                store_url="https://shop.example",
                store_name="متجر",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ) as persist:
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="كم سعر العسل؟",
            )

        assert decision is None
        persist.assert_not_called()

    def test_store_link_choice_sends_link(
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
            showroom_visit=False,
            store_url="https://shop.example",
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
        assert decision.checkout_channel == CHECKOUT_CHANNEL_STORE
        assert "https://shop.example" in decision.reply_text
        assert decision.cta_url == "https://shop.example"
        assert decision.cta_label == "فتح المتجر الإلكتروني"

    def test_whatsapp_fast_asks_for_product(
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
            showroom_visit=True,
            store_url="https://shop.example",
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
                message="طلب سريع من واتساب",
            )

        assert decision is not None
        assert decision.checkout_channel == CHECKOUT_CHANNEL_WHATSAPP
        assert decision.reason == "whatsapp_fast_selected"
        assert "وش المنتج" in decision.reply_text

    def test_inquiry_choice_defers_to_brain(
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
            showroom_visit=False,
            store_url="https://shop.example",
        )

        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=caps,
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ) as persist:
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="لدي استفسار",
            )

        assert decision is None
        persist.assert_called_once()
        assert parse_checkout_channel_choice("3", caps=caps) == CHECKOUT_CHANNEL_INQUIRY

    def test_catalog_missing_question_repeats_help_with_buttons(
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
            showroom_visit=False,
            store_url="https://shop.example",
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
                message="وين هي",
            )

        assert decision is not None
        assert decision.reason == "catalog_visibility_help"
        assert "إذا ما ظهر لك الكتالوج" in decision.reply_text
        assert "عكبر" not in decision.reply_text
        assert "هلا قولي" not in decision.reply_text
        assert len(decision.buttons) == 3

    def test_question_mark_after_catalog_missing_not_broken_reply(
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

        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                showroom_visit=False,
                store_url="https://shop.example",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="؟",
            )

        assert decision is not None
        assert decision.reason == "catalog_visibility_help"
        assert "إذا ما ظهر لك الكتالوج" in decision.reply_text
        assert "هلا قولي" not in decision.reply_text

    def test_product_question_after_prompt_defers_to_brain(
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

        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                showroom_visit=False,
                store_url="https://shop.example",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ) as persist:
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="عندكم طلح؟",
            )

        assert decision is None
        persist.assert_called_once()

    def test_showroom_channel_allows_staff_policies(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"checkout_channel": CHECKOUT_CHANNEL_SHOWROOM},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        assert not should_defer_staff_location_for_checkout_route(
            _StubDB(),
            tenant_id=10,
            customer_phone="966500000001",
            message="وين المعرض؟",
        )


class TestPurchaseIntentBlocksStaff:
    def test_staff_deferred_for_start_order_without_channel(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(monkeypatch, {"stage": "discovery", "order_prep": {}})
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        assert should_defer_staff_location_for_checkout_route(
            _StubDB(),
            tenant_id=10,
            customer_phone="966500000001",
            message="ابي اطلب",
        )

    @patch("modules.ai.brain.commerce.staff_contact_evidence.load_staff_contact_registry")
    @patch("modules.ai.brain.commerce.staff_contact_policy._load_role_graph")
    def test_staff_policy_none_for_ambiguous_token(
        self,
        mock_role_graph,
        mock_registry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            StaffContactRegistry,
        )

        _patch_brain_state(
            monkeypatch,
            {
                "stage": "checkout",
                "order_prep": {
                    "product_name": "عسل",
                    "product_id": "1",
                    "missing_fields": ["delivery_address"],
                    "order_status": "awaiting_address",
                    "checkout_channel": CHECKOUT_CHANNEL_WHATSAPP,
                },
            },
        )
        mock_registry.return_value = StaffContactRegistry(records=(), store_contact_phone="")
        mock_role_graph.return_value = None

        decision = evaluate_staff_contact_policy(
            _StubDB(),
            tenant_id=10,
            message="RQWB3094",
            customer_phone="966542189781",
        )
        assert decision is None


class TestP0AddressHijack:
    def test_short_code_is_address_patch(self) -> None:
        assert is_bare_short_address_code("RQWB3094")
        patch = resolve_address_state_patch(
            inbound_normalized_type="text",
            inbound_text="RQWB3094",
        )
        assert patch is not None
        assert patch["short_address_code"] == "RQWB3094"

    def test_short_code_build_patch(self) -> None:
        patch = build_short_address_patch("RQWB3094")
        assert patch["delivery_address_type"] == "short_address_code"

    def test_short_code_yields_during_active_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "checkout",
                "order_prep": {
                    "product_name": "عسل",
                    "product_id": "1",
                    "missing_fields": ["delivery_address"],
                    "order_status": "awaiting_address",
                    "checkout_channel": CHECKOUT_CHANNEL_WHATSAPP,
                },
            },
        )
        assert should_yield_prebrain_to_order_flow(
            _StubDB(),
            tenant_id=10,
            customer_phone="966542189781",
            message="RQWB3094",
        )

    def test_maps_url_yields_during_active_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "checkout",
                "order_prep": {
                    "product_name": "عسل",
                    "product_id": "1",
                    "missing_fields": ["location"],
                    "order_status": "awaiting_address",
                },
            },
        )
        url = "https://maps.google.com/?q=24.7,46.6"
        assert should_yield_prebrain_to_order_flow(
            _StubDB(),
            tenant_id=10,
            customer_phone="966542189781",
            message=url,
        )


class TestStaffBoundary:
    def test_ambiguous_single_token_not_generic_staff(self) -> None:
        req = classify_staff_contact_request("RQWB3094")
        assert req.kind == "none"

    def test_bare_maps_url_not_generic_staff(self) -> None:
        req = classify_staff_contact_request("https://maps.google.com/?q=24.7,46.6")
        assert req.kind == "none"

    def test_parse_numeric_channel_choice(self) -> None:
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=True,
            store_url="https://shop.example",
        )
        assert parse_checkout_channel_choice("2", caps=caps) == CHECKOUT_CHANNEL_STORE
        assert available_channels(caps) == [
            CHECKOUT_CHANNEL_WHATSAPP,
            CHECKOUT_CHANNEL_STORE,
            CHECKOUT_CHANNEL_SHOWROOM,
        ]


class TestDuplicateVCardGate:
    @patch("modules.ai.brain.commerce.staff_contact_evidence.load_staff_contact_registry")
    @patch("modules.ai.brain.commerce.staff_contact_policy._load_role_graph")
    def test_repeat_staff_card_blocked_by_state(
        self,
        mock_role_graph,
        mock_registry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from test_staff_target_classifier import (  # noqa: PLC0415
            _install_call_resolver,
            _merchant_sections,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            compile_staff_contact_registry,
        )

        _install_call_resolver(monkeypatch)
        sections = _merchant_sections()
        registry = compile_staff_contact_registry(sections, store_contact_phone="")
        mock_registry.return_value = registry
        mock_role_graph.return_value = None

        _patch_brain_state(
            monkeypatch,
            {
                "staff_contacts_sent": [
                    {"name": "بائع المعرض", "phone": "966541690226", "turn": 3},
                ],
            },
        )

        decision = evaluate_staff_contact_policy(
            MagicMock(),
            tenant_id=33,
            message="أبي أكلم موظف",
            customer_phone="966549741354",
        )
        assert decision is not None
        assert decision.deliver_contact is False
        assert decision.reason == "contact_already_sent"
