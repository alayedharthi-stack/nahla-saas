"""Runtime propagation of catalog_fact_products to product_claim_grounding_guard."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


_TALH_1KG = {
    "id": 109,
    "title": "عسل طلح نجد البري إنتاج منحلنا  1 كيلو",
    "price": "ر.س. ٣٨٧٫٠٠",
    "can_checkout": False,
    "in_stock": False,
}
_TALH_5KG = {
    "id": 121,
    "title": "عسل طلح نجد البري إنتاج منحلنا  5 كيلو",
    "price": "ر.س. ١٬٤٧٥٫٠٠",
    "can_checkout": False,
    "in_stock": False,
}


def _catalog_compose_event_without_fact_rows() -> Dict[str, Any]:
    return {
        "chosen_path": "fact_bound_persona_compose",
        "persona_compose": {
            "surface": "catalog_product_answer",
            "source": "persona_llm",
            "guard_passed": True,
        },
        "question_kind": "price",
        "catalog_product_ids": [109, 121],
        "price_source": "catalog",
        "checkout_pressure_allowed": False,
    }


def _minimal_search_ctx(*, message: str = "كم سعر الطلح؟") -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        customer_id=1,
        conversation_id=56,
        message=message,
        intent=Intent(name="ask_price", confidence=0.9, raw_message=message),
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(has_products=True, orderable=True, product_count=2),
        merchant_context={"ai_settings": {"persona_composer_enabled": True}},
    )


class TestResponderCatalogPriceFactPersist:
    def test_responder_catalog_price_persists_fact_rows_on_action_result(self) -> None:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        talh_rows = [_TALH_1KG]
        compose_result = PersonaComposeResult(
            text="عسل طلح نجد البري سعره 387 ريال",
            source="persona_llm",
            surface="catalog_product_answer",
            facts_hash="talh-price",
            guard_passed=True,
        )
        event = _catalog_compose_event_without_fact_rows()

        async def _fake_try_compose(**_kwargs: Any) -> tuple[str, PersonaComposeResult, Dict[str, Any]]:
            return compose_result.text, compose_result, event

        async def _run() -> None:
            ctx = _minimal_search_ctx()
            result = ActionResult(
                success=True,
                data={
                    "products": [],
                    "catalog_fact_products": list(talh_rows),
                    "query": "طلح",
                },
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "طلح", "source": "search"},
                reason="test talh price",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(side_effect=_fake_try_compose),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda products, **_kw: list(products),
                ):
                    text = await composer.compose(decision, result, ctx)

            assert "387" in text
            facts = list(result.data.get("catalog_fact_products") or [])
            assert len(facts) > 0
            fact_ids = {int(row["id"]) for row in facts if isinstance(row, dict)}
            assert 109 in fact_ids
            assert any(row.get("price") for row in facts if isinstance(row, dict))

        asyncio.run(_run())


class TestPipelineGuardCatalogPriceFacts:
    def test_pipeline_guard_receives_catalog_facts_for_price_qa(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.pipeline import (  # noqa: PLC0415
            _catalog_price_guard_needs_fact_rebuild,
            _rebuild_catalog_price_guard_fact_rows,
        )

        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
        result_data: Dict[str, Any] = {
            **_catalog_compose_event_without_fact_rows(),
            "catalog_fact_products": [],
            "products": [],
        }
        side_channel = [_TALH_1KG, _TALH_5KG]
        result_data["catalog_fact_products"] = []
        # Executor side channel present on result.data but cleared before guard (prod-shaped).
        rebuild_source = dict(result_data)
        rebuild_source["catalog_fact_products"] = list(side_channel)
        assert _catalog_price_guard_needs_fact_rebuild(result_data) is True

        rebuilt = _rebuild_catalog_price_guard_fact_rows(
            rebuild_source,
            catalog_product_ids=[109, 121],
        )
        assert len(rebuilt) >= 1
        result_data["catalog_fact_products"] = rebuilt

        reply = (
            "من الكتالوج:\n"
            "• عسل طلح نجد البري إنتاج منحلنا  1 كيلو سعره 387 ريال، "
            "والمنتج غير متاح للطلب حالياً"
        )
        history: List[Dict[str, Any]] = [
            {
                "direction": "outbound",
                "body": "ما ظهر عندي في الكتالوج منتجات مطابقة لطلبك.",
            },
        ]
        guard = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=list(result_data.get("catalog_fact_products") or []),
            history=history,
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109, 121],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "persona_llm",
                    "guard_passed": True,
                },
            },
        )
        assert guard.replaced is False
        assert "387" in guard.reply or "٣٨٧" in guard.reply
        assert "ما ظهر عندي سعر مؤكد" not in guard.reply

    def test_rebuild_from_products_when_side_channel_missing(self) -> None:
        from modules.ai.brain.pipeline import _rebuild_catalog_price_guard_fact_rows  # noqa: PLC0415

        rebuilt = _rebuild_catalog_price_guard_fact_rows(
            {
                "catalog_fact_products": [],
                "products": [_TALH_1KG],
            },
            catalog_product_ids=[109],
        )
        assert len(rebuilt) == 1
        assert rebuilt[0]["id"] == 109
        assert rebuilt[0]["price"] == _TALH_1KG["price"]


class TestBrainResultCatalogFactDiagnostics:
    def test_brain_result_exports_catalog_fact_diagnostics(self) -> None:
        from modules.ai.brain.pipeline import _catalog_fact_guard_diagnostics  # noqa: PLC0415

        result_data = {
            "catalog_product_ids": [109, 121],
            "catalog_fact_products": [_TALH_1KG, _TALH_5KG],
        }
        diag = _catalog_fact_guard_diagnostics(result_data)
        brain_result = {
            "catalog_product_ids": list(result_data["catalog_product_ids"]),
            **diag,
        }
        assert brain_result["catalog_fact_products_len"] > 0
        assert 109 in brain_result["catalog_fact_product_ids"]
        assert 121 in brain_result["catalog_fact_product_ids"]
        assert 387 in brain_result["catalog_fact_price_values"]
        assert 1475 in brain_result["catalog_fact_price_values"]
