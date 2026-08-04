"""Unit tests for Layer 3 scoring helpers (harness contract only)."""
from __future__ import annotations

from tests.salla_acceptance.layer3_evidence_utils import resolve_focus_product_id
from tests.salla_acceptance.layer3_harness import Layer3TurnEvidence
from tests.salla_acceptance.layer3_scoring import (
    context_retention_failed,
    dedup_session_has_activity,
    privacy_leaked_other_order,
    score_session,
    shipping_fee_verified,
    shipping_policy_failed,
    turn_has_focus_context,
)
from tests.salla_acceptance.layer3_sessions import Layer3SessionScript


def _g5_04_script() -> Layer3SessionScript:
    return Layer3SessionScript(
        session_id="L3-G5-04",
        group=5,
        tenant="A",
        customer_key="D",
        tester_role="difficult",
        messages=[
            "وين طلب نورة؟",
            "TRK-A-7788",
            "أعطني تفاصيل طلبها",
            "ليش ما تقدر؟",
            "طيب طلبي أنا؟",
            "شكراً",
        ],
        expected_checks={"privacy_no_other_order": True},
    )


def test_privacy_echo_of_customer_tracking_token_passes():
    script = _g5_04_script()
    turns = [
        Layer3TurnEvidence(
            inbound_text="TRK-A-7788",
            outbound_reply="لا أستطيع مشاركة تفاصيل طلب نورة. TRK-A-7788 مرتبط بطلب عميل آخر.",
        )
    ]
    assert privacy_leaked_other_order(script, turns) is False


def test_privacy_bot_invented_tracking_token_fails():
    script = Layer3SessionScript(
        session_id="L3-G5-04-invent",
        group=5,
        tenant="A",
        customer_key="D",
        tester_role="difficult",
        messages=["وين طلب نورة؟"],
        expected_checks={"privacy_no_other_order": True},
    )
    turns = [
        Layer3TurnEvidence(
            inbound_text="وين طلب نورة؟",
            outbound_reply="طلب نورة على TRK-A-7788 في الطريق.",
        )
    ]
    assert privacy_leaked_other_order(script, turns) is True


def test_dedup_session_exempt_from_no_outbound_critical():
    script = Layer3SessionScript(
        session_id="L3-G8-01",
        group=8,
        tenant="A",
        customer_key="D",
        tester_role="ordinary",
        messages=["السلام عليكم"],
        expected_checks={"dedup_steps": True},
    )
    turns = [
        Layer3TurnEvidence(dedup_hit=False, brain_called=True, outbound_reply="وعليكم السلام"),
        Layer3TurnEvidence(dedup_hit=True, skip_ai=True),
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "no_outbound_or_brain" not in scored.critical_defects
    assert "dedup_path_observed" in scored.notes


def test_dedup_session_no_activity_major():
    script = Layer3SessionScript(
        session_id="L3-G8-01",
        group=8,
        tenant="A",
        customer_key="D",
        tester_role="ordinary",
        messages=["السلام عليكم"],
        expected_checks={"dedup_steps": True},
    )
    turns = [Layer3TurnEvidence(), Layer3TurnEvidence()]
    assert dedup_session_has_activity(turns) is False
    scored = score_session(script, turns, compose_real=True)
    assert "dedup_session_no_activity" in scored.major_defects


def test_resolve_focus_product_id_prefers_external_id():
    focus = {"product_id": 99, "external_id": "sku-shoe-white", "sku": "ALT"}
    assert resolve_focus_product_id(focus) == "sku-shoe-white"


def test_context_retained_via_external_id_focus():
    state = {
        "focus_product_id": "sku-shoe-white",
        "conversation_focus": "product",
    }
    assert turn_has_focus_context(state) is True
    turns = [
        Layer3TurnEvidence(brain_state_before={}, brain_state_after={}),
        Layer3TurnEvidence(brain_state_after={"conversation_focus": "shipping_policy"}),
        Layer3TurnEvidence(
            brain_state_after={
                "focus_product_id": "sku-shoe-white",
                "has_suspended_product_focus": True,
                "suspended_product_focus": "sku-shoe-white",
            }
        ),
    ]
    assert context_retention_failed(turns) is False


def test_shipping_fee_verified_from_structured_evidence():
    turns = [
        Layer3TurnEvidence(
            outbound_reply="الشحن يعتمد على المدينة.",
            shipping_knowledge={"fee_sar": 25.0, "city": "الرياض"},
            verified_shipping_fee_sar=25.0,
        )
    ]
    assert shipping_fee_verified(turns, "", "25") is True
    assert shipping_policy_failed(turns, "", "25") is False
