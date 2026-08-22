"""Runtime propagation of catalog_fact_products to product_claim_grounding_guard."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_presentation_selection import PRESENTATION_NONE  # noqa: E402
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
        "compose_source": "persona_llm",
        "response_mode": "grounded_persona_compose",
        "llm_candidate_present": True,
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
    def test_responder_persists_compose_products_before_catalog_compose(self) -> None:
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
        seen_before_compose: dict[str, Any] = {}

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

            async def _capture_persisted_facts(**_kwargs: Any) -> tuple[str, PersonaComposeResult, Dict[str, Any]]:
                seen_before_compose["fact_len"] = len(
                    result.data.get("catalog_fact_products") or []
                )
                seen_before_compose["fact_ids"] = {
                    int(row["id"])
                    for row in (result.data.get("catalog_fact_products") or [])
                    if isinstance(row, dict) and row.get("id") is not None
                }
                seen_before_compose["pending_buttons"] = result.data.get("pending_buttons")
                seen_before_compose["pending_candidates"] = result.data.get("pending_candidates")
                return compose_result.text, compose_result, event

            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(side_effect=_capture_persisted_facts),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda products, **_kw: list(products),
                ):
                    text = await composer.compose(decision, result, ctx)

            assert "387" in text
            assert seen_before_compose["fact_len"] > 0
            assert 109 in seen_before_compose["fact_ids"]
            assert seen_before_compose["pending_buttons"] is None
            assert seen_before_compose["pending_candidates"] is None
            facts = list(result.data.get("catalog_fact_products") or [])
            assert len(facts) > 0
            assert 109 in {int(row["id"]) for row in facts if isinstance(row, dict)}

        asyncio.run(_run())

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


_JACKET_PRODUCT = {
    "id": 28,
    "title": "جاكيت",
    "category": "ملابس",
    "price": 169,
    "can_checkout": True,
    "in_stock": True,
}


def _gate_off_search_ctx(*, message: str, tenant_id: int = 1) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500009429",
        customer_id=1,
        conversation_id=9739,
        message=message,
        intent=Intent(name="ask_price", confidence=0.9, raw_message=message),
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(has_products=True, orderable=True, product_count=1),
        merchant_context={"ai_settings": {"persona_composer_enabled": False}},
    )


class TestResponderCatalogQaPersonaCompose:
    def test_price_qa_uses_persona_compose_not_deterministic_bypass(self) -> None:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        async def _run() -> None:
            ctx = _gate_off_search_ctx(message="كم سعر جاكيت؟")
            result = ActionResult(
                success=True,
                data={
                    "products": [_JACKET_PRODUCT],
                    "query": "جاكيت",
                    "count": 1,
                },
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "جاكيت", "source": "search"},
                reason="test jacket price gate off",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(
                    return_value=(
                        "جاكيت سعره 169 ريال.",
                        PersonaComposeResult(
                            text="جاكيت سعره 169 ريال.",
                            source="persona_llm",
                            surface="catalog_product_answer",
                            facts_hash="facts",
                            guard_passed=True,
                        ),
                        {
                            "compose_source": "persona_llm",
                            "response_mode": "grounded_persona_compose",
                            "persona_compose": {"source": "persona_llm"},
                            "question_kind": "price",
                            "price_source": "catalog",
                            "catalog_product_ids": [28],
                        },
                    ),
                ),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda products, **_kw: list(products),
                ):
                    text = await composer.compose(decision, result, ctx)

            assert "169" in text
            assert "جاكيت" in text
            assert "اختر رقم" not in text
            assert result.data.get("compose_source") == "persona_llm"
            assert result.data.get("persona_compose", {}).get("source") == "persona_llm"
            assert "catalog_deterministic_fallback" not in str(
                result.data.get("persona_compose", {}).get("source")
            )
            assert result.data.get("pending_buttons") in (None, [])
            # Single resolved price Q&A may stamp a rich card; never pick_N.
            assert not result.data.get("pending_buttons")
            cards = result.data.get("pending_product_cards") or []
            if cards:
                assert cards[0].get("kind") == "product_card"
                assert cards[0].get("title") == "جاكيت"

        asyncio.run(_run())

    def test_browse_search_still_allows_narrow_choices(self) -> None:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        async def _run() -> None:
            ctx = _gate_off_search_ctx(message="أبغى جاكيت", tenant_id=1)
            result = ActionResult(
                success=True,
                data={
                    "products": [_JACKET_PRODUCT],
                    "query": "جاكيت",
                    "count": 1,
                },
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "جاكيت", "source": "search"},
                reason="test jacket browse",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(return_value=(None, None, None)),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda products, **_kw: list(products),
                ):
                    text = await composer.compose(decision, result, ctx)

            # Ungrounded ranked singleton browse → no rich card. Brain/candidates remain.
            assert (text or "").strip()
            assert result.data.get("pending_buttons") in (None, [])
            assert not result.data.get("pending_product_cards")
            assert result.data.get("product_presentation_kind") in (
                None,
                PRESENTATION_NONE,
            )
            assert result.data.get("pending_candidates")

        asyncio.run(_run())

    def test_talh_price_gate_off_uses_persona_compose(self) -> None:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        talh_rows = [_TALH_1KG, _TALH_5KG]

        async def _run() -> None:
            ctx = _gate_off_search_ctx(message="كم سعر الطلح؟", tenant_id=33)
            result = ActionResult(
                success=True,
                data={
                    "products": [],
                    "catalog_fact_products": list(talh_rows),
                    "query": "طلح",
                    "count": 2,
                },
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "طلح", "source": "search"},
                reason="test talh price gate off",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(
                    return_value=(
                        "عسل الطلح متوفر بسعرين في الكتالوج.",
                        PersonaComposeResult(
                            text="عسل الطلح متوفر بسعرين في الكتالوج.",
                            source="persona_llm",
                            surface="catalog_product_answer",
                            facts_hash="facts",
                            guard_passed=True,
                        ),
                        {
                            "compose_source": "persona_llm",
                            "question_kind": "price",
                            "price_source": "catalog",
                            "catalog_product_ids": [109, 121],
                            "persona_compose": {"source": "persona_llm"},
                        },
                    ),
                ),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda products, **_kw: list(products),
                ):
                    text = await composer.compose(decision, result, ctx)

            assert text
            assert result.data.get("compose_source") == "persona_llm"
            assert result.data.get("question_kind") == "price"
            ids = set(result.data.get("catalog_product_ids") or [])
            assert 109 in ids
            assert 121 in ids

        asyncio.run(_run())


class TestPipelineGuardCatalogPriceFacts:
    def test_pipeline_rebuild_finds_side_channel_facts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Side-channel catalog_fact_products on result.data grounds price 387."""
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
        result_data: Dict[str, Any] = {
            **_catalog_compose_event_without_fact_rows(),
            "catalog_fact_products": [_TALH_1KG, _TALH_5KG],
            "products": [],
        }
        reply = (
            "من الكتالوج:\n"
            "• عسل طلح نجد البري إنتاج منحلنا  1 كيلو سعره 387 ريال، "
            "والمنتج غير متاح للطلب حالياً"
        )
        guard = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=list(result_data.get("catalog_fact_products") or []),
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

    def test_brain_result_exports_zero_len_diagnostics_when_rows_absent(self) -> None:
        from modules.ai.brain.pipeline import _catalog_fact_guard_diagnostics  # noqa: PLC0415

        result_data = {
            "catalog_product_ids": [109, 121],
            "catalog_fact_products": [],
        }
        diag = _catalog_fact_guard_diagnostics(result_data)
        brain_result = {
            "catalog_product_ids": list(result_data["catalog_product_ids"]),
            **diag,
        }
        assert "catalog_fact_products_len" in brain_result
        assert brain_result["catalog_fact_products_len"] == 0
        assert brain_result["catalog_fact_product_ids"] == [109, 121]
        assert brain_result["catalog_fact_price_values"] == []


