"""Regression: conditional-coupon compose must not collide with discount fallback prose."""
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

from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    build_discount_coupon_support_reply,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SHORTFALL,
    IDENTITY_STATUS_RESOLVED,
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
    BrainReplyState,
)

_MERCHANT = "متجر تجريبي عام"
_MESSAGE = "بعد كم طلب يصل الكوبون؟"
_DISCOUNT_FALLBACK = build_discount_coupon_support_reply()


def _conditional_facts() -> dict:
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
    return {
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
        "facts_snapshot_id": "snap-collision-001",
    }


def test_crqg_suppresses_discount_fallback_when_conditional_facts_present() -> None:
    result = apply_commerce_reply_quality_guard(
        reply="",
        inbound_text=_MESSAGE,
        intent_name=INTENT_GENERAL,
        customer_conditional_coupon_facts=_conditional_facts(),
    )
    assert result.reply != _DISCOUNT_FALLBACK
    assert _DISCOUNT_FALLBACK not in (result.reply or "")
    assert result.fallback_kind == "conditional_coupon_compose_collision_suppressed"


async def _run_compose_failure_pipeline_collision():
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

    ctx = BrainContext(
        tenant_id=8101,
        customer_phone="966500011122",
        message=_MESSAGE,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9),
        state=MerchantConversationState(stage="browsing", customer_goal="general_help"),
        facts=SimpleNamespace(store_name=_MERCHANT),
        history=[],
        profile={},
    )
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"customer_conditional_coupon_facts": _conditional_facts()},
    )
    result = ActionResult(success=True, data={})
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
        return_value="",
    ):
        text = await composer.compose(
            Decision(action="llm_reply", args={}),
            result,
            ctx,
        )
    guarded = apply_commerce_reply_quality_guard(
        reply=text or "",
        inbound_text=_MESSAGE,
        intent_name=INTENT_GENERAL,
        customer_conditional_coupon_facts=_conditional_facts(),
    )
    return text, guarded


def test_compose_failure_empty_reply_never_becomes_discount_fallback() -> None:
    text, guarded = asyncio.run(_run_compose_failure_pipeline_collision())
    assert text == ""
    assert guarded.reply != _DISCOUNT_FALLBACK
    assert _DISCOUNT_FALLBACK not in (guarded.reply or "")
