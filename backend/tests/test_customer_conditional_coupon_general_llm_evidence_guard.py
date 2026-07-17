"""Behavioral evals for conditional-coupon general-LLM final evidence guard."""
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
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards  # noqa: E402
from modules.ai.brain.persona.customer_conditional_coupon_answer import (  # noqa: E402
    build_customer_conditional_coupon_answer_facts_bundle,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.postprocess.customer_conditional_coupon_general_llm_evidence_guard import (  # noqa: E402
    apply_customer_conditional_coupon_general_llm_evidence_guard,
    should_apply_customer_conditional_coupon_general_llm_evidence_guard,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    IDENTITY_STATUS_RESOLVED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    build_sanitized_fact_record,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "حذاء رياضي أبيض"
_MESSAGE = "بعد كم طلب يصل الكوبون؟"
_DISCOUNT_FALLBACK = build_discount_coupon_support_reply()
_SAFE_UNCERTAINTY = (
    "قد يختلف الشرط حسب سياسة المتجر، "
    "والبيانات المتوفرة عندنا لا تؤكد العدد بدقة الآن."
)


def _facts(**overrides) -> dict:
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
        "facts_snapshot_id": "snap-general-guard-001",
    }
    base.update(overrides)
    return base


def _satisfied_facts() -> dict:
    return _facts(
        completed_orders_count=3,
        orders_shortfall=None,
        min_orders_condition_state=MIN_ORDERS_STATE_SATISFIED,
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SATISFIED,
        allow_min_orders_condition_claim=True,
        closed_reason_code=None,
    )


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        ("كود الخصم ABC123 جاهز لك.", "coupon_code_disclosure"),
        ("تم إصدار الكوبون لك الآن.", "coupon_issued_claim"),
        ("تم تطبيق الكوبون على طلبك.", "coupon_applied_claim"),
        ("كوبونك جاهز للاستخدام الآن.", "final_coupon_claim"),
        ("اطلب الآن وأرسل العنوان لإتمام الطلب.", "checkout_pressure"),
    ],
)
def test_unsafe_general_llm_claims_rejected(text: str, expected_reason: str) -> None:
    result = apply_customer_conditional_coupon_general_llm_evidence_guard(
        text,
        customer_conditional_coupon_facts=_facts(),
    )
    assert result.rejected is True
    assert result.reply == ""
    assert result.failed_reason == expected_reason


def test_false_eligibility_claim_blocked_on_general_llm_path() -> None:
    result = apply_customer_conditional_coupon_general_llm_evidence_guard(
        "أنت مؤهل الآن لأنك أكملت الطلبات المطلوبة.",
        customer_conditional_coupon_facts=_facts(),
    )
    assert result.rejected is True
    assert result.failed_reason == "final_min_orders_eligibility_claim"


def test_safe_uncertainty_general_llm_passes_unchanged() -> None:
    result = apply_customer_conditional_coupon_general_llm_evidence_guard(
        _SAFE_UNCERTAINTY,
        customer_conditional_coupon_facts=_facts(),
    )
    assert result.rejected is False
    assert result.reply == _SAFE_UNCERTAINTY


def test_evidence_backed_satisfied_wording_allowed_when_flag_true() -> None:
    result = apply_customer_conditional_coupon_general_llm_evidence_guard(
        "أكملت الطلبات المطلوبة حسب بيانات المتجر.",
        customer_conditional_coupon_facts=_satisfied_facts(),
    )
    assert result.rejected is False


def test_no_conditional_facts_means_guard_not_applicable() -> None:
    assert (
        should_apply_customer_conditional_coupon_general_llm_evidence_guard(
            customer_conditional_coupon_facts=None,
        )
        is False
    )


@patch(
    "modules.ai.brain.postprocess.customer_conditional_coupon_general_llm_evidence_guard."
    "is_customer_conditional_coupon_layer0_enabled",
    return_value=False,
)
def test_both_flags_off_zero_io(_mock_layer0: object) -> None:
    assert (
        should_apply_customer_conditional_coupon_general_llm_evidence_guard(
            customer_conditional_coupon_facts=_facts(),
        )
        is False
    )


def test_persona_guard_failure_then_unsafe_general_llm_rejected() -> None:
    bundle = build_customer_conditional_coupon_answer_facts_bundle(
        inbound_text=_MESSAGE,
        tenant_id=8102,
        customer_phone="966500011133",
        customer_conditional_coupon_facts=_facts(),
        merchant_persona={"store_name": _MERCHANT, "product_context": _PRODUCT},
    )
    persona_guard = apply_persona_compose_guards(
        "كود الخصم ABC123 جاهز لك.",
        bundle,
    )
    assert persona_guard.passed is False

    general_guard = apply_customer_conditional_coupon_general_llm_evidence_guard(
        "كود الخصم ABC123 جاهز لك.",
        customer_conditional_coupon_facts=_facts(),
    )
    assert general_guard.rejected is True
    assert general_guard.failed_reason == "coupon_code_disclosure"


def test_crqg_still_never_inserts_discount_fallback_with_conditional_facts() -> None:
    crqg = apply_commerce_reply_quality_guard(
        reply="",
        inbound_text=_MESSAGE,
        intent_name=INTENT_GENERAL,
        customer_conditional_coupon_facts=_facts(),
    )
    assert crqg.reply != _DISCOUNT_FALLBACK
    assert _DISCOUNT_FALLBACK not in (crqg.reply or "")
    assert crqg.fallback_kind == "conditional_coupon_compose_collision_suppressed"


async def _run_compose_failure_general_llm_pipeline():
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

    ctx = BrainContext(
        tenant_id=8103,
        customer_phone="966500011144",
        message=_MESSAGE,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9),
        state=MerchantConversationState(stage="browsing", customer_goal="general_help"),
        facts=SimpleNamespace(store_name=_MERCHANT),
        history=[],
        profile={},
    )
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"customer_conditional_coupon_facts": _facts()},
    )
    result = ActionResult(success=True, data={})
    composer = DefaultComposer()
    unsafe_llm = "كود الخصم ABC123 جاهز لك."

    with patch(
        "modules.ai.brain.persona.customer_conditional_coupon_answer."
        "try_compose_customer_conditional_coupon_answer",
        new_callable=AsyncMock,
        return_value=(None, None, None),
    ), patch.object(
        composer,
        "_llm_compose",
        new_callable=AsyncMock,
        return_value=unsafe_llm,
    ), patch(
        "modules.ai.brain.postprocess.customer_conditional_coupon_general_llm_evidence_guard."
        "is_customer_conditional_coupon_layer0_enabled",
        return_value=True,
    ):
        text = await composer.compose(
            Decision(action="llm_reply", args={}),
            result,
            ctx,
        )
    guarded = apply_customer_conditional_coupon_general_llm_evidence_guard(
        text or "",
        customer_conditional_coupon_facts=_facts(),
    )
    return text, guarded, result


def test_compose_failure_general_llm_unsafe_text_rejected_at_guard() -> None:
    text, guarded, result = asyncio.run(_run_compose_failure_general_llm_pipeline())
    assert text == "كود الخصم ABC123 جاهز لك."
    assert result.data.get("customer_conditional_coupon_general_llm_fallthrough") is True
    assert guarded.rejected is True
    assert guarded.reply == ""