class TestCatalogPriceGuardDbFallback:
    def _catalog_price_result_data(self) -> Dict[str, Any]:
        return {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
                "guard_passed": True,
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109, 121],
            "checkout_pressure_allowed": False,
            "catalog_fact_products": [],
            "products": [],
        }

    def test_pipeline_guard_rebuild_empty_pools_triggers_db_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415
        from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: PLC0415
            apply_product_claim_grounding_guard,
        )

        db_rows = [
            {
                "id": 109,
                "title": _TALH_1KG["title"],
                "price": 387,
                "can_checkout": False,
            },
            {
                "id": 121,
                "title": _TALH_5KG["title"],
                "price": 1475,
                "can_checkout": False,
            },
        ]
        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: list(db_rows),
        )
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        result_data = self._catalog_price_result_data()
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        assert resolved
        assert result_data["catalog_fact_rebuild_source"] == "db_by_catalog_product_ids"
        assert 387 in pl._catalog_fact_guard_diagnostics(result_data)["catalog_fact_price_values"]

        reply = "عسل طلح نجد البري إنتاج منحلنا  1 كيلو سعره 387 ريال"
        guard = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=resolved,
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109, 121],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "persona_llm",
                },
            },
        )
        assert guard.replaced is False
        assert "387" in guard.reply
        assert "ما ظهر عندي سعر مؤكد" not in guard.reply

    def test_pipeline_guard_db_fallback_skipped_without_catalog_price_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        calls: list[Any] = []

        def _db_stub(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            calls.append(True)
            return [{"id": 109, "price": 387, "can_checkout": False}]

        monkeypatch.setattr(pl, "_rebuild_catalog_price_guard_fact_rows_from_db", _db_stub)

        result_data = {
            "persona_compose": {
                "surface": "kb_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "features",
            "catalog_product_ids": [109],
            "catalog_fact_products": [],
            "products": [],
        }
        pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        assert calls == []
        assert "catalog_fact_rebuild_source" not in result_data

    def test_pipeline_guard_db_fallback_blocks_ungrounded_reply_price(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415
        from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: PLC0415
            apply_product_claim_grounding_guard,
        )

        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: [
                {"id": 109, "title": _TALH_1KG["title"], "price": 387, "can_checkout": False},
            ],
        )
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        result_data = self._catalog_price_result_data()
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        guard = apply_product_claim_grounding_guard(
            reply="سعر الطلح 999 ريال",
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=resolved,
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109, 121],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "persona_llm",
                },
            },
        )
        assert guard.replaced is True
        assert guard.stripped is True
        assert "999" not in guard.reply
        assert "ما ظهر عندي سعر مؤكد" not in guard.reply
        assert guard.scrubbed_empty is True
        assert guard.requires_grounded_recompose is True

    def test_brain_result_exports_db_rebuild_diagnostics(self) -> None:
        from modules.ai.brain.pipeline import _catalog_fact_guard_diagnostics  # noqa: PLC0415

        result_data = {
            "catalog_product_ids": [109, 121],
            "catalog_fact_products": [
                {"id": 109, "price": 387, "can_checkout": False},
            ],
            "catalog_fact_rebuild_source": "db_by_catalog_product_ids",
        }
        diag = _catalog_fact_guard_diagnostics(result_data)
        brain_result = {
            "catalog_product_ids": list(result_data["catalog_product_ids"]),
            **diag,
        }
        assert brain_result["catalog_fact_products_len"] == 1
        assert brain_result["catalog_fact_product_ids"] == [109]
        assert brain_result["catalog_fact_price_values"] == [387]
        assert brain_result["catalog_fact_rebuild_source"] == "db_by_catalog_product_ids"


