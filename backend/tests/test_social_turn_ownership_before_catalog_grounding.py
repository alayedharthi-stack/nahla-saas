"""PR-D3.5 — social turn ownership before stale commerce/catalog grounding."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.conversation_state_isolation import (  # noqa: E402
    should_replay_pending_question,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    StaffContactRecord,
    build_deliver_reply_text,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.state.stages import STAGE_DECIDING, STAGE_DISCOVERY, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_TALK_HUMAN,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
        top_products=[{"id": 1, "title": "عسل طلح", "external_id": "sku-1"}],
    )


def _ctx(
    message: str,
    *,
    intent: Intent | None = None,
    stage: str = STAGE_DISCOVERY,
    product_focus: bool = False,
    order_prep: OrderPreparationState | None = None,
    last_question: str = "",
    metadata: dict | None = None,
) -> BrainContext:
    resolved_intent = intent or intent_rules.match(message) or Intent(
        name=INTENT_GENERAL,
        confidence=0.55,
        raw_message=message,
    )
    state = MerchantConversationState(greeted=True, stage=stage)
    if product_focus:
        state.current_product_focus = {
            "id": 1,
            "title": "عسل طلح",
            "external_id": "sku-1",
            "can_checkout": True,
            "price": 100,
        }
    if order_prep is not None:
        state.order_prep = order_prep
    state.last_question_asked = last_question
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=resolved_intent,
        state=state,
        facts=_facts(),
        profile={"inbound_metadata": dict(metadata or {})},
    )


def _evidence() -> ProductClaimGroundingEvidence:
    return ProductClaimGroundingEvidence(
        available_products=({"id": 1, "title": "عسل طلح", "can_checkout": True},),
    )


def test_salam_without_al_classifies_as_greeting() -> None:
    intent = intent_rules.match("سلام عليكم")
    assert intent is not None
    assert intent.name == INTENT_GREETING


@pytest.mark.parametrize("message", ["سلام عليكم", "السلام عليكم"])
def test_greeting_during_stale_ordering_does_not_continue_checkout(message: str) -> None:
    decision = DefaultDecisionEngine().decide(
        _ctx(message, stage=STAGE_ORDERING, product_focus=True),
    )
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
    assert decision.args.get("persona_kind") == "greeting"
    assert decision.args.get("block_commerce_escalation") is True


def test_greeting_plus_product_intent_remains_commerce() -> None:
    message = "السلام عليكم أبي عسل طلح كيلو"
    intent = intent_rules.match(message)
    verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
    assert verdict.matched is False


def test_explicit_handoff_not_classified_as_social_noncommerce() -> None:
    message = "حولني لموظف"
    intent = Intent(name=INTENT_TALK_HUMAN, confidence=0.92, raw_message=message)
    verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
    assert verdict.matched is False


def test_explicit_handoff_without_active_order_routes_handoff() -> None:
    message = "حولني لموظف"
    decision = DefaultDecisionEngine().decide(
        _ctx(
            message,
            intent=Intent(name=INTENT_TALK_HUMAN, confidence=0.92, raw_message=message),
            stage=STAGE_DISCOVERY,
        ),
    )
    assert decision.action == ACTION_HANDOFF
    assert not (decision.args or {}).get("during_active_order")


def test_explicit_handoff_during_stale_order_routes_handoff() -> None:
    message = "حولني لموظف"
    prep = OrderPreparationState.from_dict(
        {"product_id": "sku-1", "product_name": "عسل طلح"},
    )
    decision = DefaultDecisionEngine().decide(
        _ctx(
            message,
            intent=Intent(name=INTENT_TALK_HUMAN, confidence=0.92, raw_message=message),
            stage=STAGE_ORDERING,
            product_focus=True,
            order_prep=prep,
        ),
    )
    assert decision.action == ACTION_HANDOFF
    assert (decision.args or {}).get("during_active_order") is True


def test_greeting_only_does_not_route_handoff() -> None:
    message = "سلام عليكم"
    decision = DefaultDecisionEngine().decide(
        _ctx(message, stage=STAGE_ORDERING, product_focus=True),
    )
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_HANDOFF


@pytest.mark.parametrize(
    "message",
    [
        "سلام عليكم",
        "شكرا",
        "الله يوفقك",
        "الف مبروك",
        "من أمين؟",
        "هههه",
        "قصدي هب لطلابك 🤣",
    ],
)
def test_social_noncommerce_blocks_pending_question_replay(message: str) -> None:
    assert should_replay_pending_question(
        inbound_text=message,
        last_question="ما المدينة التي تشحن لها؟",
    ) is False


@pytest.mark.parametrize("message", ["هب لهم من كيلو كيلو", "كيلو كيلو", "نص كيلو"])
def test_quantity_like_without_product_does_not_start_catalog_or_checkout(message: str) -> None:
    decision = DefaultDecisionEngine().decide(
        _ctx(
            message,
            intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message=message),
            stage=STAGE_ORDERING,
            product_focus=True,
        ),
    )
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
    assert decision.args.get("block_commerce_escalation") is True


def test_quantity_answer_still_works_when_quantity_slot_is_active() -> None:
    prep = OrderPreparationState.from_dict(
        {"product_id": "sku-1", "missing_fields": ["quantity"]},
    )
    decision = DefaultDecisionEngine().decide(
        _ctx(
            "نص كيلو",
            intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message="نص كيلو"),
            stage=STAGE_ORDERING,
            product_focus=True,
            order_prep=prep,
            last_question="كم الكمية تحتاج؟",
        ),
    )
    assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


def test_social_audio_transcript_blocks_catalog_grounding_rewrite() -> None:
    result = apply_catalog_product_grounding_guard(
        reply="أقترح عليك عسل القطف.",
        inbound_text="الصوت غير واضح",
        evidence=_evidence(),
        inbound_metadata={"normalized_type": "audio"},
    )
    assert result.replaced is False
    assert result.action == "allowed_social_noncommerce"
    assert "الكتالوج" not in result.reply


def test_congratulations_does_not_get_catalog_grounding_prompt() -> None:
    result = apply_catalog_product_grounding_guard(
        reply="أقترح عليك عسل القطف.",
        inbound_text="الف الف مبروك وياربي توفقهم جميعا",
        evidence=_evidence(),
    )
    assert result.replaced is False
    assert result.action == "allowed_social_noncommerce"
    assert "تبغاني أرسل لك الأقرب من الكتالوج" not in result.reply


def test_explicit_catalog_request_still_allows_catalog_grounding_rewrite() -> None:
    result = apply_catalog_product_grounding_guard(
        reply="أقترح عليك عسل القطف.",
        inbound_text="أرسل الكتالوج",
        evidence=_evidence(),
    )
    assert result.replaced is True
    assert "الكتالوج" in result.reply


def test_staff_question_does_not_route_to_catalog_or_checkout() -> None:
    message = "من أمين؟"
    decision = DefaultDecisionEngine().decide(
        _ctx(
            message,
            intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message=message),
            stage=STAGE_DECIDING,
            product_focus=True,
        ),
    )
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
    assert decision.args.get("block_commerce_escalation") is True


def test_staff_contact_reply_does_not_overclaim_owner_or_presence() -> None:
    reply = build_deliver_reply_text(
        StaffContactRecord(
            lookup_name="أمين",
            phone="+966500000001",
            section_id=1,
            role="showroom",
            aliases=("بائع المعرض",),
            is_owner=False,
            chain_index=0,
            source="test",
        ),
    )
    assert "صاحب المتجر" not in reply
    assert "بتلاقيه" not in reply
    assert "موجود" not in reply
    assert "مشغول" not in reply


@pytest.mark.parametrize(
    "message",
    [
        "وش الأنواع المتوفرة؟",
        "أرسل الكتالوج",
        "أبي عسل الطلح كيلو",
        "هل السمر متوفر؟",
    ],
)
def test_explicit_catalog_and_product_cases_remain_commerce(message: str) -> None:
    intent = intent_rules.match(message)
    verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
    assert verdict.matched is False
