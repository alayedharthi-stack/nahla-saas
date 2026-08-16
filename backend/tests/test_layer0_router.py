"""
Tests for Phase A Layer 0 Router — deterministic pre-Brain path.

Proves zero-LLM replies for pure social/FAQ turns and Brain fallback for
commerce-mixed messages. No tenant-specific hardcoding.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_STORE_INFO,
    INTENT_ASK_WORKING_HOURS,
    INTENT_FAREWELL,
    INTENT_GREETING,
    INTENT_SOCIAL,
)
from modules.ai.routing.layer0_router import (  # noqa: E402
    evaluate_layer0_route,
    layer0_router_enabled,
)
from services import turn_trace as turn_trace_mod  # noqa: E402


def _mock_db(
    *,
    store_name: str = "متجر تجريبي",
    store_url: str = "https://shop.example.com",
    support_hours: str = "9 ص - 10 م",
    store_description: str = "",
) -> MagicMock:
    snapshot = SimpleNamespace(
        store_profile={"store_name": store_name, "store_url": store_url},
        policy_summary={"support_hours": support_hours},
    )
    settings = SimpleNamespace(
        store_settings={"store_name": store_name, "store_url": store_url},
        ai_settings={"assistant_name": "نحلة"},
    )

    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "StoreKnowledgeSnapshot":
            q.filter.return_value.first.return_value = snapshot
        elif name == "TenantSettings":
            q.filter.return_value.first.return_value = settings
        else:
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    return db


def _empty_state_store():
    return patch(
        "modules.ai.brain.state.store.DefaultStateStore.load",
        return_value=SimpleNamespace(greeted=False, stage="discovery", turn=1),
    )


@pytest.fixture(autouse=True)
def _enable_layer0(monkeypatch):
    monkeypatch.setenv("LAYER0_ROUTER_ENABLED", "true")


@pytest.fixture(autouse=True)
def _mock_brain_state_store():
    with patch(
        "modules.ai.brain.state.store.DefaultStateStore.load",
        return_value=SimpleNamespace(greeted=False, stage="discovery", turn=1),
    ):
        yield


class TestLayer0PureTurns:
    def test_greeting_defers_to_brain(self):
        """Pure greetings defer to Brain persona compose (Phase 3 migration)."""
        decision = evaluate_layer0_route(
            _mock_db(),
            tenant_id=101,
            customer_phone="966500000001",
            message="السلام عليكم",
        )
        assert decision is None

    def test_thanks_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(),
            tenant_id=102,
            customer_phone="966500000002",
            message="شكراً",
        )
        assert decision is None

    def test_religious_thanks_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(),
            tenant_id=103,
            customer_phone="966500000003",
            message="جزاك الله خير",
        )
        assert decision is None

    def test_store_link_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(store_url="https://shop.example.com"),
            tenant_id=104,
            customer_phone="966500000004",
            message="رابط المتجر",
        )
        assert decision is None

    def test_store_link_missing_url_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(store_url=""),
            tenant_id=105,
            customer_phone="966500000005",
            message="رابط المتجر الإلكتروني",
        )
        assert decision is None

    def test_working_hours_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(support_hours="9 ص - 10 م"),
            tenant_id=106,
            customer_phone="966500000006",
            message="متى دوامكم",
        )
        assert decision is None

    def test_working_hours_missing_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(support_hours=""),
            tenant_id=107,
            customer_phone="966500000007",
            message="أوقات العمل",
        )
        assert decision is None

    def test_goodbye_defers_to_brain(self):
        decision = evaluate_layer0_route(
            _mock_db(),
            tenant_id=108,
            customer_phone="966500000008",
            message="مع السلامة",
        )
        assert decision is None


class TestLayer0CommerceFallback:
    def test_mixed_greeting_goes_to_brain(self):
        assert evaluate_layer0_route(
            _mock_db(),
            tenant_id=201,
            customer_phone="966500000101",
            message="السلام عليكم أريد عسل سدر",
        ) is None

    def test_mixed_thanks_price_goes_to_brain(self):
        assert evaluate_layer0_route(
            _mock_db(),
            tenant_id=202,
            customer_phone="966500000102",
            message="شكراً كم سعر السدر؟",
        ) is None

    def test_mixed_thanks_order_goes_to_brain(self):
        assert evaluate_layer0_route(
            _mock_db(),
            tenant_id=203,
            customer_phone="966500000103",
            message="جزاك الله خير أريد الطلب الآن",
        ) is None

    def test_mixed_hours_product_goes_to_brain(self):
        assert evaluate_layer0_route(
            _mock_db(),
            tenant_id=204,
            customer_phone="966500000104",
            message="هل أنتم فاتحين وعندكم عسل سدر؟",
        ) is None


class TestLayer0ExistingPoliciesUntouched:
    def test_location_policy_still_available(self):
        from modules.ai.brain.commerce.location_link_policy import (  # noqa: PLC0415
            evaluate_location_link_policy,
            location_link_policy_enabled,
        )

        assert callable(evaluate_location_link_policy)
        assert location_link_policy_enabled()

    def test_staff_contact_policy_still_available(self):
        from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
            evaluate_staff_contact_policy,
            staff_contact_policy_enabled,
        )

        assert callable(evaluate_staff_contact_policy)
        assert staff_contact_policy_enabled()


class TestLayer0NoTenantHardcoding:
    def test_router_source_has_no_tenant_33(self):
        src = Path(_BACKEND) / "modules" / "ai" / "routing" / "layer0_router.py"
        text = src.read_text(encoding="utf-8")
        assert "tenant_id == 33" not in text
        assert "tenant 33" not in text.lower()
        assert "TENANT_33" not in text

    def test_turn_trace_has_layer0_source(self):
        assert turn_trace_mod.SOURCE_LAYER0 == "layer0"
        assert turn_trace_mod.SOURCE_LAYER0 in turn_trace_mod._ALL_SOURCES


class TestLayer0NoBrainOrLlm:
    def test_evaluate_never_calls_brain_or_llm(self):
        with patch("modules.ai.brain.pipeline.get_brain") as mock_brain:
            with patch(
                "modules.ai.orchestrator.adapter.generate_ai_reply",
            ) as mock_llm:
                decision = evaluate_layer0_route(
                    _mock_db(),
                    tenant_id=301,
                    customer_phone="966500000201",
                    message="شكراً",
                )
        assert decision is None
        mock_brain.assert_not_called()
        mock_llm.assert_not_called()

    def test_thanks_path_does_not_call_llm_compose(self):
        with patch(
            "modules.ai.brain.compose.responder.DefaultComposer._compose_social_persona_ack",
            new_callable=AsyncMock,
        ) as mock_ack:
            decision = evaluate_layer0_route(
                _mock_db(),
                tenant_id=302,
                customer_phone="966500000202",
                message="يعطيك العافية",
            )
        assert decision is None
        mock_ack.assert_not_called()


class TestWorkingHoursRules:
    def test_working_hours_intent_registered(self):
        from modules.ai.brain.intent import rules  # noqa: E402

        intent = rules.match("متى تفتحون")
        assert intent is not None
        assert intent.name == INTENT_ASK_WORKING_HOURS

    def test_farewell_intent_registered(self):
        from modules.ai.brain.intent import rules  # noqa: E402

        intent = rules.match("في أمان الله")
        assert intent is not None
        assert intent.name == INTENT_FAREWELL


def test_layer0_disabled_by_env(monkeypatch):
    monkeypatch.setenv("LAYER0_ROUTER_ENABLED", "false")
    assert not layer0_router_enabled()
    assert evaluate_layer0_route(
        _mock_db(),
        tenant_id=999,
        customer_phone="966500000999",
        message="السلام عليكم",
    ) is None
