"""Tests for persona ownership measurement (no reply behaviour changes)."""
from __future__ import annotations

from dataclasses import dataclass

from modules.ai.brain.persona_ownership import (
    PersonaBypassReason,
    PersonaOwnershipRecord,
    build_brain_persona_ownership,
    sync_persona_to_turn_trace,
)
from modules.ai.compose.reply_metadata_export import finalize_post_guard_compose_provenance
from core.outbound_text_policy import reconcile_outbound_compose_provenance
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


def test_build_brain_persona_llm_overrides_search_products_template_mapping():
    rec = build_brain_persona_ownership(
        decision_action="search_products",
        decision_args={"query": "حذاء رياضي أبيض"},
        reply_state=_FakeReplyState(),
        chosen_path="fact_bound_persona_compose",
        compose_source="persona_llm",
        llm_candidate_present=True,
        persona_topic_hint="compound",
    )
    assert rec.persona_stamped is True
    assert rec.expression_owner == "persona_llm"
    assert rec.bypass_reason is None
    assert rec.persona_topic == "compound"


def test_build_brain_fallback_deterministic_on_search_products():
    rec = build_brain_persona_ownership(
        decision_action="search_products",
        decision_args={"query": "قميص قطني أزرق"},
        reply_state=_FakeReplyState(),
        chosen_path="catalog_miss_resolved_subject",
        compose_source="fallback_deterministic",
        llm_candidate_present=False,
    )
    assert rec.persona_stamped is False
    assert rec.bypass_reason == PersonaBypassReason.FALLBACK_REPLY.value
    assert rec.expression_owner == "catalog_miss_resolved_subject"


def test_build_brain_merchant_template_ownership():
    rec = build_brain_persona_ownership(
        decision_action="faq_reply",
        decision_args={},
        reply_state=_FakeReplyState(),
        chosen_path="merchant_template",
        compose_source="merchant_template",
        llm_candidate_present=False,
    )
    assert rec.bypass_reason == PersonaBypassReason.TEMPLATE_PATH.value
    assert rec.expression_owner == "template:merchant_template"


def test_build_brain_unapproved_source_cannot_claim_llm_ownership():
    rec = build_brain_persona_ownership(
        decision_action="search_products",
        decision_args={},
        reply_state=_FakeReplyState(),
        chosen_path="fact_bound_persona_compose",
        compose_source="arbitrary_runtime_source",
        llm_candidate_present=True,
    )
    assert rec.bypass_reason == PersonaBypassReason.TEMPLATE_PATH.value
    assert "search_products" in rec.expression_owner


def test_final_boundary_service_closer_persona_ownership_stays_llm():
    result_data = {
        "compose_source": "persona_llm",
        "chosen_path": "fact_bound_persona_compose",
        "llm_candidate_present": True,
        "question_kind": "compound",
        "compose_reply_candidate": (
            "حذاء رياضي أبيض سعره 220 ريال وهو متوفر. كيف أقدر أساعدك اليوم؟"
        ),
    }
    final_reply = "حذاء رياضي أبيض سعره 220 ريال وهو متوفر."
    finalize_post_guard_compose_provenance(
        result_data,
        final_text=final_reply,
        guard_replaced={"service_closer_guard": True},
    )
    reconcile_outbound_compose_provenance(
        result_data,
        decision_action="search_products",
        intent="ask_price",
        final_text=final_reply,
    )
    rec = build_brain_persona_ownership(
        decision_action="search_products",
        decision_args={"query": "حذاء رياضي أبيض"},
        reply_state=None,
        chosen_path="fact_bound_persona_compose",
        guard_replaced={"service_closer_guard": True},
        compose_source=result_data.get("compose_source"),
        llm_candidate_present=result_data.get("llm_candidate_present"),
        persona_topic_hint=str(result_data.get("question_kind") or ""),
        final_customer_text_source=result_data.get("final_customer_text_source"),
        final_text_transformed=result_data.get("final_text_transformed"),
        compose_reply_candidate=result_data.get("compose_reply_candidate"),
        final_reply=final_reply,
    )
    payload = rec.to_dict()
    assert payload["persona_stamped"] is True
    assert payload["expression_owner"] == "persona_llm"
    assert payload["bypass_reason"] is None


def test_final_boundary_wholesale_rewrite_not_persona_llm():
    result_data = {
        "compose_source": "persona_llm",
        "chosen_path": "general_offer_discovery_compose",
        "llm_candidate_present": True,
        "compose_reply_candidate": "في منتجات بأسعار مخفّضة حسب بيانات الكتالوج.",
        "general_offer_discovery_compose_active": True,
    }
    guard_reply = "نص بديل حتمي من الحارس بالكامل."
    from modules.ai.brain.persona.product_sale_offer_provenance import (  # noqa: PLC0415
        begin_product_sale_offer_text_tracking,
        finalize_product_sale_offer_text_provenance,
    )

    begin_product_sale_offer_text_tracking(
        result_data,
        "في منتجات بأسعار مخفّضة حسب بيانات الكتالوج.",
    )
    finalize_product_sale_offer_text_provenance(
        result_data,
        guard_reply,
        guard_replaced={"saudi_dialect_guard": True},
    )
    finalize_post_guard_compose_provenance(
        result_data,
        final_text=guard_reply,
        guard_replaced={"saudi_dialect_guard": True},
    )
    reconcile_outbound_compose_provenance(
        result_data,
        decision_action="search_products",
        intent="ask_product",
        final_text=guard_reply,
    )
    rec = build_brain_persona_ownership(
        decision_action="search_products",
        decision_args={},
        reply_state=None,
        chosen_path="general_offer_discovery_compose",
        guard_replaced={"saudi_dialect_guard": True},
        compose_source=result_data.get("compose_source"),
        llm_candidate_present=result_data.get("llm_candidate_present"),
        final_customer_text_source=result_data.get("final_customer_text_source"),
        final_text_transformed=result_data.get("final_text_transformed"),
        compose_reply_candidate=result_data.get("compose_reply_candidate"),
        final_reply=guard_reply,
    )
    payload = rec.to_dict()
    assert result_data.get("final_customer_text_source") not in {
        "persona_llm",
        "persona_llm_postprocess",
    }
    assert payload["persona_stamped"] is False
    assert payload["bypass_reason"] == PersonaBypassReason.TRUTH_GUARD_REWRITE.value
    assert payload["expression_owner"] == "saudi_dialect_guard"
    assert payload["expression_owner"] != "persona_llm"
    assert "template:search_products" not in str(payload["expression_owner"] or "")


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
