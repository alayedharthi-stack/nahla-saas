"""Unit tests for Layer 3 scoring helpers (harness contract only)."""
from __future__ import annotations

from tests.salla_acceptance.layer3_evidence_utils import resolve_focus_product_id
from tests.salla_acceptance.layer3_harness import Layer3TurnEvidence
from tests.salla_acceptance.layer3_scoring import (
    aggregate_suite_scores,
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


def _clean_script(**overrides) -> Layer3SessionScript:
    base = {
        "session_id": "L3-CLEAN",
        "group": 2,
        "tenant": "A",
        "customer_key": "A",
        "tester_role": "ordinary",
        "messages": ["مرحبا"],
        "expected_checks": {},
    }
    base.update(overrides)
    return Layer3SessionScript(**base)


def _llm_turn(inbound: str, reply: str, **kwargs) -> Layer3TurnEvidence:
    return Layer3TurnEvidence(
        inbound_text=inbound,
        outbound_reply=reply,
        brain_called=True,
        compose_invoked=1,
        compose_source="llm",
        **kwargs,
    )


def test_defect_free_session_scores_100_percent():
    script = _clean_script()
    turns = [_llm_turn("مرحبا", "أهلاً بك، كيف أقدر أساعدك؟")]
    scored = score_session(script, turns, compose_real=True)
    assert scored.session_pct == 100.0
    assert all(score == 5 for score in scored.axis_scores.values())
    assert not scored.major_defects
    assert not scored.critical_defects


def test_defect_free_aggregate_axes_score_100_percent():
    script = _clean_script()
    turns = [_llm_turn("مرحبا", "أهلاً بك، كيف أقدر أساعدك؟")]
    scored = score_session(script, turns, compose_real=True)
    agg = aggregate_suite_scores([scored])
    assert agg["average_session_pct"] == 100.0
    assert all(avg == 5.0 for avg in agg["axis_averages"].values())
    assert agg["isolation_accuracy_pct"] == 100.0
    assert agg["context_accuracy_pct"] == 100.0
    assert agg["conversation_quality_score"] == 100.0


def test_context_retention_major_only_when_required_and_failed():
    script = Layer3SessionScript(
        session_id="L3-CTX-REQ",
        group=3,
        tenant="A",
        customer_key="A",
        tester_role="ordinary",
        messages=["a", "b", "c"],
        expected_checks={"context_retention_required": True},
    )
    turns = [
        _llm_turn("a", "r1", brain_state_after={}),
        _llm_turn("b", "r2", brain_state_after={}),
        _llm_turn("c", "r3", brain_state_after={}),
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "context_not_retained" in scored.major_defects
    assert scored.axis_scores["context_retention"] == 2


def test_context_retention_skipped_without_required_flag():
    script = Layer3SessionScript(
        session_id="L3-G1-06",
        group=1,
        tenant="A",
        customer_key="C",
        tester_role="difficult",
        messages=["a", "b", "c"],
    )
    turns = [
        _llm_turn("a", "r1", brain_state_after={}),
        _llm_turn("b", "r2", brain_state_after={}),
        _llm_turn("c", "r3", brain_state_after={}),
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "context_not_retained" not in scored.major_defects
    assert scored.axis_scores["context_retention"] == 5


def test_context_retention_passes_with_focus_when_required():
    script = Layer3SessionScript(
        session_id="L3-CTX-FOCUS",
        group=3,
        tenant="A",
        customer_key="A",
        tester_role="ordinary",
        messages=["a", "b", "c"],
        expected_checks={"context_retention_required": True},
    )
    turns = [
        _llm_turn("a", "r1", brain_state_after={}),
        _llm_turn("b", "r2", brain_state_after={}),
        _llm_turn(
            "c",
            "r3",
            brain_state_after={
                "focus_product_id": "sku-shoe-white",
                "conversation_focus": "product",
            },
        ),
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "context_not_retained" not in scored.major_defects
    assert scored.axis_scores["context_retention"] == 5


def test_handoff_zero_compose_exempt_with_audit_note():
    script = Layer3SessionScript(
        session_id="L3-G7-01",
        group=7,
        tenant="A",
        customer_key="C",
        tester_role="ordinary",
        messages=["أبغى موظف", "حد يرد؟", "كم السعر؟"],
        expected_checks={"handoff_then_no_commerce": True},
    )
    turns = [
        Layer3TurnEvidence(inbound_text="أبغى موظف", outbound_reply="تم التحويل", handoff_active=True),
        Layer3TurnEvidence(inbound_text="حد يرد؟", outbound_reply="الموظف سيرد", handoff_active=True),
        Layer3TurnEvidence(inbound_text="كم السعر؟", outbound_reply="تحت متابعة الموظف", handoff_active=True),
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "no_llm_compose_observed" not in scored.major_defects
    assert "compose_not_expected_handoff" in scored.notes
    assert scored.axis_scores["compose_quality"] == 5


def test_non_handoff_zero_compose_still_major():
    script = _clean_script(session_id="L3-NO-COMPOSE")
    turns = [
        Layer3TurnEvidence(
            inbound_text="مرحبا",
            outbound_reply="أهلاً",
            brain_called=True,
        )
    ]
    scored = score_session(script, turns, compose_real=True)
    assert "no_llm_compose_observed" in scored.major_defects
    assert scored.axis_scores["compose_quality"] == 2


def test_dedup_compose_quality_unchanged_without_no_llm_major():
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
    assert "no_llm_compose_observed" not in scored.major_defects
    assert scored.axis_scores["compose_quality"] == 5
