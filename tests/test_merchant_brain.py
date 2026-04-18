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
    INTENT_GREETING, INTENT_ASK_PRODUCT, INTENT_START_ORDER, INTENT_GENERAL,
    BrainContext, CommerceFacts, Decision, ActionResult, Intent,
    MerchantConversationState,
)
from modules.ai.brain.decision.actions import (
    ACTION_GREET, ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER,
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
        integration_platform="salla",
    )


def _make_state(**kw) -> MerchantConversationState:
    return MerchantConversationState(**kw)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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

    def test_first_turn_always_greets(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_PRODUCT, _make_state(greeted=False), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_GREET

    def test_ask_product_after_greeting(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        eng = DefaultDecisionEngine()
        ctx = self._ctx(INTENT_ASK_PRODUCT, _make_state(greeted=True), _make_facts())
        d = eng.decide(ctx)
        assert d.action == ACTION_SEARCH_PRODUCTS

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

    def test_unknown_action_falls_back_to_llm(self):
        from modules.ai.brain.execution.executor import DefaultActionExecutor
        executor = DefaultActionExecutor()
        ctx = self._ctx()
        result = _run(executor.execute(Decision(action="unknown_action_xyz"), ctx))
        assert result.success
        assert result.data.get("type") == "llm_fallback"


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
