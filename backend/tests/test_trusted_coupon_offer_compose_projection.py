"""Tests for trusted coupon/offer compose projection (pure, snapshot-only)."""
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
from modules.ai.brain.truth_surface.coupon_offer_compose_projection import (  # noqa: E402
    AVAILABILITY_ACTIVE_OR_ELIGIBLE,
    AVAILABILITY_NONE_VERIFIED,
    AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE,
    AVAILABILITY_REQUIRES_CONTEXT,
    CouponOfferComposeProjectionError,
    project_trusted_coupon_offer_compose_facts,
    validate_trusted_coupon_offer_compose_facts,
)

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "حذاء رياضي أبيض"


def _coupon_fact(**value_overrides) -> TrustedFact:
    value = {
        "domain": TrustedDomain.COUPONS.value,
        "coupon_id": 11,
        "tenant_id": 9001,
        "eligible": True,
        "verified": True,
        "reason_when_unavailable": None,
    }
    value.update(value_overrides)
    return TrustedFact(
        domain=TrustedDomain.COUPONS,
        key=f"coupon:{value['coupon_id']}",
        value=value,
        source=TruthSource.COUPON_TABLE,
        path=f"coupon_table.id={value['coupon_id']}",
    )


def _promo_fact(**value_overrides) -> TrustedFact:
    value = {
        "domain": TrustedDomain.PROMOTIONS.value,
        "promotion_id": 21,
        "tenant_id": 9001,
        "eligible": True,
        "verified": True,
        "reason_when_unavailable": None,
    }
    value.update(value_overrides)
    return TrustedFact(
        domain=TrustedDomain.PROMOTIONS,
        key=f"promotion:{value['promotion_id']}",
        value=value,
        source=TruthSource.PROMOTION_TABLE,
        path=f"promotion_table.id={value['promotion_id']}",
    )


def _snapshot(facts, **overrides) -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        customer_phone="966500000001",
        facts=list(facts),
        loaded_domains=[TrustedDomain.COUPONS.value, TrustedDomain.PROMOTIONS.value],
        sources=["test"],
        shadow_observability={
            "eligible_coupon_count": sum(
                1 for f in facts if f.domain == TrustedDomain.COUPONS and f.value.get("eligible") is True
            ),
            "eligible_promotion_count": sum(
                1
                for f in facts
                if f.domain == TrustedDomain.PROMOTIONS and f.value.get("eligible") is True
            ),
            "merchant_label": _MERCHANT,
            "product_context": _PRODUCT,
        },
    )
    for key, val in overrides.items():
        setattr(snap, key, val)
    snap.ensure_snapshot_id()
    return snap


def test_offer_question_with_eligible_promotions() -> None:
    snap = _snapshot([_promo_fact(eligible=True)])
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="عندكم عروض؟")
    assert out["question_kind"] == "offer"
    assert out["promotion_availability"] == AVAILABILITY_ACTIVE_OR_ELIGIBLE
    assert out["verified_eligible_promotion_count"] == 1
    assert out["allow_code_mention"] is False


def test_coupon_question_with_eligible_coupons() -> None:
    snap = _snapshot([_coupon_fact(eligible=True)])
    out = project_trusted_coupon_offer_compose_facts(
        snapshot=snap,
        message="هل يوجد كوبون خصم؟",
    )
    assert out["question_kind"] == "coupon"
    assert out["coupon_availability"] == AVAILABILITY_ACTIVE_OR_ELIGIBLE
    assert out["verified_eligible_coupon_count"] == 1


def test_records_present_but_none_eligible() -> None:
    snap = _snapshot(
        [
            _coupon_fact(eligible=False, reason_when_unavailable="expired"),
            _promo_fact(eligible=False, reason_when_unavailable="outside_active_window"),
        ]
    )
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="عندكم عروض؟")
    assert out["coupon_availability"] == AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE
    assert out["promotion_availability"] == AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE
    assert "expired" in out["unavailability_reason_codes"]


def test_no_data_none_verified() -> None:
    snap = _snapshot(
        [
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key="coupon:unavailable",
                value={
                    "reason_when_unavailable": "no_coupon_data",
                    "eligible": None,
                },
                source=TruthSource.COUPON_TABLE,
                path="coupon_table.empty",
            ),
            TrustedFact(
                domain=TrustedDomain.PROMOTIONS,
                key="promotion:unavailable",
                value={
                    "reason_when_unavailable": "no_promotion_data",
                    "eligible": None,
                },
                source=TruthSource.PROMOTION_TABLE,
                path="promotion_table.empty",
            ),
        ]
    )
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="عندكم عروض؟")
    assert out["coupon_availability"] == AVAILABILITY_NONE_VERIFIED
    assert out["promotion_availability"] == AVAILABILITY_NONE_VERIFIED


def test_eligible_none_requires_context() -> None:
    snap = _snapshot(
        [
            _coupon_fact(
                eligible=None,
                verified=False,
                reason_when_unavailable="minimum_basket_unverified",
            )
        ]
    )
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="هل يوجد كوبون خصم؟")
    assert out["coupon_availability"] == AVAILABILITY_REQUIRES_CONTEXT
    assert out["allow_final_eligibility_claim"] is False
    assert "minimum_basket_unverified" in out["unavailability_reason_codes"]


def test_privacy_no_forbidden_keys_in_output_json() -> None:
    snap = _snapshot(
        [
            _coupon_fact(
                eligible=True,
                code="SECRET10",
                code_masked="***10#abc",
                customer_phone="966500000099",
            )
        ]
    )
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="كوبون خصم")
    blob = json.dumps(out, ensure_ascii=False)
    for forbidden in (
        "SECRET10",
        "code_masked",
        "customer_phone",
        "966500000099",
        '"code"',
        "applicable_products",
    ):
        assert forbidden not in blob


def test_unknown_field_rejection() -> None:
    snap = _snapshot([_coupon_fact(eligible=True)])
    out = project_trusted_coupon_offer_compose_facts(snapshot=snap, message="عروض")
    out["unexpected_field"] = "leak"
    with pytest.raises(CouponOfferComposeProjectionError, match="unknown_fields"):
        validate_trusted_coupon_offer_compose_facts(out)
