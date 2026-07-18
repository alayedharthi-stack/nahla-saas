"""Wiring tests for customer conditional-coupon compose consumption."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_CONDITION_SATISFIED,
    IDENTITY_STATUS_RESOLVED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    build_sanitized_fact_record,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)

_MERCHANT = "متجر تجريبي عام"
_MIN_ORDERS_MESSAGE = "بعد كم طلب يصل الكوبون؟"
_COMPOSE_PHONE = "966500000001"


def _eligible_compose_ai_settings(*, tenant_id: int = 9001) -> dict:
    return {
        "store_ai_mode": "test",
        "customer_conditional_coupon_compose_allowlist_tenants": [int(tenant_id)],
        "ai_test_allowed_numbers": [_COMPOSE_PHONE],
    }


def _compose_facts(**overrides):
    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=3,
        min_orders_for_eligibility=3,
        orders_shortfall=None,
        min_orders_condition_state=MIN_ORDERS_STATE_SATISFIED,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SATISFIED,
        closed_reason_code=None,
        allow_min_orders_condition_claim=True,
    )
    base = {
        "schema_version": "1",
        "surface": "customer_conditional_coupon_answer",
        "identity_status": record["identity_status"],
        "min_orders_condition_state": record["min_orders_condition_state"],
        "conditional_coupon_evaluation_state": record["conditional_coupon_evaluation_state"],
        "order_history_completeness": record["order_history_completeness"],
        "completed_orders_count": record["completed_orders_count"],
        "min_orders_for_eligibility": record["min_orders_for_eligibility"],
        "orders_shortfall": record["orders_shortfall"],
        "allow_min_orders_condition_claim": record["allow_min_orders_condition_claim"],
        "closed_reason_code": record["closed_reason_code"],
        "facts_snapshot_id": "snap-cc-test-001",
    }
    base.update(overrides)
    return base


def _layer0_fact(**overrides) -> TrustedFact:
    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=1,
        min_orders_for_eligibility=3,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code="orders_shortfall",
        allow_min_orders_condition_claim=False,
    )
    record.update(overrides)
    return TrustedFact(
        domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
        key="customer_conditional_coupon:eligibility",
        value=record,
        source=TruthSource.PROMOTION_TABLE,
        path="customer_conditional_coupon_loader.layer0",
    )


def _minimal_ctx(message: str) -> BrainContext:
    facts = SimpleNamespace(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        shipping_policy="",
        shipping_methods=[],
        shipping_notes="",
        support_hours="",
        store_contact_phone="",
        store_contact_email="",
        has_coupons=True,
        coupon_eligibility=None,
    )
    return BrainContext(
        tenant_id=9001,
        customer_phone=_COMPOSE_PHONE,
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9),
        state=_state(),
        facts=facts,
        history=[],
        profile={},
    )


def _state() -> MerchantConversationState:
    return MerchantConversationState(
        stage="browsing",
        customer_goal="general_help",
        order_prep=SimpleNamespace(to_dict=lambda: {}),
    )


def test_pipeline_injects_conditional_facts_when_compose_flag_on() -> None:
    ctx = _minimal_ctx(_MIN_ORDERS_MESSAGE)
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        facts=[_layer0_fact()],
        shadow_observability={"merchant_label": _MERCHANT},
    )
    snap.ensure_snapshot_id()
    projected = {
        "schema_version": "1",
        "surface": "customer_conditional_coupon_answer",
        "min_orders_condition_state": MIN_ORDERS_STATE_SHORTFALL,
        "conditional_coupon_evaluation_state": EVALUATION_CONDITION_SHORTFALL,
        "allow_min_orders_condition_claim": False,
        "facts_snapshot_id": snap.snapshot_id,
    }

    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_consumption_gate."
        "maybe_customer_conditional_coupon_compose_facts",
        return_value=projected,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.current_trusted_context",
        return_value=snap,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate."
        "is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ):
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=_state(),
            current_state=_state(),
            suggestion=SuggestionSnapshot(),
            decision=Decision(action="llm_reply", args={}),
            merchant_context={"ai_settings": _eligible_compose_ai_settings()},
        )
    assert "customer_conditional_coupon_facts" in reply_state.known_facts
    assert "trusted_coupon_offer_facts" not in reply_state.known_facts
    facts = reply_state.known_facts["customer_conditional_coupon_facts"]
    assert facts["min_orders_condition_state"] == MIN_ORDERS_STATE_SHORTFALL
    assert "coupon_id" not in facts


def test_pipeline_omits_key_when_compose_flag_off() -> None:
    ctx = _minimal_ctx(_MIN_ORDERS_MESSAGE)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate."
        "evaluate_customer_conditional_coupon_compose_canary",
    ) as canary_mock:
        from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate import (  # noqa: E402
            CustomerConditionalCouponComposeCanaryDecision,
            REASON_COMPOSE_MASTER_DISABLED,
        )

        canary_mock.return_value = CustomerConditionalCouponComposeCanaryDecision(
            allowed=False,
            reason=REASON_COMPOSE_MASTER_DISABLED,
        )
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=_state(),
            current_state=_state(),
            suggestion=SuggestionSnapshot(),
            decision=Decision(action="llm_reply", args={}),
        )
    assert "customer_conditional_coupon_facts" not in reply_state.known_facts


async def _run_conditional_compose_turn():
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
    from modules.ai.brain.types import BrainReplyState  # noqa: PLC0415

    compose_facts = _compose_facts()
    ctx = _minimal_ctx(_MIN_ORDERS_MESSAGE)
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"customer_conditional_coupon_facts": compose_facts},
    )
    result = ActionResult(success=True, data={})
    decision = Decision(action="llm_reply", args={})
    composer = DefaultComposer()

    with patch(
        "modules.ai.brain.persona.customer_conditional_coupon_answer."
        "evaluate_customer_conditional_coupon_compose_canary",
    ) as canary_mock, patch(
        "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
        new_callable=AsyncMock,
    ) as compose_mock:
        from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate import (  # noqa: E402
            CustomerConditionalCouponComposeCanaryDecision,
            REASON_ALLOWED,
        )

        canary_mock.return_value = CustomerConditionalCouponComposeCanaryDecision(
            allowed=True,
            reason=REASON_ALLOWED,
            compose_master_enabled=True,
        )
        from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402

        compose_mock.return_value = PersonaComposeResult(
            text="بعد 3 طلبات مكتملة يتفعل عرض الكوبون حسب بيانات المتجر.",
            source="persona_llm",
            surface="customer_conditional_coupon_answer",
            facts_hash="abc",
            guard_passed=True,
            language="ar",
        )
        text = await composer.compose(decision, result, ctx)
    return text, result, compose_mock


def test_responder_routes_to_persona_compose_once() -> None:
    text, result, compose_mock = asyncio.run(_run_conditional_compose_turn())
    assert text
    compose_mock.assert_called_once()
    assert result.data.get("customer_conditional_coupon_compose_active") is True
    assert result.data.get("chosen_path") == "customer_conditional_coupon_compose"
    assert result.data.get("compose_source") == "persona_llm"
    assert result.data.get("response_mode") == "customer_conditional_coupon_answer"
    assert result.data.get("llm_candidate_present") is True
    assert result.data.get("facts_snapshot_id") == "snap-cc-test-001"


def test_prompt_static_prefix_dynamic_suffix_for_caching() -> None:
    from modules.ai.brain.persona.customer_conditional_coupon_answer import (  # noqa: PLC0415
        build_customer_conditional_coupon_answer_facts_bundle,
    )
    from modules.ai.brain.persona.prompts import build_system_prompt, build_user_prompt  # noqa: PLC0415

    bundle = build_customer_conditional_coupon_answer_facts_bundle(
        inbound_text=_MIN_ORDERS_MESSAGE,
        tenant_id=9001,
        customer_conditional_coupon_facts=_compose_facts(),
    )
    system = build_system_prompt(bundle)
    user = build_user_prompt(bundle)
    assert "customer_conditional_coupon_answer" in system
    assert "allow_min_orders_condition_claim" in system
    assert "min_orders_condition_state:" in user
    assert "min_orders_condition_state" not in system.split("allow_min_orders_condition_claim")[0]


async def _run_compose_failure_fallthrough():
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
    from modules.ai.brain.types import BrainReplyState  # noqa: PLC0415

    ctx = _minimal_ctx(_MIN_ORDERS_MESSAGE)
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"customer_conditional_coupon_facts": _compose_facts()},
    )
    result = ActionResult(success=True, data={})
    decision = Decision(action="llm_reply", args={})
    composer = DefaultComposer()

    with patch(
        "modules.ai.brain.persona.customer_conditional_coupon_answer."
        "try_compose_customer_conditional_coupon_answer",
        new_callable=AsyncMock,
        return_value=(None, None, None),
    ), patch.object(
        composer,
        "_llm_compose",
        new_callable=AsyncMock,
        return_value="generic llm path",
    ) as llm_mock:
        text = await composer.compose(decision, result, ctx)
    return text, result, llm_mock


def test_compose_failure_falls_through_without_canned_prose() -> None:
    text, result, llm_mock = asyncio.run(_run_compose_failure_fallthrough())
    assert text == "generic llm path"
    llm_mock.assert_called_once()
    assert result.data.get("customer_conditional_coupon_compose_active") is not True
