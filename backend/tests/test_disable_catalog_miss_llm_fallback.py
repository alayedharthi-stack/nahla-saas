"""Catalog miss: weak queries stay deterministic; resolved-subject miss is LLM-owned."""
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

from modules.ai.brain.commerce.catalog_search_evidence import (  # noqa: E402
    CATALOG_MISS_CHOSEN_PATH,
    compose_catalog_miss_deterministic_reply,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.persona.facts_bundle import PERSONA_COMPOSER_SURFACES  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(
    message: str,
    *,
    intent_name: str = "ask_product",
) -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=message,
            extraction_method="rules",
        ),
        state=MerchantConversationState(greeted=True, stage="exploring"),
        history=[],
        facts=CommerceFacts(has_products=True, orderable=True, product_count=3),
    )


def _search_miss_result(*, message: str = "no_search_hits_no_top_fallback") -> ActionResult:
    return ActionResult(
        success=False,
        error="no_search_hits",
        data={"message": message},
    )


def _compose_search_miss(
    message: str,
    *,
    query: str = "",
    intent_name: str = "ask_product",
    result: ActionResult | None = None,
) -> tuple[str, dict]:
    ctx = _ctx(message, intent_name=intent_name)
    composer = DefaultComposer()
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": query or message},
        reason="test catalog miss",
    )
    action_result = result or _search_miss_result()
    text = asyncio.run(composer.compose(decision, action_result, ctx))
    return text, dict(action_result.data or {})


class TestCatalogMissNeverCallsLlm:
    def test_weak_subject_does_not_call_llm_compose(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم الرياض", intent_name="ask_price")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "رياض"},
            reason="test",
        )
        result = _search_miss_result()

        with patch.object(composer, "_llm_compose", new_callable=AsyncMock) as mock_llm:
            text = asyncio.run(composer.compose(decision, result, ctx))
        mock_llm.assert_not_awaited()
        assert "الكتالوج" in text

    def test_catalog_like_miss_calls_persona_compose_when_subject_resolved(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم سدر الحجاز", intent_name="ask_price")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "سدر الحجاز"},
            reason="test",
        )
        result = _search_miss_result()

        async def _stub_llm(_bundle):
            return "ما لقيت تطابقاً واضحاً لسدر الحجاز في الكتالوج حالياً."

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_search_miss_answer",
                new=AsyncMock(
                    return_value=(
                        "ما لقيت تطابقاً واضحاً لسدر الحجاز في الكتالوج حالياً.",
                        None,
                        {
                            "chosen_path": "catalog_miss_resolved_subject",
                            "persona_compose": {"source": "persona_llm"},
                            "compose_source": "persona_llm",
                            "response_mode": "grounded_persona_compose",
                            "llm_candidate_present": True,
                            "final_text_transformed": False,
                            "final_transform_reasons": [],
                        },
                    ),
                ),
            ):
                ctx.merchant_context = {
                    "ai_settings": {
                        "persona_composer_enabled": True,
                        "store_ai_mode": "test",
                        "ai_test_allowed_numbers": ["966500000001"],
                        "persona_composer_surfaces": list(PERSONA_COMPOSER_SURFACES),
                    },
                }
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert result.data.get("chosen_path") == "catalog_miss_resolved_subject"
        assert result.data.get("persona_compose", {}).get("source") == "persona_llm"
        assert "الكتالوج" in text

    def test_no_synced_products_does_not_call_llm_compose(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("عندكم عسل؟")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "عسل"},
            reason="test",
        )
        result = ActionResult(
            success=False,
            error="no_products",
            data={"message": "no_products_in_catalog"},
        )

        with patch.object(composer, "_llm_compose", new_callable=AsyncMock) as mock_llm:
            text = asyncio.run(composer.compose(decision, result, ctx))
        mock_llm.assert_not_awaited()
        assert "متزامنة" in text or "الكتالوج" in text


