"""P0 — Taif checkout slot vs showroom routing + catalog discovery."""
from __future__ import annotations

import os
import sys
from typing import Any, Optional
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: E402
    has_explicit_showroom_pickup_intent,
    is_bare_city_token_message,
    should_defer_contact_routing_for_checkout_slot,
)
from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    global_availability_browse_requested,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    is_generic_category_noun,
    try_types_overview_decision,
)


def _checkout_brain_state() -> dict[str, Any]:
    return {
        "order_prep": {
            "product_name": "عسل",
            "quantity_label": "نصف كيلo",
            "missing_fields": ["city"],
            "order_status": "awaiting_address",
        },
    }


def _patch_brain_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, tenant_id, phone: (None, state),
    )


class TestCheckoutSlotBlocksShowroom:
    def test_bare_taif_is_city_token(self) -> None:
        assert is_bare_city_token_message("الطايف") is True
        assert has_explicit_showroom_pickup_intent("الطايف") is False

    def test_city_answer_defers_contact_routing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(monkeypatch, _checkout_brain_state())
        assert should_defer_contact_routing_for_checkout_slot(
            object(),
            tenant_id=10,
            customer_phone="966500000001",
            message="الطايف",
        )

    def test_branch_trigger_skips_city_during_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.test_branch_arrival_routing import (  # noqa: PLC0415
            _StructuredDB,
            _branch,
            _reception,
        )
        from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: PLC0415
            evaluate_branch_trigger_routing,
        )

        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
        _patch_brain_state(monkeypatch, _checkout_brain_state())
        db = _StructuredDB(
            branches=[_branch(city="الطائف", name="معرض الطائف")],
            contacts=[_reception(display_name="أمين", role="showroom")],
        )
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=10,
            message="الطايف",
            customer_phone="966500000001",
        )
        assert decision is None

    def test_staff_contact_policy_skips_city_during_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: PLC0415
            evaluate_staff_contact_policy,
        )

        _patch_brain_state(monkeypatch, _checkout_brain_state())
        decision = evaluate_staff_contact_policy(
            object(),
            tenant_id=10,
            message="الطايف",
            customer_phone="966500000001",
        )
        assert decision is None


class TestShowroomPickupConfirmFirst:
    @pytest.fixture(autouse=True)
    def _structured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")

    def test_pickup_intent_asks_before_ameen(self) -> None:
        from tests.test_branch_arrival_routing import (  # noqa: PLC0415
            _StructuredDB,
            _branch,
            _reception,
        )
        from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: PLC0415
            evaluate_branch_trigger_routing,
        )

        db = _StructuredDB(
            branches=[_branch(city="الطائف", name="معرض الطائف")],
            contacts=[_reception(display_name="أمين", role="showroom")],
        )
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=10,
            message="أبغى أستلم من المعرض بالطائف",
        )
        assert decision is not None
        assert decision.deliver_contact is False
        assert "المعرض" in decision.reply_text


class TestExplicitStaffRoutingStillWorks:
    def test_ameen_number_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from test_staff_target_classifier import (  # noqa: PLC0415
            _StubDB,
            _install_call_resolver,
            _merchant_sections,
        )
        from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: PLC0415
            evaluate_staff_contact_policy,
        )

        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=33,
            message="أرسل رقم أمين",
        )
        assert decision is not None
        assert decision.deliver_contact is True


class TestStaffRejectionResumesCommerce:
    def test_generic_ack_replaced_on_staff_rejection(self) -> None:
        state = {
            "order_prep": {
                "city": "الطائف",
                "quantity_label": "نصف كيلo",
                "product_name": "عسل",
            },
        }
        result = apply_commerce_reply_quality_guard(
            "تمام 🌷 وصلت رسالتك.",
            inbound_text="ما أبغى أمين أنا أبغى أشتري عسل",
            state=state,
        )
        assert result.replaced is True
        assert "أبشر" in result.reply
        assert "وصلت رسالتك" not in result.reply
        assert "عسل" in result.reply or "الطائف" in result.reply


class TestCatalogDiscoveryGlobalBrowse:
    def test_types_overview_dialect_defers_to_global_browse(self) -> None:
        msg = "وش الأنواع الي عندكم"
        assert global_availability_browse_requested(msg)
        assert is_generic_category_noun("الي") is False

        class _Facts:
            has_products = True

        class _Intent:
            slots: dict = {}

        class _Ctx:
            message = msg
            facts = _Facts()
            intent = _Intent()
            tenant_id = 10

        assert try_types_overview_decision(_Ctx()) is None  # type: ignore[arg-type]
