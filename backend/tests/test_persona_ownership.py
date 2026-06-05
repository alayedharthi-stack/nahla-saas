"""Tests for persona ownership measurement (no reply behaviour changes)."""
from __future__ import annotations

from dataclasses import dataclass

from modules.ai.brain.persona_ownership import (
    PersonaBypassReason,
    PersonaOwnershipRecord,
    build_brain_persona_ownership,
    sync_persona_to_turn_trace,
)
from services.turn_trace import TurnTrace


@dataclass
class _FakeReplyState:
    persona_expression_mode: bool = False
    persona_topic: str = ""


def test_stamp_persona():
    rec = PersonaOwnershipRecord()
    rec.stamp_persona(topic="persona_social", kind="greeting")
    assert rec.persona_stamped is True
    assert rec.persona_topic == "persona_social"
    assert rec.bypass_reason is None


def test_invalidate_stamp_after_guard():
    rec = PersonaOwnershipRecord()
    rec.stamp_persona(topic="persona_identity")
    rec.invalidate_stamp(PersonaBypassReason.TRUTH_GUARD_REWRITE, "staff_escalation_truth_guard")
    assert rec.persona_stamped is False
    assert rec.bypass_reason == PersonaBypassReason.TRUTH_GUARD_REWRITE.value
    assert "staff_escalation_truth_guard" in rec.pre_stamp_layers


def test_build_brain_persona_social():
    rs = _FakeReplyState(persona_expression_mode=True, persona_topic="persona_social")
    rec = build_brain_persona_ownership(
        decision_action="llm_reply",
        decision_args={"persona_kind": "greeting"},
        reply_state=rs,
        chosen_path="llm",
    )
    assert rec.persona_stamped is True
    assert rec.persona_topic == "persona_social"


def test_build_brain_commerce_llm():
    rs = _FakeReplyState(persona_expression_mode=False)
    rec = build_brain_persona_ownership(
        decision_action="llm_reply",
        decision_args={},
        reply_state=rs,
        chosen_path="llm",
    )
    assert rec.persona_stamped is False
    assert rec.bypass_reason == PersonaBypassReason.COMMERCE_LLM.value


def test_build_brain_social_template():
    rec = build_brain_persona_ownership(
        decision_action="social_reply",
        decision_args={"social_category": "thanks"},
        reply_state=_FakeReplyState(),
        chosen_path="action",
    )
    assert rec.bypass_reason == PersonaBypassReason.SOCIAL_TEMPLATE.value


def test_on_text_replaced_noop_when_same():
    rec = PersonaOwnershipRecord()
    rec.stamp_persona(topic="persona_social")
    rec.on_text_replaced(
        layer="dedup",
        reason=PersonaBypassReason.DEDUP_REPLY,
        before="hello",
        after="hello",
    )
    assert rec.persona_stamped is True


def test_sync_persona_to_turn_trace():
    trace = TurnTrace(tenant_id=1, phone="966500000000")
    rec = PersonaOwnershipRecord()
    rec.mark_bypass(PersonaBypassReason.PRE_BRAIN_FAST_PATH, owner="render_identity_reply")
    sync_persona_to_turn_trace(trace, rec)
    assert trace.persona_stamped is False
    assert trace.bypass_reason == PersonaBypassReason.PRE_BRAIN_FAST_PATH.value
    assert trace.extra.get("persona_ownership", {}).get("bypass_reason") == (
        PersonaBypassReason.PRE_BRAIN_FAST_PATH.value
    )


def test_finalize_unknown_when_unset():
    rec = PersonaOwnershipRecord()
    final = rec.finalize()
    assert final.persona_stamped is False
    assert final.bypass_reason == PersonaBypassReason.UNKNOWN.value
