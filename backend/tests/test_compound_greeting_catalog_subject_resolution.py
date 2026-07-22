"""Platform-wide compound greeting + commerce subject resolution regressions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.persona.facts_bundle import PERSONA_COMPOSER_SURFACES  # noqa: E402
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    _extract_price_subject,
    _is_greeting_or_social_slot_token,
    _resolved_product_query,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
_PRODUCTION_MESSAGE = "السلام عليكم، كم سعر الفستان وهل هو متوفر؟"
_GENERIC_MESSAGE = "أهلًا، كم سعر العطر وهل هو متوفر؟"


def _ctx(
    message: str,
    *,
    intent_name: str = "ask_price",
    slots: dict | None = None,
    focus: dict | None = None,
) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="exploring")
    if focus:
        state.current_product_focus = dict(focus)
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=message,
            slots=dict(slots or {}),
            extraction_method="llm",
        ),
        state=state,
        history=[],
        facts=CommerceFacts(has_products=True, orderable=True, product_count=3),
    )


def _search_miss_result(*, message: str = "no_search_hits_no_top_fallback") -> ActionResult:
    return ActionResult(
        success=False,
        error="no_search_hits",
        data={"message": message},
    )


class TestCompoundGreetingSubjectResolution:
    def test_production_case_resolves_fistan_not_salam_slot(self) -> None:
        ctx = _ctx(
            _PRODUCTION_MESSAGE,
            slots={"product_query": "سلام"},
        )
        assert _extract_price_subject(_PRODUCTION_MESSAGE) == "فستان"
        assert _is_greeting_or_social_slot_token("سلام", _PRODUCTION_MESSAGE)
        assert _resolved_product_query(ctx) == "فستان"

    def test_explicit_arabic_subject_beats_conflicting_product_slot(self) -> None:
        ctx = _ctx(
            _PRODUCTION_MESSAGE,
            slots={"product_query": "عطر"},
        )
        assert _resolved_product_query(ctx) == "فستان"

    def test_explicit_english_subject_beats_conflicting_product_slot(self) -> None:
        message = (
            "Hello, how much is the white running shoe "
            "and is it available?"
        )
        ctx = _ctx(
            message,
            slots={"product_query": "rose perfume"},
        )
        assert _extract_price_subject(message) == "white running shoe"
        assert _resolved_product_query(ctx) == "white running shoe"

    def test_generic_perfume_greeting_resolves_subject(self) -> None:
        ctx = _ctx(
            _GENERIC_MESSAGE,
            slots={"product_query": "أهلًا"},
        )
        assert _extract_price_subject(_GENERIC_MESSAGE) in {"العطر", "عطر"}
        assert _resolved_product_query(ctx) in {"العطر", "عطر"}

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (
                "أهلًا، كم سعر حذاء رياضي أبيض وهل هو متوفر؟",
                "حذاء رياضي أبيض",
            ),
            (
                "مرحبًا، كم سعر عطر ورد 100ml وهل هو متوفر؟",
                "عطر ورد 100ml",
            ),
            (
                "Hi, how much is the rose perfume 100ml and is it in stock?",
                "rose perfume 100ml",
            ),
        ],
    )
    def test_multiword_subject_not_truncated_at_conjunction(
        self,
        message: str,
        expected: str,
    ) -> None:
        assert _extract_price_subject(message) == expected

    def test_decision_engine_search_query_uses_resolved_subject(self) -> None:
        ctx = _ctx(
            _PRODUCTION_MESSAGE,
            slots={"product_query": "سلام"},
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query") in {"فستان", "الفستان"}

    def test_greeting_only_does_not_resolve_product_query(self) -> None:
        msg = "السلام عليكم"
        ctx = _ctx(msg, intent_name="greeting", slots={"product_query": "سلام"})
        assert _resolved_product_query(ctx) == ""
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_pronoun_price_with_focus_preserves_context(self) -> None:
        focus = {"title": "حذاء رياضي أبيض", "external_id": "sku-1"}
        ctx = _ctx("سعره؟", slots={"product_query": "عطر"}, focus=focus)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("product") == focus


class TestCatalogMissComposePath:
    def test_search_miss_attempts_persona_compose_for_weak_query(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم الرياض", intent_name="ask_price")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "رياض"},
            reason="test",
        )
        result = _search_miss_result()

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_search_miss_answer",
                new=AsyncMock(
                    return_value=(
                        "ما لقيت تطابقاً واضحاً في الكتالوج حالياً.",
                        None,
                        {
                            "compose_source": "persona_llm",
                            "llm_candidate_present": True,
                            "final_text_transformed": False,
                            "chosen_path": "catalog_miss_resolved_subject",
                        },
                    ),
                ),
            ) as mock_compose:
                text = await composer.compose(decision, result, ctx)
                mock_compose.assert_awaited_once()
                return text

        text = asyncio.run(_run())
        assert "الكتالوج" in text
        assert result.data.get("chosen_path") == "catalog_miss_resolved_subject"

    def test_search_miss_provider_failure_emergency_fallback_metadata(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم سدر الحجاز", intent_name="ask_price")
        ctx.merchant_context = {
            "ai_settings": {
                "persona_composer_enabled": True,
                "store_ai_mode": "test",
                "ai_test_allowed_numbers": ["966500000001"],
                "persona_composer_surfaces": list(PERSONA_COMPOSER_SURFACES),
            },
        }
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "سدر الحجاز"},
            reason="test",
        )
        result = _search_miss_result()

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_search_miss_answer",
                new=AsyncMock(side_effect=RuntimeError("provider_down")),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert text
        assert result.data.get("compose_source") == "fallback_deterministic"
        assert result.data.get("fallback_reason") == "compose_exception:RuntimeError"
        assert result.data.get("fallback_action_type") == "catalog_search_miss"
        assert result.data.get("llm_candidate_present") is False
        assert result.data.get("compose_route_attempted") is True

    def test_subject_setup_exception_still_attempts_natural_compose(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("كم سعر قميص قطني أزرق؟", intent_name="ask_price")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "قميص قطني أزرق"},
            reason="test",
        )
        result = _search_miss_result()

        async def _run() -> str:
            with (
                patch(
                    "modules.ai.brain.clarification.resolved_product_guard."
                    "extract_resolved_product_subject",
                    side_effect=RuntimeError("setup_failed"),
                ),
                patch(
                    "modules.ai.brain.persona.catalog_product_answer."
                    "try_compose_catalog_search_miss_answer",
                    new=AsyncMock(
                        return_value=(
                            "ما ظهر تطابق مؤكد في الكتالوج حالياً.",
                            None,
                            {
                                "compose_source": "persona_llm",
                                "llm_candidate_present": True,
                                "chosen_path": "catalog_miss_resolved_subject",
                            },
                        )
                    ),
                ) as compose_mock,
            ):
                text = await composer.compose(decision, result, ctx)
                compose_mock.assert_awaited_once()
                return text

        text = asyncio.run(_run())
        assert text
        assert result.data.get("compose_source") == "persona_llm"
        assert result.data.get("compose_route_attempted") is True
        assert not result.data.get("fallback_reason")

    def test_resolved_subject_compose_receives_catalog_facts(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx(
            _PRODUCTION_MESSAGE,
            slots={"product_query": "سلام"},
        )
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "فستان"},
            reason="test",
        )
        result = ActionResult(
            success=True,
            data={
                "products": [],
                "catalog_fact_products": [
                    {
                        "title": "فستان",
                        "price": 164,
                        "in_stock": True,
                        "can_checkout": True,
                    }
                ],
            },
        )
        captured: dict = {}

        async def _stub_compose(**kwargs):
            captured.update(kwargs)
            return (
                "فستان متوفر بسعر 164 ريال.",
                None,
                {
                    "compose_source": "persona_llm",
                    "llm_candidate_present": True,
                    "chosen_path": "catalog_miss_resolved_subject",
                },
            )

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                new=AsyncMock(side_effect=_stub_compose),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert "164" in text or "فستان" in text
        assert captured.get("catalog_search_query") == "فستان"


def test_constitution_compound_greeting_no_new_deterministic_prose_paths() -> None:
    from modules.ai.compose.constitutional_policy import scan_responder_direct_template_returns

    hits = scan_responder_direct_template_returns(
        Path(__file__).resolve().parents[1] / "modules" / "ai" / "brain" / "compose" / "responder.py",
    )
    assert "compose_catalog_miss_deterministic_reply" not in hits
