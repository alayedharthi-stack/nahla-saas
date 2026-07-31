"""Commerce cheap-route enforcement and availability reply warm-up."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    commerce_prompt_max_chars,
    explain_commerce_prompt_slim_gate,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.cost.model_router import (  # noqa: E402
    detect_compose_standard_signals,
    resolve_compose_model_route,
)
from modules.ai.brain.cost.model_router_audit import TIER_CHEAP, TIER_STANDARD  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
    EVIDENCE_VARIANT_OPTIONS,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    build_operational_availability_conflict_reply,
)
from modules.ai.brain.commerce_reply_humanizer import apply_commerce_reply_humanizer  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    Intent,
    MerchantConversationState,
)
from modules.ai.orchestrator.llm_cost_audit import resolve_model_for_provider  # noqa: E402

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_DRY_AVAILABILITY_REPLY = "متوفر عسل طلح بعدة أحجام، أي حجم يناسبك؟"


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP", "gpt-5.6-luna")
    monkeypatch.setenv("NAHLA_MODEL_STANDARD", "gpt-5.6-terra")
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        orderable=True,
        store_name="متجر تجريبي",
    )


def _ctx(
    *,
    intent_name: str,
    message: str,
    reply_state: BrainReplyState | None = None,
) -> BrainContext:
    rs = reply_state or BrainReplyState(
        store_name="متجر تجريبي",
        intent_name=intent_name,
        primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
    )
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=Intent(name=intent_name, confidence=0.95, raw_message=message),
        state=MerchantConversationState(),
        facts=_facts(),
        reply_state=rs,
    )


class TestRoutineCommerceCheapRoute:
    @pytest.mark.parametrize(
        ("message", "intent"),
        [
            ("هل عندكم عسل طلح؟", INTENT_SOLUTION_SEEKING_COMMERCE),
            ("هل عندكم سدر؟", INTENT_ASK_PRODUCT),
            ("هل عندكم فساتين؟", INTENT_ASK_PRODUCT),
            ("عندكم جوالات؟", INTENT_ASK_PRODUCT),
            ("عندكم أقلام؟", INTENT_ASK_PRODUCT),
            ("متوفر الشاحن؟", INTENT_ASK_PRODUCT),
            ("بكم العباية؟", INTENT_ASK_PRICE),
            ("بكم عسل الطلح؟", INTENT_ASK_PRICE),
            ("متوفر السدر؟", INTENT_ASK_PRODUCT),
        ],
    )
    def test_resolve_route_cheap(self, message: str, intent: str) -> None:
        route = resolve_compose_model_route(
            intent_name=intent,
            reply_state=BrainReplyState(
                store_name="x",
                intent_name=intent,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            ),
        )
        assert route.tier == TIER_CHEAP
        assert route.model == "gpt-5.6-luna"
        assert "sonnet" not in route.model.lower()

    def test_routine_commerce_not_upgraded_by_soft_policy(self) -> None:
        needs, _ = detect_compose_standard_signals(
            intent_name=INTENT_ASK_PRODUCT,
            reply_state=BrainReplyState(
                store_name="x",
                intent_name=INTENT_ASK_PRODUCT,
                policy_reason="service_availability_not_handoff",
            ),
        )
        assert needs is False


class TestOperationalGuardThenStyledHumanizer:
    def test_guard_operational_then_humanizer_varies(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *a, **k: "عسل طلح",
        )
        ev = MagicMock()
        ev.evidence_state = EVIDENCE_VARIANT_OPTIONS
        ev.evidence_ok_for_positive = True
        operational = build_operational_availability_conflict_reply(ev)
        assert operational == "متوفر عسل طلح بعدة خيارات."
        assert operational != _DRY_AVAILABILITY_REPLY

        styled = apply_commerce_reply_humanizer(
            operational,
            inbound_text="هل عندكم عسل طلح؟",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            post_guard_rewrite=True,
            tenant_id=7,
            conversation_id=11,
            turn_id=2,
            product_title="عسل طلح",
        )
        assert styled.replaced
        assert styled.style_signature
        assert _DRY_AVAILABILITY_REPLY not in styled.reply
        assert "متوفر" in styled.reply and "عسل طلح" in styled.reply

    def test_dress_category_differs_from_honey(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *a, **k: "فساتين",
        )
        ev = MagicMock()
        ev.evidence_state = EVIDENCE_VARIANT_OPTIONS
        ev.evidence_ok_for_positive = True
        operational = build_operational_availability_conflict_reply(ev)
        styled = apply_commerce_reply_humanizer(
            operational,
            inbound_text="هل عندكم فساتين؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            post_guard_rewrite=True,
            tenant_id=7,
            conversation_id=22,
            turn_id=1,
            product_title="فساتين",
        ).reply
        assert "فساتين" in styled
        assert _DRY_AVAILABILITY_REPLY not in styled
