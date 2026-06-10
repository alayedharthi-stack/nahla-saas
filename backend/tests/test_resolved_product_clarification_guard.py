"""
P1-A — resolved product clarification guard tests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.ai.brain.clarification.resolved_product_guard import (
    apply_resolved_product_clarify_guard,
    compose_resolved_product_search_miss,
    extract_resolved_product_subject,
    extract_resolved_product_subject_from_message,
    is_product_identification_clarification,
    search_retry_queries,
)
from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.decision.actions import ACTION_CLARIFY, ACTION_SEARCH_PRODUCTS
from modules.ai.brain.execution.executor import _ClarifyHandler
from modules.ai.brain.intent import rules
from modules.ai.brain.product_discovery_gate import clarify_instead_of_top_products
from modules.ai.brain.types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(message: str, *, focus: dict | None = None) -> BrainContext:
    intent = rules.match(message)
    if intent is None:
        intent = Intent(name="ask_price", confidence=0.9, raw_message=message)
    state = MerchantConversationState(greeted=True, stage="discovery")
    if focus:
        state.current_product_focus = focus
    return BrainContext(
        tenant_id=42,
        customer_phone="966500000099",
        message=message,
        intent=intent,
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
    )


class TestResolvedProductSubject:
    @pytest.mark.parametrize(
        "message,expected_fragment",
        (
            ("عسل السمر بكم الكيلو", "السمر"),
            ("بكم يطلع الكيلو السمر", "السمر"),
            ("عسل الطلح بكم الكيلو", "الطلح"),
        ),
    )
    def test_extract_from_price_messages(self, message, expected_fragment):
        subject = extract_resolved_product_subject_from_message(message)
        assert expected_fragment in subject

    def test_extract_from_context_with_focus(self):
        ctx = _ctx("سعره سمح", focus={"title": "عسل سمر الحجاز", "id": 1})
        assert "سمر" in extract_resolved_product_subject(ctx)


class TestProductIdentificationDetection:
    @pytest.mark.parametrize(
        "text",
        (
            "أي نوع أو صفة تهمك بالضبط؟ مثلاً سدر، طلح، أو حجم معيّن",
            "تحب أعطيك الأسعار حسب النوع (سدر / طلح / ضهيان)؟",
            "تقصد حاجة أو مواصفة معيّنة؟ وضّح الاستخدام أو الصفة",
        ),
    )
    def test_detects_type_reopen_copy(self, text):
        assert is_product_identification_clarification(text)

    def test_allows_bare_price_clarify(self):
        assert not is_product_identification_clarification(
            "تقصد سعر كيلو أي منتج؟ اكتب اسم المنتج أو نوعه وأعطيك السعر."
        )


class TestClarifyGuard:
    def test_blocks_type_clarify_when_subject_resolved(self):
        ctx = _ctx("عسل السمر بكم الكيلو")
        bad = (
            "حاضر، بخصوص *عسل السمر* — أي نوع أو صفة تهمك بالضبط؟ "
            "مثلاً سدر، طلح، أو حجم معيّن — وأرشّح لك الأنسب."
        )
        out = apply_resolved_product_clarify_guard(ctx, bad, source="test")
        assert "مثلاً سدر" not in out
        assert "أي نوع" not in out
        assert "السمر" in out

    def test_passes_through_when_no_subject(self):
        ctx = _ctx("بكم")
        q = "تقصد سعر كيلو أي منتج؟"
        assert apply_resolved_product_clarify_guard(ctx, q, source="test") == q


class TestSearchRetryQueries:
    def test_yields_shorter_token_variants(self):
        alts = search_retry_queries("عسل السمر")
        assert any("سمر" in a for a in alts)


class TestSearchMissCompose:
    def test_responder_never_emits_sdr_tlh_on_search_miss(self):
        async def _run():
            composer = DefaultComposer()
            ctx = _ctx("عسل السمر بكم الكيلو")
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "عسل السمر"},
                reason="test",
                confidence=0.9,
            )
            result = ActionResult(
                success=False,
                error="no_search_hits",
                data={"message": "no_search_hits_no_top_fallback"},
            )
            return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert "مثلاً سدر" not in text
        assert "أي نوع" not in text
        assert "السمر" in text
        assert "الكتالوج" in text

    def test_search_miss_template_is_honest_failure(self):
        text = compose_resolved_product_search_miss("عسل السمر")
        assert "ما لقيت" in text or "ما ظهر" in text
        assert "مثلاً" not in text


class TestExecutorClarifyHandler:
    def test_executor_clarify_guarded(self):
        async def _run():
            handler = _ClarifyHandler()
            ctx = _ctx("عسل الطلح بكم")
            decision = Decision(
                action=ACTION_CLARIFY,
                args={
                    "question": (
                        "أي نوع أو صفة تهمك؟ مثلاً سدر، طلح، أو حجم معيّن"
                    ),
                    "query": "عسل الطلح",
                },
                reason="test",
                confidence=0.8,
            )
            return await handler.handle(decision, ctx)

        result = asyncio.run(_run())
        q = result.data.get("question") or ""
        assert "مثلاً سدر" not in q
        assert "الطلح" in q


class TestClarifyInsteadGuard:
    def test_clarify_instead_blocks_legacy_general_attribute_with_subject(self):
        ctx = _ctx("عسل السمر بكم الكيلو", focus=None)
        dec = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
        if dec.action == ACTION_CLARIFY:
            q = str((dec.args or {}).get("question") or "")
            assert "تقصد حاجة أو مواصفة" not in q
            assert "مثلاً سدر" not in q


class TestSearchHandlerRetry:
    def test_search_retries_before_failure(self):
        from modules.ai.brain.execution.search import ProductSearchHandler

        async def _run():
            handler = ProductSearchHandler()
            ctx = _ctx("عسل السمر بكم الكيلو")
            ctx._db = MagicMock()
            ctx.customer_id = None

            hit = [{"id": 1, "title": "عسل سمر", "price": "100", "orderable": True}]

            async def fake_execute(tool, payload):
                q = payload.get("query", "")
                if q in {"السمر", "سمر"}:
                    return MagicMock(payload={"products": hit})
                return MagicMock(payload={"products": []})

            mock_runtime = MagicMock()
            mock_runtime.execute = AsyncMock(side_effect=fake_execute)

            with patch(
                "modules.ai.commerce.runtime.CommerceToolRuntime",
                return_value=mock_runtime,
            ):
                decision = Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": "عسل السمر"},
                    reason="test",
                    confidence=0.9,
                )
                return await handler.handle(decision, ctx)

        result = asyncio.run(_run())
        assert result.success is True
        assert result.data.get("count") == 1