class TestCatalogPriceGuardDbImportPath:
    def test_db_rebuild_uses_models_product_import_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        from models import Product  # noqa: PLC0415

        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        product = MagicMock()
        product.id = 109
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [product]

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder._format",
            lambda _self, _p: {
                "id": 109,
                "title": _TALH_1KG["title"],
                "price": 387,
                "can_checkout": False,
            },
        )

        rows = pl._rebuild_catalog_price_guard_fact_rows_from_db(
            db,
            tenant_id=33,
            catalog_product_ids=[109, 121],
        )
        assert len(rows) == 1
        assert rows[0]["id"] == 109
        assert rows[0]["price"] == 387
        assert db.query.call_args[0][0] is Product

    def test_db_rebuild_import_failure_returns_empty_without_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import builtins

        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        real_import = builtins.__import__

        def _fail_models_import(name: str, *args: Any, **kwargs: Any):
            if name == "models":
                raise ImportError("simulated_models_import_failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_models_import)

        with caplog.at_level("WARNING"):
            rows = pl._rebuild_catalog_price_guard_fact_rows_from_db(
                object(),
                tenant_id=33,
                catalog_product_ids=[109, 121],
            )

        assert rows == []
        joined = " ".join(caplog.messages)
        assert "_rebuild_catalog_price_guard_fact_rows_from_db" in joined
        assert "stage=import_product_model" in joined
        assert "tenant_id=33" in joined
        assert "catalog_product_ids_count=2" in joined
        assert "ImportError" in joined


class TestCatalogPriceGuardFieldExtraction:
    def test_guard_fact_row_resolves_sale_price_when_price_null(self) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        formatted = {
            "id": 109,
            "title": _TALH_1KG["title"],
            "price": None,
            "sale_price": "ر.س. ٣٨٧٫٠٠",
            "regular_price": None,
            "can_checkout": False,
        }
        row = pl._catalog_guard_fact_row_from_product(formatted, pid_int=109)
        assert row is not None
        assert row["id"] == 109
        assert pl._catalog_guard_fact_rows_have_grounded_price([row]) is True
        assert 387 in pl._catalog_fact_guard_diagnostics(
            {"catalog_fact_products": [row]},
        )["catalog_fact_price_values"]

    def test_guard_fact_row_resolves_regular_price_when_price_and_sale_null(self) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        formatted = {
            "id": 121,
            "title": _TALH_5KG["title"],
            "price": None,
            "sale_price": None,
            "regular_price": "١٬٤٧٥٫٠٠",
            "can_checkout": False,
        }
        row = pl._catalog_guard_fact_row_from_product(formatted, pid_int=121)
        assert row is not None
        assert row["id"] == 121
        assert pl._catalog_guard_fact_rows_have_grounded_price([row]) is True
        assert 1475 in pl._catalog_fact_guard_diagnostics(
            {"catalog_fact_products": [row]},
        )["catalog_fact_price_values"]

    def test_db_rebuild_survives_sale_price_only_format_dict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        from models import Product  # noqa: PLC0415

        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        product = MagicMock()
        product.id = 109
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [product]

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder._format",
            lambda _self, _p: {
                "id": 109,
                "title": _TALH_1KG["title"],
                "price": None,
                "sale_price": 387,
                "regular_price": None,
                "can_checkout": False,
            },
        )

        rows = pl._rebuild_catalog_price_guard_fact_rows_from_db(
            db,
            tenant_id=33,
            catalog_product_ids=[109],
        )
        assert len(rows) == 1
        assert rows[0]["price"] == 387
        assert 387 in pl._catalog_fact_guard_diagnostics(
            {"catalog_fact_products": rows},
        )["catalog_fact_price_values"]
        assert db.query.call_args[0][0] is Product

    def test_db_fallback_sale_price_grounds_reply_not_rewritten(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415
        from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: PLC0415
            apply_product_claim_grounding_guard,
        )

        db_rows = [
            {
                "id": 109,
                "title": _TALH_1KG["title"],
                "price": "ر.س. ٣٨٧٫٠٠",
                "can_checkout": False,
            },
        ]
        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: list(db_rows),
        )
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        result_data = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
                "guard_passed": True,
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109, 121],
            "checkout_pressure_allowed": False,
            "catalog_fact_products": [],
            "products": [],
        }
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        guard = apply_product_claim_grounding_guard(
            reply="عسل طلح نجد البري سعره 387 ريال",
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=resolved,
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109, 121],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "persona_llm",
                },
            },
        )
        assert guard.replaced is False
        assert "387" in guard.reply
        assert "ما ظهر عندي سعر مؤكد" not in guard.reply

    def test_db_fallback_still_rewrites_ungrounded_999(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415
        from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: PLC0415
            apply_product_claim_grounding_guard,
        )

        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: [
                {
                    "id": 109,
                    "title": _TALH_1KG["title"],
                    "price": "ر.س. ٣٨٧٫٠٠",
                    "can_checkout": False,
                },
            ],
        )
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        result_data = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109],
            "checkout_pressure_allowed": False,
            "catalog_fact_products": [],
            "products": [],
        }
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        guard = apply_product_claim_grounding_guard(
            reply="سعر الطلح 999 ريال",
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            executor_products=[],
            catalog_fact_products=resolved,
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "persona_llm",
                },
            },
        )
        assert guard.replaced is True
        assert guard.stripped is True
        assert "999" not in guard.reply
        assert "ما ظهر عندي سعر مؤكد" not in guard.reply
        assert guard.scrubbed_empty is True
        assert guard.requires_grounded_recompose is True

    def test_guard_context_accepts_string_false_checkout_pressure(self) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        base = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109, 121],
            "catalog_fact_products": [],
        }
        for value in (False, "false", "False", 0):
            data = dict(base)
            data["checkout_pressure_allowed"] = value
            assert pl._is_catalog_product_price_guard_context(data) is True
        for value in (True, None, "true", 1):
            data = dict(base)
            data["checkout_pressure_allowed"] = value
            assert pl._is_catalog_product_price_guard_context(data) is False


