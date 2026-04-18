"""
tests/test_merchant_brain.py
─────────────────────────────
Unit tests for Merchant Brain Phase 1.

These tests are *pure unit tests* — no database, no HTTP, no LLM calls.
Every external dependency is replaced with a mock or stub.

Scenarios:
  1. greeting      — customer says "مرحبا" → ACTION_GREET, greet template
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
    INTENT_GREETING, INTENT_ASK_OWNER_CONTACT, INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING, INTENT_ASK_STORE_INFO, INTENT_START_ORDER,
    INTENT_GENERAL, INTENT_WHO_ARE_YOU,
    BrainContext, CommerceFacts, Decision, ActionResult, Intent,
    MerchantConversationState, OrderPreparationState,
)
from modules.ai.brain.decision.actions import (
    ACTION_FAQ_REPLY, ACTION_GREET, ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_LLM_REPLY,
)


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

    def test_store_info_question(self):
        from modules.ai.brain.intent.rules import match
        result = match("وين موقعكم")
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
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_GREETING, _make_state(greeted=False), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_GREET

    def test_first_turn_product_question_does_not_force_greeting(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_PRODUCT, _make_state(greeted=False), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_SEARCH_PRODUCTS

    def test_ask_product_after_greeting(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_PRODUCT, _make_state(greeted=True), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_SEARCH_PRODUCTS

    def test_identity_goes_to_faq(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_WHO_ARE_YOU, _make_state(greeted=False), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_FAQ_REPLY
        assert d.args["topic"] == "identity"

    def test_shipping_goes_to_faq(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_SHIPPING, _make_state(greeted=True), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_FAQ_REPLY
        assert d.args["topic"] == "shipping"

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

    def test_continue_order_preparation_with_checkout_slots(self):
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
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER

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
    def test_greeting_contains_store_name(self):
        from modules.ai.brain.compose.templates import greeting
        text = greeting(store_name="متجر النور")
        assert "متجر النور" in text

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
        assert kwargs["prompt_overrides"]["__full_system_prompt"]
        assert result.data["chosen_path"] == "llm"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Full pipeline (mocked externals)
# ─────────────────────────────────────────────────────────────────────────────

class TestBrainPipeline:
    """End-to-end pipeline tests with all external I/O mocked."""

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
        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
        state  = _make_state(greeted=False)
        facts  = _make_facts()

        classifier = self._mock_classifier(intent)
        state_store = self._mock_state_store(state)
        facts_loader = self._mock_facts_loader(facts)

        brain = MagicMock()
        brain.classifier = classifier
        brain.state_store = state_store
        brain.facts_loader = facts_loader

        from modules.ai.brain.pipeline import MerchantBrain
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import PassThroughPolicyGate
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        from modules.ai.brain.compose.responder import DefaultComposer

        memory_updater = self._mock_memory_updater()

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

        reply = _run(b.process(
            db=self._db(),
            tenant_id=1,
            customer_phone="+966500000001",
            message="مرحبا",
            history=[],
            profile={},
        ))

        assert isinstance(reply, str)
        assert len(reply) > 0
        # Greeting template should mention متجرنا or the store name
        assert "أهلاً" in reply or "مرحب" in reply or "متجر" in reply

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
        with patch("modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
                   new_callable=AsyncMock, return_value="fallback reply"):
            reply = _run(b.process(
                db=self._db(),
                tenant_id=1,
                customer_phone="+966500000001",
                message="عندكم منتج؟",
                history=[],
                profile={},
            ))

        assert isinstance(reply, str)
        assert len(reply) > 0
