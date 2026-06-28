"""PR-D5 — grounded staff presence guard."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    StaffContactRecord,
    StaffContactRegistry,
    build_staff_identity_reply_text,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.staff_presence_evidence import (  # noqa: E402
    StaffPresenceEvidence,
    evaluate_staff_presence_evidence,
)
from modules.ai.brain.postprocess.staff_presence_truth_guard import (  # noqa: E402
    apply_staff_presence_truth_guard,
    reply_contains_forbidden_staff_claim,
    reply_contains_owner_overclaim,
    reply_contains_presence_overclaim,
)
from modules.ai.brain.state.stages import STAGE_DECIDING, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)


def _ameen_record(*, is_owner: bool = False) -> StaffContactRecord:
    return StaffContactRecord(
        lookup_name="أمين",
        phone="+966500050674",
        section_id=1,
        role="showroom",
        aliases=("بائع المعرض",),
        is_owner=is_owner,
        chain_index=0,
        source="test",
    )


def _registry() -> StaffContactRegistry:
    return StaffContactRegistry(records=(_ameen_record(),))


def _evidence(**kwargs) -> StaffPresenceEvidence:
    base = dict(
        matched_record=_ameen_record(),
        registry_records=(_ameen_record(),),
        staff_context_active=True,
        evidence_source="test",
    )
    base.update(kwargs)
    return StaffPresenceEvidence(**base)


def _ctx(message: str, *, stage: str = STAGE_DECIDING, product_focus: bool = False) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage=stage)
    if product_focus:
        state.current_product_focus = {"id": 1, "title": "عسل طلح"}
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message=message),
        state=state,
        facts=CommerceFacts(has_products=True, product_count=5, in_stock_count=5),
    )


def test_identity_reply_uses_configured_role_only() -> None:
    reply = build_staff_identity_reply_text(_ameen_record())
    assert "أمين" in reply
    assert "بائع المعرض" in reply
    assert "صاحب المتجر" not in reply
    assert "بتلاقيه" not in reply
    assert "موجود" not in reply


def test_owner_overclaim_blocked_for_non_owner_record() -> None:
    evidence = _evidence()
    llm_reply = "أمين هو صاحب المتجر وبتلاقيه في معرض آل عايد بالطائف."
    assert reply_contains_owner_overclaim(llm_reply, evidence=evidence)
    assert reply_contains_presence_overclaim(llm_reply, evidence=evidence)

    result = apply_staff_presence_truth_guard(
        reply=llm_reply,
        inbound_text="من أمين؟",
        registry=_registry(),
    )
    assert result.replaced is True
    assert "صاحب المتجر" not in result.reply
    assert "بتلاقيه" not in result.reply
    assert "بائع المعرض" in result.reply


def test_location_presence_claim_blocked_for_wain_ameen() -> None:
    evidence = _evidence()
    llm_reply = "أمين موجود في المعرض، بتلاقيه هناك."
    result = apply_staff_presence_truth_guard(
        reply=llm_reply,
        inbound_text="وين أمين؟",
        registry=_registry(),
    )
    assert result.replaced is True
    assert "موجود" not in result.reply
    assert "بتلاقيه" not in result.reply
    assert "مشغول" not in result.reply


def test_busy_claim_blocked_without_availability_evidence() -> None:
    evidence = _evidence()
    llm_reply = "أمين مشغول الآن، جرب بعد شوي."
    result = apply_staff_presence_truth_guard(
        reply=llm_reply,
        inbound_text="وين أمين؟",
        registry=_registry(),
    )
    assert result.replaced is True
    assert "مشغول" not in result.reply


def test_contact_request_uses_grounded_deliver_text() -> None:
    evidence = _evidence()
    llm_reply = "أمين موجود، هذا رقم وهمي 0500009999."
    result = apply_staff_presence_truth_guard(
        reply=llm_reply,
        inbound_text="أرسل رقمه",
        registry=_registry(),
    )
    assert result.replaced is True
    assert "0500009999" not in result.reply
    assert "تتواصل" in result.reply


def test_allowed_role_fact_preserved_when_no_overclaim() -> None:
    evidence = _evidence()
    grounded = "أمين بائع المعرض."
    assert reply_contains_forbidden_staff_claim(grounded, evidence=evidence) is False
    result = apply_staff_presence_truth_guard(
        reply=grounded,
        inbound_text="من أمين؟",
    )
    assert result.replaced is False


def test_owner_claim_allowed_when_record_is_owner() -> None:
    owner = _ameen_record(is_owner=True)
    evidence = _evidence(matched_record=owner, registry_records=(owner,))
    llm_reply = "صاحب المتجر متواجد للتواصل."
    assert reply_contains_owner_overclaim(llm_reply, evidence=evidence) is False


def test_presence_claim_allowed_with_availability_evidence() -> None:
    evidence = _evidence(
        availability_status="available",
        availability_evidence_source="test_status_feed",
    )
    llm_reply = "أمين موجود الآن في المعرض."
    assert reply_contains_presence_overclaim(llm_reply, evidence=evidence) is False


def test_staff_question_does_not_route_to_catalog() -> None:
    decision = DefaultDecisionEngine().decide(_ctx("من أمين؟", product_focus=True))
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


def test_pr_d35_greeting_still_blocks_checkout() -> None:
    message = "سلام عليكم"
    decision = DefaultDecisionEngine().decide(_ctx(message, stage=STAGE_ORDERING, product_focus=True))
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


def test_pr_d35_mixed_greeting_product_remains_commerce() -> None:
    message = "السلام عليكم أبي عسل طلح كيلو"
    verdict = resolve_current_turn_social_non_commerce(
        message,
        intent=intent_rules.match(message),
    )
    assert verdict.matched is False


def test_evaluate_staff_presence_evidence_matches_registry_name() -> None:
    evidence = evaluate_staff_presence_evidence(
        message="من أمين؟",
        registry=_registry(),
    )
    assert evidence.staff_context_active is True
    assert evidence.matched_record is not None
    assert evidence.matched_record.lookup_name == "أمين"
