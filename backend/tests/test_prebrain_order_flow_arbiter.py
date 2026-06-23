"""Pre-brain order flow ownership arbiter — slot answer protection."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: E402
    MSG_GENERAL_CONTACT_IN_CHANNEL,
)
from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: E402
    has_strong_prebrain_contact_intent,
    should_yield_prebrain_to_order_flow,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)


def _patch_brain_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, tenant_id, phone: (None, state),
    )


def _active_order_state(
    *,
    missing_fields: list[str],
    stage: str = "checkout",
    customer_phone_in_prep: str = "",
) -> dict[str, Any]:
    return {
        "stage": stage,
        "order_prep": {
            "product_name": "عسل",
            "product_id": "123",
            "city": "الطائف",
            "short_address_code": "TAPC3299",
            "missing_fields": missing_fields,
            "order_status": "awaiting_address",
            "customer_phone": customer_phone_in_prep,
        },
    }


class TestActiveOrderNameSlot:
    def test_name_like_answer_yields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name", "customer_last_name"]),
        )
        assert should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message="اسمي خالد الحارثي",
        )

    def test_staff_contact_policy_defers_for_name_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name", "customer_last_name"]),
        )
        decision = evaluate_staff_contact_policy(
            object(),
            tenant_id=10,
            message="اسمي خالد الحارثي",
            customer_phone="966549741354",
        )
        assert decision is None


class TestActiveOrderPhoneReference:
    def test_phone_reference_with_known_whatsapp_yields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name", "customer_last_name"]),
        )
        msg = "اسمي خالد الحارثي\nالجوال مسجل عندكم"
        assert should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message=msg,
        )

    def test_no_general_channel_on_composite_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name", "customer_last_name"]),
        )
        msg = "اسمي خالد الحارثي\nالجوال مسجل عندكم"
        decision = evaluate_staff_contact_policy(
            object(),
            tenant_id=10,
            message=msg,
            customer_phone="966549741354",
        )
        assert decision is None


class TestExplicitStaffStillAllowed:
    def test_strong_staff_request_does_not_yield(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name"]),
        )
        msg = "حولني لموظف"
        assert has_strong_prebrain_contact_intent(msg)
        assert not should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message=msg,
        )

    @patch("modules.ai.brain.commerce.staff_contact_evidence.load_staff_contact_registry")
    @patch("modules.ai.brain.commerce.staff_contact_policy._load_role_graph")
    def test_staff_policy_still_fires_on_explicit_ask(
        self,
        mock_role_graph,
        mock_registry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from test_staff_target_classifier import (  # noqa: PLC0415
            _StubDB,
            _install_call_resolver,
            _merchant_sections,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            StaffContactRegistry,
        )

        _install_call_resolver(monkeypatch)
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name"]),
        )
        mock_registry.return_value = StaffContactRegistry(records=(), store_contact_phone="")
        mock_role_graph.return_value = None

        decision = evaluate_staff_contact_policy(
            _StubDB(_merchant_sections()),
            tenant_id=33,
            message="أبي أكلم موظف",
            customer_phone="966549741354",
        )
        assert decision is not None


class TestWeakContactWordsInsufficient:
    def test_weak_contact_only_during_checkout_yields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(
                missing_fields=["customer_first_name", "customer_last_name"],
            ),
        )
        assert should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message="الجوال مسجل عندكم",
        )

    def test_weak_contact_outside_order_does_not_auto_yield(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(monkeypatch, {"stage": "browsing", "order_prep": {}})
        assert not should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message="الجوال مسجل عندكم",
        )


class TestRealisticCheckoutSequence:
    def test_full_sequence_identity_turn_no_stub_reply(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name", "customer_last_name"]),
        )
        msg = "اسمي خالد الحارثي\nالجوال مسجل عندكم"
        decision = evaluate_staff_contact_policy(
            object(),
            tenant_id=10,
            message=msg,
            customer_phone="966549741354",
        )
        assert decision is None
        assert MSG_GENERAL_CONTACT_IN_CHANNEL not in (decision.reply_text if decision else "")

    def test_city_still_deferred(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "checkout",
                "order_prep": {
                    "product_name": "عسل",
                    "missing_fields": ["city"],
                    "order_status": "awaiting_address",
                },
            },
        )
        assert should_yield_prebrain_to_order_flow(
            object(),
            tenant_id=10,
            customer_phone="966549741354",
            message="الطايف",
        )


class TestContactFamilyPoliciesUseArbiter:
    def test_branch_trigger_defers_during_checkout(
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
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name"]),
        )
        db = _StructuredDB(
            branches=[_branch(city="الطائف", name="معرض الطائف")],
            contacts=[_reception(display_name="أمين", role="showroom")],
        )
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=10,
            message="اسمي خالد",
            customer_phone="966549741354",
        )
        assert decision is None

    def test_staff_recovery_defers_during_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.commerce.staff_contact_recovery import (  # noqa: PLC0415
            evaluate_staff_contact_recovery,
        )

        monkeypatch.setenv("STAFF_CONTACT_RECOVERY_ENABLED", "1")
        _patch_brain_state(
            monkeypatch,
            _active_order_state(missing_fields=["customer_first_name"]),
        )
        result = evaluate_staff_contact_recovery(
            object(),
            tenant_id=10,
            phone="966549741354",
            message="اسمي خالد الحارثي",
        )
        assert result is None
