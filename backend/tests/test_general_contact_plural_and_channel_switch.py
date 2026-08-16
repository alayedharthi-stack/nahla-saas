"""PR-A: general contact plural guard, channel switch, pending operational choice."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_SHOWROOM,
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    CheckoutChannelCapabilities,
    evaluate_checkout_route_owner,
)
from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: E402
    extract_staff_name_candidate,
    is_general_contact_numbers_request,
)
from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: E402
    PENDING_PICKUP_MAPS_OR_CONTACT,
    evaluate_pending_operational_choice_routing,
    is_pending_choice_confirmation,
    load_pending_operational_context,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    MSG_NAME_NOT_CONFIGURED,
    classify_staff_contact_request,
    resolve_staff_contact,
    compile_staff_contact_registry,
)


class _StubConv:
    def __init__(self) -> None:
        self.extra_metadata: dict = {}
        self.id = 1


class _StubDB:
    def __init__(self, brain_state: dict | None = None) -> None:
        self._brain_state = brain_state or {}
        self._conv = _StubConv()

    def add(self, _obj: object) -> None:
        pass

    def flush(self) -> None:
        pass


def _patch_brain_state(monkeypatch: pytest.MonkeyPatch, brain_state: dict) -> None:
    conv = _StubConv()
    conv.extra_metadata = {"brain_state": brain_state}

    def _load(_db: object, *, tenant_id: int, phone: str) -> tuple:
        del tenant_id, phone
        return conv, dict(brain_state)

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        _load,
    )


class TestContactPluralGuard:
    def test_send_numbers_is_general_not_named(self) -> None:
        msg = "ارسل الأرقام لاهنت"
        assert is_general_contact_numbers_request(msg) is True
        assert extract_staff_name_candidate(msg) == ""
        req = classify_staff_contact_request(msg)
        assert req.kind == "general_channel"
        assert req.kind != "named"

    def test_send_numbers_not_name_not_configured(self) -> None:
        msg = "ارسل الأرقام لاهنت"
        req = classify_staff_contact_request(msg)
        registry = compile_staff_contact_registry([], store_contact_phone="")
        resolution = resolve_staff_contact(registry, req, message=msg)
        assert MSG_NAME_NOT_CONFIGURED not in str(
            resolution.reason or "",
        )
        assert resolution.unknown_name is False

    @pytest.mark.parametrize(
        "msg",
        [
            "ارسل الرقم",
            "بيانات التواصل",
            "ارسل أرقامكم",
        ],
    )
    def test_general_contact_variants(self, msg: str) -> None:
        assert is_general_contact_numbers_request(msg) is True
        assert classify_staff_contact_request(msg).kind == "general_channel"

    def test_named_staff_still_named(self) -> None:
        msg = "أبي رقم هشام"
        assert is_general_contact_numbers_request(msg) is False
        assert extract_staff_name_candidate(msg) == "هشام"
        req = classify_staff_contact_request(msg)
        assert req.kind == "named"
        assert req.kind != "general_channel"
        registry = compile_staff_contact_registry([], store_contact_phone="")
        resolution = resolve_staff_contact(registry, req, message=msg)
        assert resolution.unknown_name is True
        assert resolution.reason == "name_not_configured"


class TestChannelSwitchAfterWhatsapp:
    def test_store_link_after_whatsapp_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"checkout_channel": CHECKOUT_CHANNEL_WHATSAPP},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=True,
            store_url="https://shop.example",
            store_name="Demo",
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
                message="المتجر الإلكتروني",
            )

        assert decision is not None
        assert decision.checkout_channel == CHECKOUT_CHANNEL_STORE
        assert decision.reason in {"store_link_delivered", "store_link_unavailable"}
        assert decision.cta_url == "https://shop.example"
        if decision.reason == "store_link_delivered":
            assert "https://shop.example" not in decision.reply_text
        persist.assert_called()

    def test_showroom_after_whatsapp_defers_when_branch_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"checkout_channel": CHECKOUT_CHANNEL_WHATSAPP},
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
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._branch_showroom_routing_available",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="زيارة المعرض",
            )

        assert decision is None

    def test_showroom_unavailable_when_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"checkout_channel": CHECKOUT_CHANNEL_WHATSAPP},
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
                message="زيارة المعرض",
            )

        assert decision is not None
        assert decision.reason == "showroom_visit_unavailable"
        assert "زيارة المعرض غير مهيأة" in decision.reply_text


class TestPendingOperationalChoice:
    def test_confirmation_detector(self) -> None:
        assert is_pending_choice_confirmation("نعم") is True
        assert is_pending_choice_confirmation("ارسل") is True
        assert is_pending_choice_confirmation("تمام") is True
        assert is_pending_choice_confirmation("اخبار العسل") is False

    def test_yes_consumes_pending_pickup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "order_prep": {
                    "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                    "pending_operational_branch_id": 7,
                },
            },
        )
        config = SimpleNamespace(
            maps_url="https://maps.example/pin",
            location_instructions_text="",
            location_response_mode="plus_reception",
        )
        target = SimpleNamespace(name="Showroom", wa_id="966500000099", raw_phone="0500000099")
        with patch(
            "modules.operations.branch_arrival_keyword_evidence.load_branch_action_config",
            return_value=config,
        ), patch(
            "modules.ai.brain.commerce.branch_trigger_router._build_reception_targets",
            return_value=(target, "تقدر تتواصل مع البائع."),
        ), patch(
            "modules.ai.brain.commerce.pending_operational_choice.clear_pending_operational_choice",
            return_value=True,
        ) as clear_mock:
            decision = evaluate_pending_operational_choice_routing(
                _StubDB(),
                tenant_id=10,
                message="نعم",
                customer_phone="966500000001",
            )

        assert decision is not None
        assert decision.reason == "pending_pickup_confirmed"
        assert decision.maps_url == "https://maps.example/pin"
        assert decision.use_cta is True
        clear_mock.assert_called_once()

    def test_send_consumes_pending_pickup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "order_prep": {
                    "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                    "pending_operational_branch_id": 3,
                },
            },
        )
        config = SimpleNamespace(
            maps_url="https://maps.example/branch",
            location_instructions_text="",
            location_response_mode="plus_instructions",
        )
        with patch(
            "modules.operations.branch_arrival_keyword_evidence.load_branch_action_config",
            return_value=config,
        ), patch(
            "modules.ai.brain.commerce.branch_trigger_router._build_reception_targets",
            return_value=(None, ""),
        ), patch(
            "modules.ai.brain.commerce.pending_operational_choice.clear_pending_operational_choice",
            return_value=True,
        ):
            decision = evaluate_pending_operational_choice_routing(
                _StubDB(),
                tenant_id=10,
                message="ارسل",
                customer_phone="966500000001",
            )

        assert decision is not None
        assert decision.reason == "pending_pickup_confirmed"
        assert "موقعنا" in decision.reply_text

    def test_load_pending_context(self) -> None:
        choice, branch_id = load_pending_operational_context(
            {
                "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                "pending_operational_branch_id": 12,
            },
        )
        assert choice == PENDING_PICKUP_MAPS_OR_CONTACT
        assert branch_id == 12
