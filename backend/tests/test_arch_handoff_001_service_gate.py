"""
ARCH-HANDOFF-001 — service availability gate vs talk_to_human / pre-brain handoff.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.handoff_detector import is_handoff_request  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_HANDOFF, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.rules import match as match_intent  # noqa: E402
from modules.ai.brain.intent.service_availability_gate import (  # noqa: E402
    is_service_availability_inquiry,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_TALK_HUMAN,
    Intent,
    MerchantConversationState,
)


# ── False positives that must NOT escalate ───────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "فيه أحد يلسعني بالرياض؟",
        "فيه احد يقدر يلسعني بالرياض",
        "هل يوجد أحد يسوي لسع نحل بالرياض؟",
        "فيه أحد يقدر يوصل لي الطلب اليوم؟",
        "هل في أحد يساعد في التركيب؟",
        "فيه احد يصلح الجهاز عندكم؟",
        "هل يوجد أحد يقدر يشرح لي طريقة الاستخدام؟",
        "فيه أحد يقدر يعمل لي عرض سعر؟",
        "هل في أحد يسوي صيانة؟",
        "فيه احد يقدر يجهز الطلب بسرعة؟",
        "فيه أحد بالرياض يبيع العسل؟",
        "هل في أحد قريب مني بالدمام؟",
        "فيه أحد في الرياض؟",
        "هل في أحد بالرياض؟",
        "فيه أحد مختص بالعسل السدر؟",
        "هل يوجد أحد خبير في النحل؟",
        "فيه احد دكتور يفهم لسع النحل؟",
        "هل في أحد فني تركيب؟",
        "فيه أحد استشاري تغذية؟",
        "فيه أحد يعرف أبو هشام؟",
        "فيه احد من عندكم اسمه خالد؟",
        "فيه أحد يقدر يفيدني بخصوص العسل؟",
        "هل يوجد أحد يساعدني أختار النوع المناسب؟",
        "فيه احد يقدر يشرح لي الفرق بين السدر والطلح؟",
        "هل في أحد ينصحني وش الأفضل للسكر؟",
        "فيه أحد يقدر يجاوب على استفساري؟",
        "فيه موظف يقدر يساعدني؟",
        "هل يوجد مختص بالعسل؟",
        "فيه شخص يشرح لي الفرق؟",
    ],
)
def test_service_inquiry_must_not_be_talk_to_human(message: str):
    assert is_service_availability_inquiry(message)
    intent = match_intent(message)
    assert intent is None or intent.name != INTENT_TALK_HUMAN
    assert not is_handoff_request(message)


# ── True handoff — must still escalate ───────────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "فيه أحد يرد؟",
        "فيه احد يرد علي",
        "هل في أحد يرد",
        "هل يوجد أحد يتواصل معي",
        "محد رد علي",
        "كلموني",
        "حولني للموظف",
        "ابي اكلم احد",
        "ابغى اتكلم مع موظف",
        "فيه موظف يرد؟",
        "هل يوجد موظف",
        "فيه أحد؟",
        "هل في أحد؟",
        "فيه احد هنا",
        "في احد يرد",
        "محد يرد",
        "ما احد يرد",
    ],
)
def test_genuine_handoff_stays_talk_to_human(message: str):
    assert not is_service_availability_inquiry(message)
    intent = match_intent(message)
    assert intent is not None
    assert intent.name == INTENT_TALK_HUMAN


@pytest.mark.parametrize(
    "message",
    [
        "فيه احد يرد علي",
        "هل في احد يرد",
        "كلموني",
        "حولني للموظف",
    ],
)
def test_pre_brain_handoff_still_fires_for_genuine_requests(message: str):
    assert is_handoff_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "هل يوجد أحد يقدر يشرح لي طريقة الاستخدام؟",
        "هل في أحد يساعد في التركيب؟",
        "هل في أحد بالرياض؟",
    ],
)
def test_pre_brain_blocked_for_service_availability(message: str):
    assert not is_handoff_request(message)


def test_decision_engine_routes_service_inquiry_to_llm_not_handoff():
    msg = "فيه أحد يلسعني بالرياض؟"
    intent = Intent(
        name=INTENT_TALK_HUMAN,
        confidence=0.92,
        raw_message=msg,
        extraction_method="test",
    )
    state = MerchantConversationState()
    ctx = BrainContext(
        message=msg,
        tenant_id=33,
        customer_phone="966500000000",
        intent=intent,
        state=state,
        facts=CommerceFacts(),
        history=[],
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_HANDOFF
    assert (decision.args or {}).get("policy_reason") == "service_availability_not_handoff"
