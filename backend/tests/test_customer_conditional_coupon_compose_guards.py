"""Adversarial evals for customer_conditional_coupon_answer persona guards."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards  # noqa: E402
from modules.ai.brain.persona.customer_conditional_coupon_answer import (  # noqa: E402
    build_customer_conditional_coupon_answer_facts_bundle,
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

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "حذاء رياضي أبيض"
_MESSAGE = "بعد كم طلب يصل الكوبون؟"


def _facts(**overrides) -> dict:
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
        "facts_snapshot_id": "snap-guard-001",
    }
    base.update(overrides)
    return base


def _bundle(facts: dict):
    return build_customer_conditional_coupon_answer_facts_bundle(
        inbound_text=_MESSAGE,
        tenant_id=8101,
        customer_phone="966500011122",
        customer_conditional_coupon_facts=facts,
        merchant_persona={"store_name": _MERCHANT, "product_context": _PRODUCT},
    )


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        ("كود الخصم ABC123 جاهز لك.", "coupon_code_disclosure"),
        ("تم إصدار الكوبون لك الآن.", "coupon_issued_claim"),
        ("تم تطبيق الكوبون على طلبك.", "coupon_applied_claim"),
        ("اطلب الآن وأرسل العنوان لإتمام الطلب.", "checkout_pressure"),
    ],
)
def test_adversarial_claims_rejected(text: str, expected_reason: str) -> None:
    guard = apply_persona_compose_guards(text, _bundle(_facts()))
    assert guard.passed is False
    assert guard.failed_reason == expected_reason


def test_false_eligibility_claim_blocked_when_allow_flag_false() -> None:
    facts = _facts(
        completed_orders_count=1,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        allow_min_orders_condition_claim=False,
        closed_reason_code="orders_shortfall",
    )
    guard = apply_persona_compose_guards(
        "أنت مؤهل الآن لأنك أكملت الطلبات المطلوبة.",
        _bundle(facts),
    )
    assert guard.passed is False
    assert guard.failed_reason == "final_min_orders_eligibility_claim"


def test_evidence_backed_satisfied_wording_allowed_when_flag_true() -> None:
    guard = apply_persona_compose_guards(
        "أكملت الطلبات المطلوبة حسب بيانات المتجر.",
        _bundle(_facts()),
    )
    assert guard.passed is True
