"""Tests for customer conditional-coupon compose projection (pure, snapshot-only)."""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_projection import (  # noqa: E402
    CustomerConditionalCouponComposeProjectionError,
    project_customer_conditional_coupon_compose_facts,
    validate_customer_conditional_coupon_compose_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    FACT_DOMAIN,
    FACT_SCHEMA_VERSION,
    IDENTITY_STATUS_AMBIGUOUS,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_ORDERS_SHORTFALL,
    REASON_SUBJECT_AMBIGUOUS,
    build_sanitized_fact_record,
)

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "حذاء رياضي أبيض"


def _layer0_record(**overrides) -> dict:
    base = build_sanitized_fact_record(
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
    base.update(overrides)
    return base


def _cc_fact(record: dict) -> TrustedFact:
    return TrustedFact(
        domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
        key="customer_conditional_coupon:eligibility",
        value=record,
        source=TruthSource.PROMOTION_TABLE,
        path="customer_conditional_coupon_loader.layer0",
    )


def _snapshot(*facts: TrustedFact, tenant_id: int = 9001) -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        facts=list(facts),
        loaded_domains=[TrustedDomain.CUSTOMER_CONDITIONAL_COUPON.value],
        sources=["test"],
        shadow_observability={
            "merchant_label": _MERCHANT,
            "product_context": _PRODUCT,
        },
    )
    snap.ensure_snapshot_id()
    return snap


def test_satisfied_allow_true_generic_merchant() -> None:
    record = _layer0_record()
    snap = _snapshot(_cc_fact(record))
    out = project_customer_conditional_coupon_compose_facts(snapshot=snap)
    assert out["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert out["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SATISFIED
    assert out["allow_min_orders_condition_claim"] is True
    assert out["completed_orders_count"] == 3
    validate_customer_conditional_coupon_compose_facts(out)


def test_shortfall_allow_false() -> None:
    record = _layer0_record(
        completed_orders_count=1,
        min_orders_for_eligibility=3,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code=REASON_ORDERS_SHORTFALL,
        allow_min_orders_condition_claim=False,
    )
    snap = _snapshot(_cc_fact(record))
    out = project_customer_conditional_coupon_compose_facts(snapshot=snap)
    assert out["orders_shortfall"] == 2
    assert out["allow_min_orders_condition_claim"] is False


def test_requires_context_unresolved_identity() -> None:
    record = _layer0_record(
        identity_status=IDENTITY_STATUS_UNRESOLVED,
        completed_orders_count=None,
        min_orders_for_eligibility=None,
        orders_shortfall=None,
        min_orders_condition_state="not_evaluated",
        conditional_coupon_evaluation_state=EVALUATION_REQUIRES_CONTEXT,
        closed_reason_code=REASON_CUSTOMER_UNVERIFIED,
        allow_min_orders_condition_claim=False,
        order_history_completeness="unverified",
        order_history_completeness_source=None,
    )
    snap = _snapshot(_cc_fact(record))
    out = project_customer_conditional_coupon_compose_facts(snapshot=snap)
    assert out["identity_status"] == IDENTITY_STATUS_UNRESOLVED
    assert out["conditional_coupon_evaluation_state"] == EVALUATION_REQUIRES_CONTEXT


def test_ambiguous_identity_fails_closed() -> None:
    record = _layer0_record(
        identity_status=IDENTITY_STATUS_AMBIGUOUS,
        conditional_coupon_evaluation_state=EVALUATION_REQUIRES_CONTEXT,
        closed_reason_code=REASON_SUBJECT_AMBIGUOUS,
        allow_min_orders_condition_claim=False,
        completed_orders_count=None,
        min_orders_for_eligibility=None,
    )
    snap = _snapshot(_cc_fact(record))
    with pytest.raises(CustomerConditionalCouponComposeProjectionError, match="ambiguous"):
        project_customer_conditional_coupon_compose_facts(snapshot=snap)


def test_sanitizer_unknown_key_rejection() -> None:
    record = _layer0_record(customer_id=99)
    snap = _snapshot(_cc_fact(record))
    with pytest.raises(CustomerConditionalCouponComposeProjectionError, match="sanitizer"):
        project_customer_conditional_coupon_compose_facts(snapshot=snap)


def test_tenant_mismatch_fails_closed() -> None:
    record = _layer0_record()
    snap = _snapshot(_cc_fact(record), tenant_id=9001)
    with pytest.raises(CustomerConditionalCouponComposeProjectionError, match="tenant"):
        project_customer_conditional_coupon_compose_facts(
            snapshot=snap,
            expected_tenant_id=9002,
        )


def test_invalid_schema_version_rejected() -> None:
    record = _layer0_record(fact_schema_version="v7")
    snap = _snapshot(_cc_fact(record))
    with pytest.raises(CustomerConditionalCouponComposeProjectionError):
        project_customer_conditional_coupon_compose_facts(snapshot=snap)


def test_privacy_no_forbidden_keys_in_output_json() -> None:
    record = _layer0_record()
    snap = _snapshot(_cc_fact(record))
    out = project_customer_conditional_coupon_compose_facts(snapshot=snap)
    blob = json.dumps(out, ensure_ascii=False)
    for forbidden in ('"coupon_id"', '"phone"', '"customer_id"', FACT_DOMAIN):
        if forbidden == FACT_DOMAIN:
            continue
        assert forbidden not in blob
    assert out["surface"] == "customer_conditional_coupon_answer"
