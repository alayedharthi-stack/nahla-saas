"""Tests for trusted coupon/offer consumption gate (fail-closed)."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

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
    CouponOfferComposeProjectionError,
)
from modules.ai.brain.truth_surface.coupon_offer_consumption_gate import (  # noqa: E402
    maybe_trusted_coupon_offer_compose_facts,
    safe_coupon_offer_consumption_trace_metadata,
)


def _snapshot_with_eligible_coupon() -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        facts=[
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key="coupon:1",
                value={"coupon_id": 1, "eligible": True, "verified": True},
                source=TruthSource.COUPON_TABLE,
                path="coupon_table.id=1",
            )
        ],
        loaded_domains=[TrustedDomain.COUPONS.value],
        sources=["test"],
        shadow_observability={"eligible_coupon_count": 1},
    )
    snap.ensure_snapshot_id()
    return snap


def test_flag_off_returns_none() -> None:
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.project_trusted_coupon_offer_compose_facts",
    ) as project:
        out = maybe_trusted_coupon_offer_compose_facts(
            message="عندكم عروض؟",
            snapshot=_snapshot_with_eligible_coupon(),
        )
    assert out is None
    project.assert_not_called()


def test_flag_on_social_message_returns_none() -> None:
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ):
        out = maybe_trusted_coupon_offer_compose_facts(
            message="السلام عليكم",
            snapshot=_snapshot_with_eligible_coupon(),
        )
    assert out is None


def test_flag_on_offer_question_returns_facts() -> None:
    snap = _snapshot_with_eligible_coupon()
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ):
        out = maybe_trusted_coupon_offer_compose_facts(message="عندكم عروض؟", snapshot=snap)
    assert isinstance(out, dict)
    assert out.get("surface") == "trusted_coupon_offer_answer"
    assert out.get("question_kind") == "offer"


def test_flag_on_coupon_question_returns_facts() -> None:
    snap = _snapshot_with_eligible_coupon()
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ):
        out = maybe_trusted_coupon_offer_compose_facts(
            message="هل يوجد كوبون خصم؟",
            snapshot=snap,
        )
    assert isinstance(out, dict)
    assert out.get("question_kind") == "coupon"


def test_missing_snapshot_returns_none() -> None:
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ):
        out = maybe_trusted_coupon_offer_compose_facts(message="عندكم عروض؟", snapshot=None)
    assert out is None


def test_invalid_projection_returns_none_and_safe_error_metadata() -> None:
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.project_trusted_coupon_offer_compose_facts",
        side_effect=CouponOfferComposeProjectionError("invalid_surface"),
    ):
        out = maybe_trusted_coupon_offer_compose_facts(
            message="عندكم عروض؟",
            snapshot=_snapshot_with_eligible_coupon(),
        )
    assert out is None
    meta = safe_coupon_offer_consumption_trace_metadata(
        CouponOfferComposeProjectionError("invalid_surface"),
    )
    assert meta["status"] == "error"
    assert meta["stage"] == "coupon_offer_compose_projection"
    assert meta["error_class"] == "CouponOfferComposeProjectionError"
    assert "invalid_surface" not in str(meta)