class TestCatalogPriceGuardStaleRowHandoff:
    def test_resolve_returns_empty_when_pool_and_db_lack_grounded_prices(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: [],
        )

        result_data = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109, 121],
            "checkout_pressure_allowed": False,
            "catalog_fact_products": [
                {"id": 109, "title": _TALH_1KG["title"], "can_checkout": False},
            ],
            "products": [{"id": 121, "title": _TALH_5KG["title"], "can_checkout": False}],
        }
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        assert resolved == []
        assert result_data["catalog_fact_products"] == []
        assert result_data.get("catalog_fact_rebuild_source") is None
        diag = pl._catalog_fact_guard_diagnostics(result_data)
        assert diag["catalog_fact_products_len"] == 0
        assert diag["catalog_fact_price_values"] == []
        assert 109 in diag["catalog_fact_product_ids"]

    def test_db_rebuild_regular_price_only_format_dict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        from models import Product  # noqa: PLC0415

        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        product = MagicMock()
        product.id = 121
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [product]

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder._format",
            lambda _self, _p: {
                "id": 121,
                "title": _TALH_5KG["title"],
                "price": None,
                "sale_price": None,
                "regular_price": 1475,
                "can_checkout": False,
            },
        )

        rows = pl._rebuild_catalog_price_guard_fact_rows_from_db(
            db,
            tenant_id=33,
            catalog_product_ids=[121],
        )
        assert len(rows) == 1
        assert rows[0]["price"] == 1475
        assert 1475 in pl._catalog_fact_guard_diagnostics(
            {"catalog_fact_products": rows},
        )["catalog_fact_price_values"]

    def test_resolve_diagnostics_include_rebuild_source_after_db_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        monkeypatch.setattr(
            pl,
            "_rebuild_catalog_price_guard_fact_rows_from_db",
            lambda *_args, **_kwargs: [
                {
                    "id": 109,
                    "title": _TALH_1KG["title"],
                    "price": 387,
                    "can_checkout": False,
                },
            ],
        )

        result_data = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "price",
            "price_source": "catalog",
            "catalog_product_ids": [109],
            "checkout_pressure_allowed": False,
            "catalog_fact_products": [],
            "products": [],
        }
        resolved = pl._resolve_catalog_price_guard_fact_rows(
            result_data,
            db=object(),
            tenant_id=33,
        )
        assert len(resolved) == 1
        assert result_data["catalog_fact_rebuild_source"] == "db_by_catalog_product_ids"
        diag = pl._catalog_fact_guard_diagnostics(result_data)
        assert diag["catalog_fact_products_len"] == 1
        assert diag["catalog_fact_rebuild_source"] == "db_by_catalog_product_ids"
        assert 387 in diag["catalog_fact_price_values"]
        assert 109 in diag["catalog_fact_product_ids"]


