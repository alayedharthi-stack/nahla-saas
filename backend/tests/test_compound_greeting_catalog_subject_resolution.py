"""Platform-wide compound greeting + commerce subject resolution regressions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
from modules.ai.compose.reply_metadata_export import (  # noqa: E402
    finalize_post_guard_compose_provenance,
)
from services.merchant_brain_turn import _build_provenance  # noqa: E402

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

    def test_generic_perfume_greeting_resolves_subject(self) -> None:
        ctx = _ctx(
            _GENERIC_MESSAGE,
            slots={"product_query": "أهلًا"},
        )
        assert _extract_price_subject(_GENERIC_MESSAGE) in {"العطر", "عطر"}
        assert _resolved_product_query(ctx) in {"العطر", "عطر"}

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
        ctx = _ctx("كم سعره؟", slots={"product_query": "ه"}, focus=focus)
        assert _resolved_product_query(ctx) == ""


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
        assert result.data.get("fallback_reason")
        assert result.data.get("fallback_action_type") == "catalog_search_miss"
        assert result.data.get("llm_candidate_present") is False

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


class TestGuardReplacementProvenance:
    def test_finalize_post_guard_compose_provenance_marks_guard_rewrite(self) -> None:
        data = {
            "compose_reply_candidate": "candidate from persona compose",
            "compose_source": "persona_llm",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
        }
        finalize_post_guard_compose_provenance(
            data,
            final_text="حدّد المنتج أو المقاس المطلوب.",
            guard_replaced={"commerce_reply_quality_guard": True},
        )
        assert data["final_text_transformed"] is True
        assert data["final_transform_reasons"] == ["commerce_reply_quality_guard"]
        assert data["final_customer_text_source"] == "guard_rewrite"
        assert data["compose_source"] == "persona_llm"

    def test_build_provenance_uses_compose_reply_candidate_boundary(self) -> None:
        provenance = _build_provenance(
            brain_result={
                "compose_source": "persona_llm",
                "chosen_path": "catalog_miss_resolved_subject",
                "llm_candidate_present": True,
                "final_text_transformed": True,
                "final_transform_reasons": ["commerce_reply_quality_guard"],
                "compose_reply_candidate": "LLM catalog answer about فستان",
            },
            brain_reply_candidate="LLM catalog answer about فستان",
            reply_text="حدّد المنتج أو المقاس المطلوب.",
            brain_persona_compose_event={
                "compose_source": "persona_llm",
                "chosen_path": "catalog_miss_resolved_subject",
                "llm_candidate_present": True,
            },
            trace=SimpleNamespace(chosen_path="", reply_source="", fallback_source=""),
            live_provenance_tracker={
                "final_transform_reasons": ["commerce_reply_quality_guard"],
            },
        )
        assert provenance.final_text_transformed is True
        assert provenance.final_transform_reasons == ["commerce_reply_quality_guard"]
        assert provenance.llm_candidate_present is True
        assert provenance.compose_source == "persona_llm"


def test_constitution_compound_greeting_no_new_deterministic_prose_paths() -> None:
    from modules.ai.compose.constitutional_policy import scan_responder_direct_template_returns

    hits = scan_responder_direct_template_returns(
        Path(__file__).resolve().parents[1] / "modules" / "ai" / "brain" / "compose" / "responder.py",
    )
    assert "compose_catalog_miss_deterministic_reply" not in hits
