"""
brain/pipeline.py
──────────────────
MerchantBrain — the Phase 1 Commerce Decision Engine.

Turn processing flow:
  message → IntentClassifier → StateStore.load → FactsLoader.load
          → BrainContext assembly
          → DecisionEngine.decide
          → PolicyGate.gate
          → ActionExecutor.execute
          → StateStore.transition + save
          → Composer.compose
          → MemoryUpdater.update
          → reply string

The build_default_brain() factory wires all Phase 1 default implementations
together. Any layer can be replaced by passing a different implementation.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    INTENT_GENERAL,
)
from .protocols import (
    IntentClassifier,
    StateStore,
    FactsLoader,
    DecisionMaker,
    PolicyGate,
    ActionExecutor,
    Composer,
    MemoryUpdater,
)

logger = logging.getLogger("nahla.brain.pipeline")


class MerchantBrain:
    """
    Orchestrates all Brain layers for a single customer turn.
    """

    def __init__(
        self,
        classifier: IntentClassifier,
        state_store: StateStore,
        facts_loader: FactsLoader,
        decision_engine: DecisionMaker,
        policy_gate: PolicyGate,
        executor: ActionExecutor,
        composer: Composer,
        memory_updater: MemoryUpdater,
    ) -> None:
        self._classifier     = classifier
        self._state_store    = state_store
        self._facts_loader   = facts_loader
        self._decision_engine= decision_engine
        self._policy_gate    = policy_gate
        self._executor       = executor
        self._composer       = composer
        self._memory_updater = memory_updater

    async def process(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
        message: str,
        history: List[Dict[str, Any]],
        profile: Dict[str, Any],
        customer_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
    ) -> str:
        t0 = time.monotonic()

        # ── 1. Intent ────────────────────────────────────────────────────
        state_for_classify = self._state_store.load(db, tenant_id, customer_phone)
        intent: Intent = await self._classifier.classify(message, history, state_for_classify)

        # ── 2. Load state + facts ─────────────────────────────────────────
        state: MerchantConversationState = state_for_classify
        facts: CommerceFacts             = self._facts_loader.load(db, tenant_id)

        # ── 3. Assemble context ───────────────────────────────────────────
        ctx = BrainContext(
            tenant_id      = tenant_id,
            customer_phone = customer_phone,
            message        = message,
            intent         = intent,
            state          = state,
            facts          = facts,
            history        = history,
            profile        = profile,
            customer_id    = customer_id,
            conversation_id= conversation_id,
        )
        # Attach db for handlers that need it (avoids threading Session issues)
        ctx._db = db  # type: ignore[attr-defined]

        stage_before = state.stage

        # ── 4. Decision ───────────────────────────────────────────────────
        decision: Decision   = self._decision_engine.decide(ctx)
        reason_before_policy = decision.reason
        decision             = self._policy_gate.gate(decision, ctx)

        # ── 5. Execute ────────────────────────────────────────────────────
        result: ActionResult = await self._executor.execute(decision, ctx)

        # ── 6. Compose reply ──────────────────────────────────────────────
        reply: str = await self._composer.compose(decision, result, ctx)

        # ── 7. Transition + persist state ─────────────────────────────────
        new_state = self._state_store.transition(state, intent, decision)
        if result.data.get("checkout_url"):
            new_state.checkout_url  = result.data["checkout_url"]
        if result.data.get("order_id"):
            new_state.draft_order_id = str(result.data["order_id"])
        if result.data.get("product") and not new_state.current_product_focus:
            new_state.current_product_focus = result.data["product"]

        self._state_store.save(db, tenant_id, customer_phone, new_state)

        # ── 8. Persist trace ──────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.state  = new_state
        self._memory_updater.update(db, ctx, decision, result, reply, stage_before, latency_ms)

        # ── 9. Structured turn trace (searchable in Railway logs) ─────────
        try:
            logger.info(
                "[BrainTurn] %s",
                json.dumps({
                    "tenant_id":     tenant_id,
                    "phone":         customer_phone[-4:] if len(customer_phone) >= 4 else "****",
                    "turn":          new_state.turn,
                    "message_len":   len(message),
                    # Intent layer
                    "intent":        intent.name,
                    "confidence":    round(intent.confidence, 2),
                    "slots":         intent.slots,
                    "method":        intent.extraction_method,
                    # State transition
                    "stage_before":  stage_before,
                    "stage_after":   new_state.stage,
                    "greeted":       new_state.greeted,
                    "product_focus": (new_state.current_product_focus or {}).get("title"),
                    "draft_order":   new_state.draft_order_id,
                    # Commerce facts snapshot
                    "facts": {
                        "products":      facts.product_count,
                        "in_stock":      getattr(facts, "in_stock_count", None),
                        "orderable":     getattr(facts, "orderable", facts.has_products and facts.has_active_integration),
                        "coupons":       facts.has_coupons,
                        "integration":   facts.has_active_integration,
                        "platform":      getattr(facts, "integration_platform", "unknown"),
                        "store":         facts.store_name,
                    },
                    # Decision layer
                    "action":           decision.action,
                    "reason":           decision.reason,
                    "policy_modified":  decision.reason != reason_before_policy,
                    # Execution + response
                    "exec_success":     result.success,
                    "exec_error":       result.error,
                    "response_mode":    "llm" if decision.action == "llm_reply" else "template",
                    "reply_len":        len(reply),
                    "latency_ms":       latency_ms,
                }, ensure_ascii=False),
            )
        except Exception:
            pass   # trace logging must never break the reply path

        return reply


# ── Factory ───────────────────────────────────────────────────────────────────

def build_default_brain() -> MerchantBrain:
    """Wire all Phase 2 default implementations together."""
    from .intent.classifier  import DefaultIntentClassifier
    from .state.store        import DefaultStateStore
    from .facts.commerce_facts import DefaultFactsLoader
    from .decision.engine    import DefaultDecisionEngine
    from .decision.policy    import RealPolicyGate
    from .execution.executor import DefaultActionExecutor
    from .compose.responder  import DefaultComposer
    from .memory.updater     import DefaultMemoryUpdater

    return MerchantBrain(
        classifier     = DefaultIntentClassifier(),
        state_store    = DefaultStateStore(),
        facts_loader   = DefaultFactsLoader(),
        decision_engine= DefaultDecisionEngine(),
        policy_gate    = RealPolicyGate(),    # Phase 2: real rules
        executor       = DefaultActionExecutor(),
        composer       = DefaultComposer(),
        memory_updater = DefaultMemoryUpdater(),
    )


# Module-level singleton — created lazily on first use
_brain_instance: Optional[MerchantBrain] = None


def get_brain() -> MerchantBrain:
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = build_default_brain()
    return _brain_instance
