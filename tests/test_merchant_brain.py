"""
tests/test_merchant_brain.py
─────────────────────────────
Unit tests for Merchant Brain Phase 1.

These tests are *pure unit tests* — no database, no HTTP, no LLM calls.
Every external dependency is replaced with a mock or stub.

Scenarios:
  1. greeting      — customer says "مرحبا" → persona_social greeting compose
  2. ask_product   — customer asks for a product → ACTION_SEARCH_PRODUCTS
  3. draft_order   — customer says "أبغى أطلب" with product in focus → ACTION_PROPOSE_DRAFT_ORDER
  4. no_products   — store has no products → ACTION_LLM_REPLY (or search with empty result → no_products template)
  5. fallback      — unknown message → ACTION_LLM_REPLY
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List

import sys
from pathlib import Path

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Import brain modules ───────────────────────────────────────────────────────
from modules.ai.brain.types import (
    INTENT_GREETING, INTENT_ASK_LOCATION, INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PRODUCT, INTENT_ASK_SHIPPING, INTENT_ASK_STORE_INFO,
    INTENT_START_ORDER, INTENT_GENERAL, INTENT_WHO_ARE_YOU,
    BrainContext, CommerceFacts, Decision, ActionResult, Intent,
    MerchantConversationState, OrderPreparationState, SalesContextSnapshot,
)
from modules.ai.brain.decision.actions import (
    ACTION_FAQ_REPLY, ACTION_GREET, ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_ORDER_CONTEXT_UPDATE, ACTION_LLM_REPLY,
)
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
)


def _assert_persona_greeting_llm(decision: Decision) -> None:
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_facts(has_products: bool = True, has_coupons: bool = False) -> CommerceFacts:
    return CommerceFacts(
        has_products=has_products,
        product_count=5 if has_products else 0,
        in_stock_count=5 if has_products else 0,
        has_active_integration=True,
        orderable=has_products,          # orderable = integration + in_stock
        has_coupons=has_coupons,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
        store_description="متجر عسل فاخر ومنتجات طبيعية",
        store_contact_phone="+966500000001",
        shipping_policy="الشحن خلال 2-4 أيام عمل",
        support_hours="9am-10pm",
        shipping_methods=["سمسا", "ارامكس"],
        integration_platform="salla",
    )


def _make_state(**kw) -> MerchantConversationState:
    return MerchantConversationState(**kw)


def _run(coro):
    return asyncio.run(coro)


def _brain_pipeline_quota_patch():
    """Stub quota gate for brain unit tests that use MagicMock DB sessions."""
    from core.wa_usage import AllowResult  # noqa: PLC0415

    return patch(
        "core.wa_usage.check_limit",
        return_value=AllowResult(
            allowed=True,
            reason="ok",
            used_total=0,
            limit=1000,
            pct=0.0,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Intent rules
# ─────────────────────────────────────────────────────────────────────────────

class TestIntentRules:
    def test_greeting_arabic(self):
        from modules.ai.brain.intent.rules import match
        result = match("السلام عليكم")
        assert result is not None
        assert result.name == INTENT_GREETING
        assert result.confidence >= 0.90

    def test_greeting_hello(self):
        from modules.ai.brain.intent.rules import match
        result = match("مرحبا")
        assert result is not None
        assert result.name == INTENT_GREETING

    def test_identity_question(self):
        from modules.ai.brain.intent.rules import match
        result = match("من أنت")
        assert result is not None
        assert result.name == INTENT_WHO_ARE_YOU

    def test_physical_location_question(self):
        """Physical shop / maps phrasing → ask_location since af6186c3."""
        from modules.ai.brain.intent.rules import match
        result = match("وين موقعكم")
        assert result is not None
        assert result.name == INTENT_ASK_LOCATION

    def test_online_store_link_question(self):
        """Online storefront phrasing stays on ask_store_info."""
        from modules.ai.brain.intent.rules import match
        result = match("رابط المتجر")
        assert result is not None
        assert result.name == INTENT_ASK_STORE_INFO

    def test_owner_contact_question(self):
        from modules.ai.brain.intent.rules import match
        result = match("أبغى رقم التواصل")
        assert result is not None
        assert result.name == INTENT_ASK_OWNER_CONTACT

    def test_ask_product(self):
        from modules.ai.brain.intent.rules import match
        result = match("عندكم شاشة كمبيوتر؟")
        assert result is not None
        assert result.name == INTENT_ASK_PRODUCT

    def test_start_order(self):
        from modules.ai.brain.intent.rules import match
        result = match("أبغى أطلب منتج")
        assert result is not None
        assert result.name == INTENT_START_ORDER

    def test_unknown_returns_none(self):
        from modules.ai.brain.intent.rules import match
        result = match("xkcd 927 zyxw")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Decision engine
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionEngine:
    def _ctx(self, intent_name: str, state: MerchantConversationState, facts: CommerceFacts) -> BrainContext:
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="test",
            intent=Intent(name=intent_name, confidence=0.90, raw_message="test"),
            state=state,
            facts=facts,
        )
        return ctx

    def test_greeting_decision(self):
        """Phase 3 — pure first-turn greeting routes to persona LLM by default."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_GREETING, _make_state(greeted=False), _make_facts())
        ctx.message = "مرحبا"
        ctx.intent.raw_message = "مرحبا"
        ctx.intent.slots = {}
        d = eng.decide(ctx)
        assert not (d.args or {}).get("embedded_greeting")
        assert not (ctx.intent.slots or {}).get("embedded_greeting")
        _assert_persona_greeting_llm(d)

    def test_greeting_decision_uses_template_when_avoid_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_GREETING, _make_state(greeted=False), _make_facts())
        ctx.message = "مرحبا"
        ctx.intent.raw_message = "مرحبا"
        ctx.intent.slots = {}
        d = eng.decide(ctx)
        assert d.action == ACTION_GREET
        assert d.action != ACTION_LLM_REPLY

    def _product_inquiry_ctx(
        self, state: MerchantConversationState,
    ) -> BrainContext:
        """Real SKU inquiry — required since 30b997da product discovery gate.

        Availability phrasing alone does not yield a catalog query; the
        classifier/slot layer supplies ``product_query`` (here: the noun
        from the customer's ask) before the decision engine searches.
        """
        msg = "عندكم شاشة كمبيوتر؟"
        ctx = self._ctx(INTENT_ASK_PRODUCT, state, _make_facts())
        ctx.message = msg
        ctx.intent.raw_message = msg
        ctx.intent.slots = {"product_query": "شاشة كمبيوتر"}
        return ctx

    def test_first_turn_product_question_does_not_force_greeting(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._product_inquiry_ctx(_make_state(greeted=False))
        d = eng.decide(ctx)
        assert d.action == ACTION_SEARCH_PRODUCTS

    def test_ask_product_after_greeting(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._product_inquiry_ctx(_make_state(greeted=True))
        d = eng.decide(ctx)
        assert d.action == ACTION_SEARCH_PRODUCTS

    def test_identity_goes_to_persona_compose(self):
        """Identity probes use persona compose since 6635e858 — not FAQ templates."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        msg = "من أنت"
        ctx = self._ctx(INTENT_WHO_ARE_YOU, _make_state(greeted=False), _make_facts())
        ctx.message = msg
        ctx.intent.raw_message = msg
        d = eng.decide(ctx)
        assert d.action == ACTION_LLM_REPLY
        assert d.args["topic"] == "persona_identity"
        assert d.args.get("block_commerce_escalation") is True

    def test_shipping_goes_to_brain_with_topic_hint(self):
        """``faq_shipping()`` template was disabled (June 2026) — the
        canned "بالنسبة للشحن: …" reply read robotic on simple
        questions like "تتوصلون للقصيم؟". Every ASK_SHIPPING decision
        now routes to ``ACTION_LLM_REPLY`` with a ``topic_hint`` so
        the brain composes the reply itself."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_SHIPPING, _make_state(greeted=True), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_LLM_REPLY
        assert d.args.get("topic_hint") == "shipping"
        # Legacy ``topic='shipping'`` arg (the FAQ-template trigger)
        # MUST be absent so the composer never short-circuits into
        # ``T.faq_shipping(...)``.
        assert d.args.get("topic") != "shipping"

    def test_start_order_with_focus(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            current_product_focus={"id": 1, "external_id": "ext-1", "title": "منتج تجريبي"},
        )
        ctx = self._ctx(INTENT_START_ORDER, state, _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_active_order_captures_checkout_fulfillment_slots(self):
        """City + short code during ordering → order_context_update since 0526ae17."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            stage="ordering",
            current_product_focus={"id": 1, "external_id": "ext-1", "title": "منتج تجريبي"},
            order_prep=OrderPreparationState(customer_first_name="محمد"),
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="الرياض وكودي ABCD1234",
            intent=Intent(
                name=INTENT_GENERAL,
                confidence=0.72,
                raw_message="الرياض وكودي ABCD1234",
                slots={"city": "الرياض", "short_address_code": "ABCD1234"},
            ),
            state=state,
            facts=_make_facts(),
        )
        d = eng.decide(ctx)
        assert d.action == ACTION_ORDER_CONTEXT_UPDATE
        assert d.args.get("city") == "الرياض"
        assert d.args.get("short_address_code") == "ABCD1234"
        assert d.args.get("fulfillment_kind") == "location_update"

    def test_ask_product_no_catalog(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_PRODUCT, _make_state(greeted=True), _make_facts(has_products=False))
        d = eng.decide(ctx)
        assert d.action == ACTION_LLM_REPLY

    def test_unknown_fallback(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_GENERAL, _make_state(greeted=True), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_LLM_REPLY


# ─────────────────────────────────────────────────────────────────────────────
# 3. Composer templates
# ─────────────────────────────────────────────────────────────────────────────

class TestComposerTemplates:
    def test_greeting_phatic_variant_may_include_store_name(self):
        from modules.ai.brain.compose.templates import greeting

        # ARCH-KB-001: store name only in variant 2 — phatic, no self-intro.
        text = greeting(store_name="متجر النور", variant=2)
        assert "متجر النور" in text
        assert "أنا " not in text
        assert "كيف أقدر" not in text

    def test_greeting_default_variant_is_phatic_without_store_name(self):
        from modules.ai.brain.compose.templates import greeting

        text = greeting(store_name="متجر النور", variant=0)
        assert "متجر النور" not in text
        assert "أنا " not in text
        assert "كيف أقدر" not in text

    def test_product_results(self):
        from modules.ai.brain.compose.templates import product_results
        text = product_results(product_lines="• منتج 1 — 100 ريال (متاح)", query="شاشة", count=1)
        assert "شاشة" in text
        assert "منتج 1" in text

    def test_no_products(self):
        from modules.ai.brain.compose.templates import no_products
        text = no_products()
        assert "عذراً" in text or "منتجات" in text

    def test_draft_order_with_link(self):
        from modules.ai.brain.compose.templates import draft_order_created
        text = draft_order_created(
            product={"title": "سماعة جي بي ال"},
            reference="ORD-001",
            checkout_url="https://pay.example.com/x",
            total=299.0,
            currency="SAR",
        )
        assert "سماعة جي بي ال" in text
        assert "ORD-001" in text
        assert "https://pay.example.com/x" in text

    def test_draft_order_intent_only(self):
        from modules.ai.brain.compose.templates import order_intent_captured
        text = order_intent_captured(product={"title": "كيبورد لوجيتك"})
        assert "كيبورد لوجيتك" in text

    def test_collect_order_details(self):
        from modules.ai.brain.compose.templates import collect_order_details
        text = collect_order_details(
            product={"title": "كيبورد لوجيتك"},
            question="ما اسمك الأول لإكمال الطلب؟",
            missing_fields=["customer_first_name"],
        )
        assert "كيبورد لوجيتك" in text
        assert "اسمك الأول" in text

    def test_faq_shipping_template(self):
        from modules.ai.brain.compose.templates import faq_shipping
        text = faq_shipping(
            shipping_policy="الشحن خلال 2-4 أيام عمل",
            shipping_methods=["سمسا"],
            support_hours="9am-10pm",
        )
        assert "الشحن" in text
        assert "سمسا" in text


# ─────────────────────────────────────────────────────────────────────────────
# 4. Executor dispatching
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutor:
    def _ctx(self) -> BrainContext:
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="test",
            intent=Intent(name=INTENT_GREETING, confidence=0.95, raw_message="test"),
            state=_make_state(),
            facts=_make_facts(),
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        return ctx

    def test_greet_action(self):
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.decision.actions import ACTION_GREET
        executor = DefaultActionExecutor()
        ctx = self._ctx()
        result = _run(executor.execute(Decision(action=ACTION_GREET), ctx))
        assert result.success
        assert result.data.get("type") == "greet"

    def test_handoff_action(self):
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        executor = DefaultActionExecutor()
        ctx = self._ctx()
        result = _run(executor.execute(Decision(action=ACTION_HANDOFF), ctx))
        assert result.success
        assert result.data.get("type") == "handoff"

    def test_faq_action(self):
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        executor = DefaultActionExecutor()
        ctx = self._ctx()
        result = _run(executor.execute(Decision(action=ACTION_FAQ_REPLY, args={"topic": "shipping"}), ctx))
        assert result.success
        assert result.data.get("type") == "faq"
        assert result.data.get("topic") == "shipping"

    def test_unknown_action_falls_back_to_llm(self):
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        executor = DefaultActionExecutor()
        ctx = self._ctx()
        result = _run(executor.execute(Decision(action="unknown_action_xyz"), ctx))
        assert result.success
        assert result.data.get("type") == "llm_fallback"


class TestOrderPreparation:
    def _ctx(self, *, message: str, slots: Dict[str, Any] | None = None, state: MerchantConversationState | None = None) -> BrainContext:
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message=message,
            intent=Intent(
                name=INTENT_START_ORDER if "أطلب" in message else INTENT_GENERAL,
                confidence=0.90,
                raw_message=message,
                slots=slots or {},
            ),
            state=state or _make_state(
                greeted=True,
                current_product_focus={"id": 1, "external_id": "101", "title": "عسل سدر"},
            ),
            facts=_make_facts(),
            profile={},
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        return ctx

    def test_draft_order_collects_missing_fields_first(self):
        from modules.ai.brain.execution.orders import DraftOrderHandler

        handler = DraftOrderHandler()
        ctx = self._ctx(message="أبغى أطلب")
        result = _run(handler.handle(
            Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={"product": {"id": 1, "external_id": "101", "title": "عسل سدر"}},
            ),
            ctx,
        ))

        assert result.success
        assert result.data["needs_collection"] is True
        assert "customer_first_name" in result.data["missing_fields"]
        assert result.data["question"]

    def test_draft_order_uses_short_code_and_creates_order(self):
        from modules.ai.brain.execution.orders import DraftOrderHandler
        from store_integration.models import NormalizedOrder

        handler = DraftOrderHandler()
        state = _make_state(
            greeted=True,
            current_product_focus={"id": 1, "external_id": "101", "title": "عسل سدر"},
            order_prep=OrderPreparationState(
                customer_first_name="محمد",
                customer_last_name="العتيبي",
                city="الرياض",
                short_address_code="ABCD1234",
                quantity=2,
            ),
        )
        ctx = self._ctx(message="كمل الطلب", state=state)

        with patch(
            "store_integration.order_service.create_draft_order",
            new=AsyncMock(return_value=NormalizedOrder(
                id="123",
                reference_id="ORD-123",
                status="draft",
                total=240.0,
                currency="SAR",
                payment_link="https://pay.example.com/order/123",
                customer_name="محمد العتيبي",
                customer_phone="+966500000001",
            )),
        ) as mock_create, patch(
            "modules.ai.brain.execution.orders.resolve_short_address",
            new=AsyncMock(return_value=None),
        ):
            result = _run(handler.handle(
                Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": {"id": 1, "external_id": "101", "title": "عسل سدر"}},
                ),
                ctx,
            ))

        assert result.success
        assert result.data["checkout_url"] == "https://pay.example.com/order/123"
        assert result.data["order_prep"]["short_address_code"] == "ABCD1234"
        _, order_input = mock_create.await_args.args
        assert order_input.short_address_code == "ABCD1234"
        assert order_input.items[0].quantity == 2

    def test_extract_address_signals_from_google_maps(self):
        from services.address_resolution import extract_address_signals

        signals = extract_address_signals(
            "هذا موقعي https://maps.google.com/?q=24.7136,46.6753 وكودي abcd1234"
        )
        assert signals["short_address_code"] == "ABCD1234"
        assert "maps.google.com" in signals["google_maps_url"]
        assert signals["latitude"] == pytest.approx(24.7136)
        assert signals["longitude"] == pytest.approx(46.6753)


class TestThinLLMComposer:
    def test_llm_compose_uses_brain_reply_state(self):
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.types import BrainReplyState, SuggestionSnapshot
        from modules.ai.orchestrator.types import AIReplyPayload

        composer = DefaultComposer()
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="رسالة غامضة",
            intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message="رسالة غامضة"),
            state=_make_state(greeted=True, stage="exploring"),
            facts=_make_facts(),
            profile={"preferred_language": "ar", "communication_style": "neutral"},
        )
        ctx.reply_state = BrainReplyState(
            store_name="متجر تجريبي",
            tone="neutral",
            stage="exploring",
            customer_goal="discover_products",
            known_facts={"store_name": "متجر تجريبي"},
            recommended_next_step="clarify_need",
        )
        ctx.suggestion = SuggestionSnapshot(suggested_next_step="clarify_need")
        result = ActionResult(success=True, data={"type": "llm_fallback"})

        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=AIReplyPayload(
                reply_text="رد ذكي قصير",
                provider_used="anthropic",
                metadata={"model": "claude-test"},
            ),
        ) as mock_generate:
            reply = _run(composer.compose(Decision(action=ACTION_LLM_REPLY), result, ctx))

        assert reply == "رد ذكي قصير"
        kwargs = mock_generate.call_args.kwargs
        assert kwargs["context_metadata"]["brain_state"]["stage"] == "exploring"
        assert kwargs["context_metadata"]["brain_state"]["recommended_next_step"] == "clarify_need"
        assert "sales_context" in kwargs["context_metadata"]
        assert kwargs["prompt_overrides"]["__full_system_prompt"]
        assert result.data["chosen_path"] == "llm"

    def test_llm_compose_passes_history(self):
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.types import BrainReplyState, SuggestionSnapshot
        from modules.ai.orchestrator.types import AIReplyPayload

        composer = DefaultComposer()
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="رسالة جديدة",
            intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message="رسالة جديدة"),
            state=_make_state(greeted=True, stage="exploring"),
            facts=_make_facts(),
            profile={"preferred_language": "ar", "communication_style": "neutral"},
            history=[
                {"direction": "inbound", "body": "أبغى منتج"},
                {"direction": "outbound", "body": "أكيد"},
            ],
            sales_context=SalesContextSnapshot(conversation_memory={"conversation_summary": "ملخص"}),
        )
        ctx.reply_state = BrainReplyState(store_name="متجر", stage="exploring")
        ctx.suggestion = SuggestionSnapshot(suggested_next_step="clarify_need")
        result = ActionResult(success=True, data={"type": "llm_fallback"})

        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=AIReplyPayload(reply_text="رد", provider_used="anthropic", metadata={"model": "x"}),
        ) as mock_generate:
            reply = _run(composer.compose(Decision(action=ACTION_LLM_REPLY), result, ctx))

        assert reply == "رد"
        assert len(mock_generate.call_args.kwargs["history"]) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 5. Full pipeline (mocked externals)
# ─────────────────────────────────────────────────────────────────────────────

class TestBrainPipeline:
    """End-to-end pipeline tests with all external I/O mocked."""

    def _quota_allowed_patch(self):
        """Brain unit tests use MagicMock DB — stub quota gate."""
        return _brain_pipeline_quota_patch()

    def _build_brain(self, mock_classify, mock_facts, mock_state):
        from modules.ai.brain.pipeline import MerchantBrain
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import PassThroughPolicyGate
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        return MerchantBrain(
            classifier     = mock_classify,
            state_store    = mock_state,
            facts_loader   = mock_facts,
            decision_engine= DefaultDecisionEngine(),
            policy_gate    = PassThroughPolicyGate(),
            executor       = DefaultActionExecutor(),
            composer       = DefaultComposer(),
            memory_updater = DefaultMemoryUpdater(),
        )

    def _mock_state_store(self, state: MerchantConversationState):
        store = MagicMock()
        store.load.return_value = state
        store.save.return_value = None
        store.transition.return_value = state
        return store

    def _mock_facts_loader(self, facts: CommerceFacts):
        loader = MagicMock()
        loader.load.return_value = facts
        return loader

    def _mock_classifier(self, intent: Intent):
        cls = MagicMock()
        cls.classify = AsyncMock(return_value=intent)
        return cls

    def _mock_memory_updater(self):
        updater = MagicMock()
        updater.update.return_value = None
        return updater

    def _db(self):
        db = MagicMock()
        db.add.return_value = None
        db.commit.return_value = None
        return db

    def test_greeting_scenario(self):
        """Cold pure greeting → persona LLM compose by default (Phase 3)."""
        intent = Intent(
            name=INTENT_GREETING,
            confidence=0.95,
            raw_message="مرحبا",
            slots={},
        )
        state = _make_state(greeted=False)
        facts = _make_facts()

        classifier = self._mock_classifier(intent)
        state_store = self._mock_state_store(state)
        facts_loader = self._mock_facts_loader(facts)
        captured: Dict[str, Any] = {}

        def _transition_side_effect(current_state, transition_intent, decision):
            captured["decision"] = decision
            current_state.greeted = True
            return current_state

        state_store.transition.side_effect = _transition_side_effect

        from modules.ai.brain.pipeline import MerchantBrain
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import PassThroughPolicyGate
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.compose.responder import DefaultComposer

        memory_updater = self._mock_memory_updater()
        llm_mock = AsyncMock(return_value="ياهلا 🌷")

        b = MerchantBrain(
            classifier=classifier,
            state_store=state_store,
            facts_loader=facts_loader,
            decision_engine=DefaultDecisionEngine(),
            policy_gate=PassThroughPolicyGate(),
            executor=DefaultActionExecutor(),
            composer=DefaultComposer(),
            memory_updater=memory_updater,
        )

        with _brain_pipeline_quota_patch(), patch(
            "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
            llm_mock,
        ):
            reply = _run(b.process(
                db=self._db(),
                tenant_id=1,
                customer_phone="+966500000001",
                message="مرحبا",
                history=[],
                profile={},
            ))

        llm_mock.assert_called_once()
        assert isinstance(reply, dict)
        text = reply.get("reply")
        assert isinstance(text, str)
        assert text.strip() == "ياهلا 🌷"
        assert not (intent.slots or {}).get("embedded_greeting")
        assert "المنتج" not in text
        assert "السعر" not in text

        decision = captured.get("decision")
        assert decision is not None
        _assert_persona_greeting_llm(decision)

    def test_no_products_scenario(self):
        intent = Intent(name=INTENT_ASK_PRODUCT, confidence=0.90, raw_message="عندكم منتج؟",
                        slots={"product_query": "منتج"})
        state  = _make_state(greeted=True)
        facts  = _make_facts(has_products=False)

        from modules.ai.brain.pipeline import MerchantBrain
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import PassThroughPolicyGate
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.compose.responder import DefaultComposer

        memory_updater = self._mock_memory_updater()

        b = MerchantBrain(
            classifier=self._mock_classifier(intent),
            state_store=self._mock_state_store(state),
            facts_loader=self._mock_facts_loader(facts),
            decision_engine=DefaultDecisionEngine(),
            policy_gate=PassThroughPolicyGate(),
            executor=DefaultActionExecutor(),
            composer=DefaultComposer(),
            memory_updater=memory_updater,
        )

        # When no products, DecisionEngine → ACTION_LLM_REPLY
        # DefaultComposer._llm_compose will fail (no DB/API) → generic_fallback
        with _brain_pipeline_quota_patch(), patch("modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
                   new_callable=AsyncMock, return_value="fallback reply"):
            reply = _run(b.process(
                db=self._db(),
                tenant_id=1,
                customer_phone="+966500000001",
                message="عندكم منتج؟",
                history=[],
                profile={},
            ))

        assert isinstance(reply, dict)
        assert isinstance(reply.get("reply"), str)
        assert len(reply["reply"]) > 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Simplification pass — single source of decision (Decision → Composer)
# ─────────────────────────────────────────────────────────────────────────────

class TestStateDrivenSimplification:
    """Locks the rules added in the simplification pass.

    Each test maps to one bullet in the user-facing simplification spec:
      - templates are state-driven (no GREET when greeted=True or mid-order)
      - no welcome twice in the same conversation
      - LLM fallback always receives intent / state / product / goal
      - state survives the order flow (ASK_PRODUCT mid-order doesn't reset)
    """

    def _ctx(self, intent_name: str, state: MerchantConversationState,
             facts: CommerceFacts, message: str = "test",
             slots: Dict[str, Any] | None = None) -> BrainContext:
        return BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message=message,
            intent=Intent(
                name=intent_name, confidence=0.9,
                raw_message=message, slots=slots or {},
            ),
            state=state,
            facts=facts,
        )

    # --- DecisionEngine: mid-order checkout lock (87e028ea) + fulfillment slots (0526ae17) ---

    def test_ask_product_mid_order_with_order_prep_continues_checkout(self):
        """Mid-order ASK_PRODUCT with default order_prep → rule_based_checkout since 87e028ea.

        Section 3.7 no longer treats ASK_PRODUCT as continuation, but the earlier
        deterministic checkout block still fires while order_prep is active unless
        has_explicit_commerce_topic_change bypasses it.
        """
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            stage="ordering",
            current_product_focus={"id": 1, "external_id": "ext-1", "title": "فستان"},
        )
        ctx = self._ctx(INTENT_ASK_PRODUCT, state, _make_facts(),
                        message="تعرض لي المنتجات بالصور؟")
        d = eng.decide(ctx)
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "rule_based_checkout" in (d.reason or "")

    def test_address_message_mid_order_captures_fulfillment_slots(self):
        """City + short code mid-order → order_context_update since 0526ae17."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            stage="ordering",
            current_product_focus={"id": 1, "external_id": "ext-1", "title": "فستان"},
        )
        ctx = self._ctx(
            INTENT_ASK_PRODUCT, state, _make_facts(),
            message="الرياض وكودي ABCD1234",
            slots={"city": "الرياض", "short_address_code": "ABCD1234"},
        )
        d = eng.decide(ctx)
        assert d.action == ACTION_ORDER_CONTEXT_UPDATE
        assert d.args.get("city") == "الرياض"
        assert d.args.get("short_address_code") == "ABCD1234"
        assert d.args.get("fulfillment_kind") == "location_update"

    # --- Conversation Commerce State Tracking (merchant feedback round 2) ---
    #
    # The merchant reported that after the customer typed their national
    # address mid-funnel the bot replied "قبل ما نكمّل، اختر المنتج اللي
    # تبغاه". Three regression locks below cover the three known root
    # causes: the pre-product stash block hijacking address signals from
    # a live funnel, the safety net forcing a search when focus is gone,
    # and the safety net asking "ما المنتج؟" when order_prep already
    # holds a valid product_id we can recover from cache.

    def test_address_signal_with_live_order_prep_does_not_stash(self):
        """short_address_code arrives without current_product_focus BUT
        order_prep carries a name + product_id. That's not a pre-product
        stash — it's the next slot in an active funnel."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_STASH_ADDRESS_PRE_PRODUCT
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            stage="ordering",
            current_product_focus=None,
            order_prep=OrderPreparationState(
                product_id="ext-1",
                customer_first_name="محمد",
                city="الرياض",
            ),
            last_search_candidates=[
                {"id": 1, "external_id": "ext-1", "title": "عسل سمر",
                 "can_checkout": True, "orderable": True},
            ],
        )
        ctx = self._ctx(
            INTENT_GENERAL, state, _make_facts(),
            message="ABCD1234",
            slots={"short_address_code": "ABCD1234"},
        )
        d = eng.decide(ctx)
        assert d.action != ACTION_STASH_ADDRESS_PRE_PRODUCT, (
            "address signal mid-funnel must not be routed to stash-pre-product"
        )

    def test_safety_net_recovers_focus_from_order_prep_product_id(self):
        """Focus was wiped but order_prep still remembers the product_id
        and cached candidates contain it → safety net must rehydrate the
        focus and continue the order, NOT show 'ما المنتج؟'."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        state = _make_state(
            greeted=True,
            stage="ordering",
            current_product_focus=None,
            order_prep=OrderPreparationState(
                product_id="ext-7",
                customer_first_name="نورة",
                city="جدة",
                short_address_code="JEDD9988",
            ),
            last_search_candidates=[
                {"id": 7, "external_id": "ext-7", "title": "عسل السدر",
                 "can_checkout": True, "orderable": True},
            ],
        )
        ctx = self._ctx(
            INTENT_GENERAL, state, _make_facts(),
            message="تمام، أرسل الطلب",
        )
        d = eng.decide(ctx)
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER
        recovered = d.args.get("forced_product") or d.args.get("product") or {}
        assert recovered.get("external_id") == "ext-7"
        assert d.args.get("source") == "order_prep_recovery"
        # The reason string MUST explicitly call out the recovery so it's
        # easy to grep in production Railway logs.
        assert "order_prep" in (d.reason or "").lower()

    # --- Composer: defense-in-depth greet guard ---

    def test_composer_downgrades_greet_when_already_greeted(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Legacy template re-greet when routine LLM avoid is enabled."""
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
        from modules.ai.brain.compose.persona_template_engine import (
            PERSONA_ALLOWED_EMOJI,
            PERSONA_GREETING_REGREET,
            persona_reply_has_light_emoji,
            persona_reply_is_warm_greeting,
        )
        from modules.ai.brain.compose.responder import DefaultComposer
        composer = DefaultComposer()
        state = _make_state(greeted=True)
        ctx = BrainContext(
            tenant_id=1, customer_phone="+966500000001", message="هلا",
            intent=Intent(name=INTENT_GREETING, confidence=0.9, raw_message="هلا"),
            state=state, facts=_make_facts(), profile={},
        )
        decision = Decision(action=ACTION_GREET)
        llm_mock = AsyncMock(return_value="must not call llm")
        with patch(
            "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
            llm_mock,
        ):
            reply = _run(composer.compose(decision, ActionResult(success=True), ctx))
        llm_mock.assert_not_called()
        assert reply.strip()
        assert reply in PERSONA_GREETING_REGREET or persona_reply_is_warm_greeting(reply)
        assert persona_reply_has_light_emoji(reply)
        assert sum(reply.count(e) for e in PERSONA_ALLOWED_EMOJI) <= 1
        assert "المنتج" not in reply
        assert "السعر" not in reply
        assert decision.action == ACTION_GREET
        assert decision.action != ACTION_LLM_REPLY

    def test_composer_mid_order_phatic_greet_is_warm_without_checkout_pressure(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Mid-order phatic greet → natural warm reply; no checkout slot pressure (#443)."""
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
        from modules.ai.brain.compose.persona_template_engine import (
            PERSONA_ALLOWED_EMOJI,
            PERSONA_GREETING_COLD,
            PERSONA_GREETING_REGREET,
            persona_reply_has_light_emoji,
            persona_reply_is_order_aware_greeting,
            persona_reply_is_warm_greeting,
        )
        from modules.ai.brain.compose.responder import DefaultComposer
        composer = DefaultComposer()
        state = _make_state(greeted=False, stage="ordering",
                            current_product_focus={"id": 1, "title": "X"})
        ctx = BrainContext(
            tenant_id=1, customer_phone="+966500000001", message="هلا",
            intent=Intent(name=INTENT_GREETING, confidence=0.9, raw_message="هلا"),
            state=state, facts=_make_facts(), profile={},
        )
        decision = Decision(action=ACTION_GREET)
        llm_mock = AsyncMock(return_value="must not call llm")
        with patch(
            "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
            llm_mock,
        ):
            reply = _run(composer.compose(decision, ActionResult(success=True), ctx))
        llm_mock.assert_not_called()
        assert reply.strip()
        assert (
            reply in PERSONA_GREETING_REGREET
            or reply in PERSONA_GREETING_COLD
            or persona_reply_is_warm_greeting(reply)
        )
        assert not persona_reply_is_order_aware_greeting(reply)
        assert "نكمل طلبك" not in reply
        assert "أرسل عنوان" not in reply
        assert "الدفع" not in reply
        assert "اسمك" not in reply
        assert persona_reply_has_light_emoji(reply)
        assert sum(reply.count(e) for e in PERSONA_ALLOWED_EMOJI) <= 1
        assert decision.action == ACTION_GREET
        assert decision.action != ACTION_LLM_REPLY

    def test_composer_first_greet_still_fires_template(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Legacy ACTION_GREET template path when routine LLM avoid is enabled."""
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
        from modules.ai.brain.compose.responder import DefaultComposer
        composer = DefaultComposer()
        state = _make_state(greeted=False, stage="discovery")
        ctx = BrainContext(
            tenant_id=1, customer_phone="+966500000001", message="السلام عليكم",
            intent=Intent(name=INTENT_GREETING, confidence=0.95,
                          raw_message="السلام عليكم"),
            state=state, facts=_make_facts(), profile={},
        )
        reply = _run(composer.compose(
            Decision(action=ACTION_GREET), ActionResult(success=True), ctx,
        ))
        # ARCH-KB-001: ACTION_GREET still fires; phatic reply + salam etiquette.
        assert reply.strip()
        assert "وعليكم السلام" in reply
        assert "هلا" in reply
        assert "أنا " not in reply
        assert "كيف أقدر" not in reply
        # variant=0 (empty history) — store name not required in phatic greeting.
        assert "متجر تجريبي" not in reply

    # --- LLM fallback contract: intent + state + product + goal ---

    def test_minimal_reply_state_carries_required_fields(self):
        """Even the degraded reply_state built when ctx.reply_state is None
        must surface the four fields the simplification spec requires."""
        from modules.ai.brain.compose.responder import DefaultComposer
        composer = DefaultComposer()
        state = _make_state(
            greeted=True, stage="ordering",
            current_product_focus={"id": 7, "title": "عطر العود"},
            customer_goal="complete_purchase",
        )
        ctx = BrainContext(
            tenant_id=1, customer_phone="+966500000001",
            message="نعم", intent=Intent(name=INTENT_GENERAL, confidence=0.6,
                                          raw_message="نعم"),
            state=state, facts=_make_facts(), profile={},
        )
        rs = composer._minimal_reply_state(ctx)
        assert rs.intent_name == INTENT_GENERAL
        assert rs.stage == "ordering"
        assert rs.selected_product == {"id": 7, "title": "عطر العود"}
        assert rs.response_goal  # non-empty

    # --- Pipeline: greeted inferred from history (catches proactive sends) ---

    def test_pipeline_infers_greeted_from_outbound_history(self):
        """History showing prior outbound → established persona re-greet via LLM."""
        from modules.ai.brain.compose.persona_template_engine import (
            PERSONA_ALLOWED_EMOJI,
            persona_reply_has_light_emoji,
            persona_reply_is_warm_greeting,
        )
        from modules.ai.brain.pipeline import MerchantBrain
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import PassThroughPolicyGate
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.compose.responder import DefaultComposer

        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="هلا")
        state  = _make_state(greeted=False)  # state on disk says not greeted
        facts  = _make_facts()

        state_store = MagicMock()
        state_store.load.return_value = state
        state_store.save.return_value = None
        state_store.transition.side_effect = lambda s, i, d: s

        classifier = MagicMock()
        classifier.classify = AsyncMock(return_value=intent)

        facts_loader = MagicMock()
        facts_loader.load.return_value = facts

        memory_updater = MagicMock()
        memory_updater.update.return_value = None

        b = MerchantBrain(
            classifier=classifier, state_store=state_store,
            facts_loader=facts_loader,
            decision_engine=DefaultDecisionEngine(),
            policy_gate=PassThroughPolicyGate(),
            executor=DefaultActionExecutor(),
            composer=DefaultComposer(),
            memory_updater=memory_updater,
        )

        history = [
            {"direction": "in",  "body": "السلام عليكم"},
            {"direction": "out", "body": "صبحًا خصمك جاهز — اضغط الزر تحت لإكمال الطلب"},
        ]

        llm_mock = AsyncMock(return_value="ياهلا 🌷")
        with _brain_pipeline_quota_patch(), patch(
            "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
            llm_mock,
        ):
            reply = _run(b.process(
                db=MagicMock(), tenant_id=1, customer_phone="+966500000001",
                message="هلا", history=history, profile={},
            ))

        llm_mock.assert_called_once()
        text = reply["reply"]
        assert isinstance(text, str)
        assert text.strip() == "ياهلا 🌷"
        assert persona_reply_is_warm_greeting(text)
        assert persona_reply_has_light_emoji(text)
        assert sum(text.count(e) for e in PERSONA_ALLOWED_EMOJI) <= 1
        assert "المنتج" not in text
        assert "السعر" not in text