class TestCatalogMissDeterministicTemplates:
    def test_returns_safe_template_on_miss(self) -> None:
        text, data = _compose_search_miss("بكم الرياض", query="رياض")
        assert "الكتالوج" in text
        assert data.get("chosen_path") in {
            CATALOG_MISS_CHOSEN_PATH,
            "catalog_miss_resolved_subject",
        }

    def test_no_synced_template(self) -> None:
        reply = compose_catalog_miss_deterministic_reply(no_synced_products=True)
        assert "متزامنة" in reply
        assert "LLM" not in reply

    def test_chosen_path_is_not_llm_fallback(self) -> None:
        _, data = _compose_search_miss("هل عندكم فستان؟", query="فستان")
        assert data.get("chosen_path") != "catalog_miss_llm_fallback"
        assert data.get("chosen_path") == CATALOG_MISS_CHOSEN_PATH


class TestCatalogMissContentSafety:
    @pytest.mark.parametrize(
        "message,query",
        [
            ("بكم الرياض", "رياض"),
            ("عندكم شي؟", "شي"),
            ("بكم سدر الحجاز", "سدر الحجاز"),
        ],
    )
    def test_no_prices_or_recommendations_after_miss(
        self,
        message: str,
        query: str,
    ) -> None:
        text, _ = _compose_search_miss(message, query=query)
        lowered = text.lower()
        assert "ريال" not in lowered
        assert "sar" not in lowered
        for banned in ("أنصحك", "الأفضل", "أنصح"):
            assert banned not in text

    def test_catalog_hit_path_unaffected(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم الطلح")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "طلح"},
            reason="test",
        )
        result = ActionResult(
            success=True,
            data={
                "products": [
                    {
                        "id": 1,
                        "title": "عسل طلح",
                        "price": 120,
                        "can_checkout": True,
                        "external_id": "ext-1",
                    }
                ],
            },
        )

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                new=AsyncMock(
                    return_value=(
                        "عسل طلح سعره 120 ريال.",
                        None,
                        {
                            "compose_source": "persona_llm",
                            "persona_compose": {"source": "persona_llm"},
                            "question_kind": "price",
                        },
                    ),
                ),
            ):
                with patch.object(
                    composer,
                    "_llm_compose",
                    new_callable=AsyncMock,
                ) as mock_llm:
                    text = await composer.compose(decision, result, ctx)
            mock_llm.assert_not_awaited()
            return text

        text = asyncio.run(_run())
        assert "طلح" in text or "عسل" in text

    def test_variant_pricing_path_unaffected(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("بكم الطلح")
        decision = Decision(
            action=ACTION_VARIANT_PRICING,
            args={"reply_text": "سعر الطلح: 120 ريال"},
            reason="variant bound",
        )
        result = ActionResult(
            success=True,
            data={"reply_text": "سعر الطلح: 120 ريال"},
        )
        text = asyncio.run(composer.compose(decision, result, ctx))
        assert "120" in text

    def test_non_commerce_intent_does_not_enter_catalog_miss(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("السلام عليكم", intent_name="greeting")
        decision = Decision(
            action=ACTION_SOCIAL_REPLY,
            args={"topic": "greeting", "social_category": "greeting"},
            reason="social",
        )
        result = ActionResult(success=True, data={})

        text = asyncio.run(composer.compose(decision, result, ctx))
        assert text.strip()
        assert result.data.get("chosen_path") != CATALOG_MISS_CHOSEN_PATH


class TestProductClaimGroundingDefenseLayer:
    def test_grounding_guard_still_detects_catalog_miss_path(self) -> None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            build_product_claim_grounding_evidence,
        )

        evidence = build_product_claim_grounding_evidence(
            None,
            7,
            chosen_path=CATALOG_MISS_CHOSEN_PATH,
            executor_products=[],
        )
        assert evidence.catalog_miss_this_turn is True