class TestCatalogCompoundProvenancePipeline:
    def test_guard_context_accepts_compound_requested_facets(self) -> None:
        from modules.ai.brain import pipeline as pl  # noqa: PLC0415

        data = {
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "compound",
            "requested_facets": ["price", "availability"],
            "price_source": "catalog",
            "catalog_product_ids": [501],
            "checkout_pressure_allowed": False,
        }
        assert pl._is_catalog_product_price_guard_context(data) is True

    def test_live_pipeline_records_service_closer_llm_provenance(self) -> None:
        from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415
        from tests.commerce_scenario_fixtures import (  # noqa: PLC0415
            make_scenario_db,
            seed_conversation,
            seed_customer,
            seed_tenant,
        )

        session, _engine = make_scenario_db()
        tenant = seed_tenant(session, name="متجر تجريبي عام")
        customer = seed_customer(session, tenant.id, name="أحمد سالم")
        conversation = seed_conversation(
            session,
            tenant.id,
            customer_id=customer.id,
        )
        brain = get_brain()
        message = "كم سعر حذاء رياضي أبيض وهل هو متوفر؟"
        candidate = (
            "حذاء رياضي أبيض سعره 220 ريال وهو متوفر للطلب. "
            "كيف أقدر أساعدك اليوم؟"
        )
        product = {
            "id": 501,
            "title": "حذاء رياضي أبيض",
            "price": 220,
            "can_checkout": True,
            "in_stock": True,
        }
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "حذاء رياضي أبيض"},
        )
        action_result = ActionResult(
            success=True,
            data={
                "query": "حذاء رياضي أبيض",
                "products": [product],
                "catalog_fact_products": [product],
            },
        )

        stack = ExitStack()
        stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
        stack.enter_context(
            patch(
                "core.wa_usage.check_limit",
                return_value=SimpleNamespace(
                    allowed=True,
                    used_total=0,
                    limit=1000,
                    reason="",
                ),
            )
        )
        stack.enter_context(
            patch(
                "core.ai_disabled_gate.is_ai_disabled_for_conversation",
                return_value=SimpleNamespace(disabled=False, reason=None),
            )
        )
        stack.enter_context(
            patch("core.store_knowledge.build_merchant_context", return_value={})
        )
        stack.enter_context(
            patch.object(
                brain._classifier,
                "classify",
                return_value=Intent(
                    name="ask_price",
                    confidence=0.95,
                    raw_message=message,
                ),
            )
        )
        stack.enter_context(
            patch.object(brain._decision_engine, "decide", return_value=decision)
        )
        stack.enter_context(
            patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d)
        )
        stack.enter_context(
            patch.object(
                brain._state_store,
                "load",
                return_value=MerchantConversationState(
                    stage="exploring",
                    greeted=True,
                ),
            )
        )
        stack.enter_context(patch.object(brain._state_store, "save"))
        stack.enter_context(
            patch.object(
                brain._facts_loader,
                "load",
                return_value=CommerceFacts(
                    store_name="متجر تجريبي عام",
                    has_products=True,
                    product_count=1,
                    in_stock_count=1,
                    orderable=True,
                ),
            )
        )
        stack.enter_context(patch.object(brain._memory_updater, "update"))
        stack.enter_context(
            patch.object(
                brain._executor,
                "execute",
                new=AsyncMock(return_value=action_result),
            )
        )
        compose_call = stack.enter_context(
            patch(
                "modules.ai.brain.persona.catalog_product_answer."
                "try_compose_catalog_product_answer",
                new=AsyncMock(
                    return_value=(
                        candidate,
                        None,
                        {
                            "chosen_path": "fact_bound_persona_compose",
                            "persona_compose": {
                                "source": "persona_llm",
                                "surface": "catalog_product_answer",
                            },
                            "compose_source": "persona_llm",
                            "response_mode": "grounded_persona_compose",
                            "llm_candidate_present": True,
                            "final_text_transformed": False,
                            "final_transform_reasons": [],
                            "final_customer_text_source": "persona_llm",
                            "question_kind": "compound",
                            "requested_facets": ["price", "availability"],
                            "price_source": "catalog",
                            "availability_source": "catalog",
                            "checkout_pressure_allowed": False,
                            "catalog_product_ids": [501],
                            "catalog_fact_products": [product],
                        },
                    )
                ),
            )
        )

        async def _run() -> dict[str, Any]:
            with stack:
                return await brain.process(
                    db=session,
                    tenant_id=tenant.id,
                    customer_phone="966500000001",
                    message=message,
                    history=[],
                    profile={
                        "id": customer.id,
                        "name": "أحمد سالم",
                        "preferred_language": "ar",
                    },
                    customer_id=customer.id,
                    conversation_id=conversation.id,
                )

        try:
            output = asyncio.run(_run())
        finally:
            session.close()

        compose_call.assert_awaited_once()
        assert "كيف أقدر أساعدك اليوم" not in output["reply"]
        assert "220" in output["reply"]
        assert output["compose_source"] == "persona_llm"
        assert output["llm_candidate_present"] is True
        assert output["final_text_transformed"] is True
        assert "service_closer_guard" in output["final_transform_reasons"]
        assert output["final_customer_text_source"] == "persona_llm_postprocess"
        assert output["decision_action"] == ACTION_SEARCH_PRODUCTS
        assert output["persona_ownership"]["expression_owner"] == "persona_llm"
        assert output["persona_ownership"]["bypass_reason"] is None

    def test_persona_llm_compose_overrides_template_search_products_owner(self) -> None:
        from modules.ai.brain.persona_ownership import build_brain_persona_ownership  # noqa: PLC0415
        from modules.ai.compose.reply_metadata_export import approved_compose_source  # noqa: PLC0415

        result_data = {
            "compose_source": "persona_llm",
            "question_kind": "compound",
            "requested_facets": ["price", "availability"],
        }
        ownership = build_brain_persona_ownership(
            decision_action="search_products",
            decision_args={"query": "حذاء رياضي أبيض"},
            reply_state=None,
            chosen_path="fact_bound_persona_compose",
            guard_replaced={},
        )
        if approved_compose_source(result_data.get("compose_source")) == "persona_llm":
            ownership.stamp_persona(
                topic=str(result_data.get("question_kind") or "catalog_product_answer"),
                kind="grounded_persona_compose",
                owner="persona_llm",
            )
        payload = ownership.to_dict()
        assert payload["persona_stamped"] is True
        assert payload["expression_owner"] == "persona_llm"
        assert payload["bypass_reason"] is None
