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
          → projected Brain state + SuggestionEngine
          → Composer.compose
          → StateStore.save
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
    BrainReplyState,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SalesContextSnapshot,
    SuggestionSnapshot,
    INTENT_GENERAL,
    INTENT_PICK_LIST_ITEM,
)
from .protocols import (
    IntentClassifier,
    StateStore,
    FactsLoader,
    DecisionMaker,
    PolicyGate,
    ActionExecutor,
    SuggestionEngine,
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
        suggestion_engine: Optional[SuggestionEngine] = None,
        sales_context_loader: Optional[Any] = None,
    ) -> None:
        self._classifier     = classifier
        self._state_store    = state_store
        self._facts_loader   = facts_loader
        self._decision_engine= decision_engine
        self._policy_gate    = policy_gate
        self._executor       = executor
        self._composer       = composer
        self._memory_updater = memory_updater
        if sales_context_loader is None:
            from .facts.sales_context import DefaultSalesContextLoader
            sales_context_loader = DefaultSalesContextLoader()
        self._sales_context_loader = sales_context_loader
        if suggestion_engine is None:
            from .suggestion.engine import DefaultSuggestionEngine
            suggestion_engine = DefaultSuggestionEngine()
        self._suggestion_engine = suggestion_engine

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
        tenant_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()

        # ── 0. Tenant isolation context (single source of truth for the turn) ─
        # Built once here. Every downstream layer (sales context loader,
        # handlers/runtime, memory updater, signal emitter) MUST reuse this
        # object instead of re-deriving the tenant id from raw inputs.
        from modules.ai.security import (
            TenantIsolationLayer,
            TenantIsolationViolation,
        )

        try:
            if tenant_context is not None:
                TenantIsolationLayer.assert_active(tenant_context)
                if int(tenant_context.tenant_id) != int(tenant_id):
                    raise TenantIsolationViolation(
                        f"tenant_context.tenant_id={tenant_context.tenant_id} "
                        f"does not match process(tenant_id={tenant_id})"
                    )
                tenant_ctx = tenant_context
            else:
                tenant_ctx = TenantIsolationLayer.make_context(
                    tenant_id      = tenant_id,
                    customer_phone = customer_phone,
                    customer_id    = customer_id,
                )
        except TenantIsolationViolation:
            # Hard error — never silently degrade tenant safety. The caller
            # surface (router / orchestrator) is responsible for translating
            # this into a user-facing error.
            raise

        # ── 1. Intent ────────────────────────────────────────────────────
        state_for_classify = self._state_store.load(db, tenant_id, customer_phone)

        # ── 1b. Infer greeted from history (catches proactive outbounds) ─
        # If ANY prior outbound exists in history (cart recovery template,
        # automation push, manual agent reply, even our own previous turn
        # when persistence didn't land for some reason), the customer has
        # already heard from us — never re-introduce. This is the catch-net
        # for paths that send messages outside the Brain pipeline and forget
        # to call StateStore.mark_greeted.
        if not state_for_classify.greeted and _history_has_outbound(history):
            logger.info(
                "[BrainPipeline] inferred greeted=True from history "
                "(prior outbound found) tenant=%s",
                tenant_id,
            )
            state_for_classify.greeted = True

        intent: Intent = await self._classifier.classify(message, history, state_for_classify)

        # ── 2. Load state + facts ─────────────────────────────────────────
        state: MerchantConversationState = state_for_classify
        facts: CommerceFacts             = self._facts_loader.load(db, tenant_id)

        # ── 3. Assemble context ───────────────────────────────────────────
        sales_context: SalesContextSnapshot = self._sales_context_loader.load(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            state=state,
            history=history,
            profile=profile,
            customer_id=customer_id,
            tenant_context=tenant_ctx,
        )
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
            sales_context  = sales_context,
            tenant_context = tenant_ctx,
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

        # ── 6. Project next state + suggestion snapshot ───────────────────
        new_state = self._state_store.transition(state, intent, decision)
        if result.data.get("checkout_url"):
            new_state.checkout_url  = result.data["checkout_url"]
            new_state.stage = "checkout"
        if result.data.get("order_id"):
            new_state.draft_order_id = str(result.data["order_id"])
        if result.data.get("product") and (
            decision.action == "search_products" or not new_state.current_product_focus
        ):
            new_state.current_product_focus = result.data["product"]
        if result.data.get("order_prep"):
            new_state.order_prep = OrderPreparationState.from_dict(result.data.get("order_prep"))

        # ── 6b. Persist search candidates so user can pick by number ─────────
        # IMPORTANT: the source of truth is the executor (search.py returns
        # `products`). The composer tags a narrower `pending_candidates`
        # for buttons, but it runs AFTER this block — so we must NOT rely on
        # it to populate state. This was the root cause of the production bug
        # where pick_list_item ("اخترت 2") fell through to the LLM because
        # state.last_search_candidates was always empty.
        _search_products = (
            result.data.get("pending_candidates")
            or result.data.get("products")
            or []
        )
        if decision.action == "search_products" and _search_products:
            # Cap to 8 so a pick like "5" is meaningful but state stays small.
            new_state.last_search_candidates = list(_search_products)[:8]
        elif intent.name == INTENT_PICK_LIST_ITEM:
            # Clear candidates after a successful pick (decision engine already
            # consumed the chosen product into ACTION_PROPOSE_DRAFT_ORDER).
            new_state.last_search_candidates = []

        new_state.customer_goal = _infer_customer_goal(intent, decision, state.customer_goal)
        ctx.state = new_state
        suggestion = self._suggestion_engine.suggest(ctx, decision, result)
        new_state.recent_messages = list((history or [])[-20:])
        new_state.conversation_summary = str(
            (sales_context.conversation_memory or {}).get("conversation_summary", "")
            or new_state.conversation_summary
            or ""
        )
        if result.data.get("recommended_products"):
            new_state.last_recommended_products = list(result.data["recommended_products"])
        elif sales_context.recommendations:
            new_state.last_recommended_products = list(sales_context.recommendations[:5])
        new_state.recommended_next_step = suggestion.suggested_next_step
        new_state.pending_action = suggestion.suggested_next_step or new_state.pending_action
        ctx.suggestion = suggestion
        _tenant_tone = ""
        _tenant_overlay = ""
        try:
            from modules.ai.prompts.tenant_overlay import (  # noqa: PLC0415
                get_tenant_tone, load_tenant_ai_overlay,
            )
            _tenant_tone = get_tenant_tone(db, tenant_id)
            _tenant_overlay = load_tenant_ai_overlay(db, tenant_id)
        except Exception:
            pass

        ctx.reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=state,
            current_state=new_state,
            suggestion=suggestion,
            decision=decision,
            tenant_tone=_tenant_tone,
            tenant_overlay=_tenant_overlay,
        )

        # ── 7. Compose reply ──────────────────────────────────────────────
        reply: str = await self._composer.compose(decision, result, ctx)

        asked_now = _infer_last_question(decision, result, suggestion)
        if asked_now:
            new_state.last_question_asked = asked_now
            new_state.last_question_answered = False
        else:
            new_state.last_question_asked = state.last_question_asked
            new_state.last_question_answered = True if state.last_question_asked else state.last_question_answered

        # ── 8. Persist state ───────────────────────────────────────────────
        self._state_store.save(db, tenant_id, customer_phone, new_state)

        # ── 9. Persist trace ──────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.state = new_state
        result.data.setdefault("chosen_path", _resolve_chosen_path(decision, result))
        self._memory_updater.update(db, ctx, decision, result, reply, stage_before, latency_ms)
        pending_buttons: List[Dict[str, Any]] = list(result.data.get("pending_buttons") or [])

        # ── 10. Structured turn trace (searchable in Railway logs) ────────
        try:
            logger.info(
                "[BrainTurn] %s",
                json.dumps({
                    "tenant_id":     tenant_id,
                    "phone":         customer_phone[-4:] if len(customer_phone) >= 4 else "****",
                    "turn":          new_state.turn,
                    "message_len":   len(message),
                    # Intent layer
                    "detected_intent": intent.name,
                    "confidence":    round(intent.confidence, 2),
                    "slots":         intent.slots,
                    "method":        intent.extraction_method,
                    # State transition
                    "stage_before":  stage_before,
                    "stage_after":   new_state.stage,
                    "greeted":       new_state.greeted,
                    "product_focus": (new_state.current_product_focus or {}).get("title"),
                    "draft_order":   new_state.draft_order_id,
                    "order_prep_missing": list(getattr(new_state.order_prep, "missing_fields", []) or []),
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
                    "action":             decision.action,
                    "chosen_path":        result.data.get("chosen_path"),
                    "reason":             decision.reason,
                    "policy_modified":    decision.reason != reason_before_policy,
                    "whether_coupon_logic_considered": suggestion.coupon_logic_considered,
                    "suggested_next_step": suggestion.suggested_next_step,
                    "customer_goal":      new_state.customer_goal,
                    "selected_product":   (new_state.current_product_focus or {}).get("title"),
                    "checkout_city":      getattr(new_state.order_prep, "city", ""),
                    "short_address_code": getattr(new_state.order_prep, "short_address_code", ""),
                    # Execution + response
                    "exec_success":     result.success,
                    "exec_error":       result.error,
                    "response_mode":    "llm" if result.data.get("chosen_path", "").startswith("llm") else "template",
                    "reply_len":        len(reply),
                    "latency_ms":       latency_ms,
                }, ensure_ascii=False),
            )
        except Exception:
            pass   # trace logging must never break the reply path

        return {"reply": reply, "buttons": pending_buttons}


# ── Brain state helpers ────────────────────────────────────────────────────────

def _infer_customer_goal(intent: Intent, decision: Decision, previous_goal: str = "") -> str:
    mapping = {
        "who_are_you": "understand_assistant_role",
        "greeting": "start_conversation",
        "ask_product": "discover_products",
        "ask_price": "evaluate_price",
        "start_order": "start_purchase",
        "pay_now": "complete_purchase",
        "ask_shipping": "understand_shipping",
        "ask_store_info": "understand_store_info",
        "ask_owner_contact": "contact_store",
        "track_order": "track_order",
        "talk_to_human": "reach_human_support",
        "hesitation": "resolve_purchase_hesitation",
    }
    if decision.action == "send_payment_link":
        return "complete_purchase"
    if decision.action == "handoff_to_human":
        return "reach_human_support"
    return mapping.get(intent.name, previous_goal or "general_help")


def _infer_last_question(
    decision: Decision,
    result: ActionResult,
    suggestion: SuggestionSnapshot,
) -> str:
    if decision.action == "clarify":
        return str(result.data.get("question") or "").strip()
    if suggestion.needs_follow_up_question:
        return str(suggestion.follow_up_question or "").strip()
    return ""


def _resolve_chosen_path(decision: Decision, result: ActionResult) -> str:
    chosen = str(result.data.get("chosen_path") or "").strip()
    if chosen:
        return chosen
    if decision.action == "llm_reply":
        return "llm"
    if decision.action in {"greet", "faq_reply", "clarify", "narrow_choices"}:
        return "rule"
    return "action"


def _build_reply_state(
    *,
    ctx: BrainContext,
    previous_state: MerchantConversationState,
    current_state: MerchantConversationState,
    suggestion: SuggestionSnapshot,
    decision: Decision,
    tenant_tone: str = "",
    tenant_overlay: str = "",
) -> BrainReplyState:
    recent_turns = []
    for turn in (ctx.history or [])[-4:]:
        body = str(turn.get("body") or "").strip()
        if not body:
            continue
        role = "customer" if turn.get("direction") == "in" else "assistant"
        recent_turns.append(f"{role}: {body}")

    sensitivity_score = float(ctx.profile.get("price_sensitivity_score") or 0.5)
    selected_product = current_state.current_product_focus or None

    known_facts = {
        "store_name": ctx.facts.store_name,
        "store_url": ctx.facts.store_url,
        "has_products": ctx.facts.has_products,
        "product_count": ctx.facts.product_count,
        "in_stock_count": ctx.facts.in_stock_count,
        "orderable": ctx.facts.orderable,
        "shipping_policy": ctx.facts.shipping_policy,
        "shipping_methods": ctx.facts.shipping_methods,
        "shipping_notes": ctx.facts.shipping_notes,
        "support_hours": ctx.facts.support_hours,
        "contact_phone": ctx.facts.store_contact_phone,
        "contact_email": ctx.facts.store_contact_email,
        "checkout_preparation": current_state.order_prep.to_dict(),
    }

    effective_tone = tenant_tone or str(ctx.profile.get("communication_style") or "neutral")

    return BrainReplyState(
        store_name=ctx.facts.store_name,
        tone=effective_tone,
        stage=current_state.stage,
        customer_goal=current_state.customer_goal,
        selected_product=selected_product,
        price_sensitivity=_price_sensitivity_label(sensitivity_score),
        known_facts=known_facts,
        last_question_asked=previous_state.last_question_asked,
        last_question_answered=previous_state.last_question_answered,
        recommended_next_step=suggestion.suggested_next_step or current_state.recommended_next_step,
        coupon_policy={
            "has_coupons": ctx.facts.has_coupons,
            "eligible_code": ctx.facts.coupon_eligibility,
            "discount_ok_now": suggestion.discount_ok_now,
            "coupon_logic_considered": suggestion.coupon_logic_considered,
        },
        recent_turns=recent_turns,
        policy_reason=str(decision.args.get("policy_reason") or ""),
        conversation_summary=current_state.conversation_summary,
        store_knowledge=(ctx.sales_context.store_profile if ctx.sales_context else {}),
        customer_memory={
            **(ctx.sales_context.customer_profile if ctx.sales_context else {}),
            **(ctx.sales_context.customer_preferences if ctx.sales_context else {}),
        },
        last_recommended_products=list(current_state.last_recommended_products or []),
        tenant_overlay=tenant_overlay,
        explicit_pending_action=current_state.pending_action,
        intent_name=getattr(ctx.intent, "name", "") or "",
        response_goal=_compose_response_goal(decision, suggestion),
    )


def _compose_response_goal(decision: Decision, suggestion: SuggestionSnapshot) -> str:
    """Single-line summary of WHY this turn is being composed.

    Surfaced to the LLM in the operating-rules block so the model knows
    what success looks like instead of inferring it from the message and
    the persona. Kept short so it fits in the prompt without bloating it.
    """
    parts: List[str] = []
    if decision.reason:
        parts.append(decision.reason.strip())
    nxt = (suggestion.suggested_next_step or "").strip() if suggestion else ""
    if nxt and nxt not in (decision.reason or ""):
        parts.append(f"next_step={nxt}")
    if suggestion and suggestion.needs_follow_up_question and suggestion.follow_up_question:
        parts.append(f"ask_one={suggestion.follow_up_question.strip()}")
    return " | ".join(parts) or "advance the conversation toward the next sales step"


def _history_has_outbound(history: List[Dict[str, Any]]) -> bool:
    """True when at least one prior outbound message exists in history.

    The webhook persists the inbound message BEFORE calling Brain, but
    only outbound rows count as "we already greeted" — an inbound from
    the customer obviously doesn't.
    """
    for turn in (history or []):
        direction = str(turn.get("direction") or "").strip().lower()
        if direction in {"out", "outbound"}:
            body = str(turn.get("body") or "").strip()
            if body:
                return True
    return False


def _price_sensitivity_label(score: float) -> str:
    if score < 0.25:
        return "منخفضة"
    if score < 0.5:
        return "متوسطة"
    if score < 0.75:
        return "مرتفعة"
    return "مرتفعة جداً"


# ── Factory ───────────────────────────────────────────────────────────────────

def build_default_brain() -> MerchantBrain:
    """Wire all Phase 2 default implementations together.

    The decision engine is wrapped (left-to-right) by:
      1. ``DefaultDecisionEngine`` — the rule / state machine engine.
      2. ``PolicyOverrideLayer``   — Phase 1.7, attaches a non-binding
         ``policy_hint`` to ``decision.args``.  No-op when no learned
         policy exists for the current ``(intent, industry)``.
      3. ``PolicyBiasLayer``       — Phase 1.9, narrowly mutates
         ``decision.args`` (UI / choice_count / recommendation_style)
         for non-protected actions.  Hard-disabled by default — the
         master switch ``LEARNED_POLICY_BIAS_ENABLED`` must be flipped
         on for this layer to do anything.

    Both decorators are *defense-in-depth*: failure inside any of them
    falls back to the inner decision unchanged, so wiring them by
    default carries zero behavioral risk for a fresh deploy.
    """
    from .intent.classifier  import DefaultIntentClassifier
    from .state.store        import DefaultStateStore
    from .facts.commerce_facts import DefaultFactsLoader
    from .decision.engine    import DefaultDecisionEngine
    from .decision.policy    import RealPolicyGate
    from .execution.executor import DefaultActionExecutor
    from .compose.responder  import DefaultComposer
    from .memory.updater     import DefaultMemoryUpdater
    from .suggestion.engine  import DefaultSuggestionEngine
    from .facts.sales_context import DefaultSalesContextLoader

    # Build the layered decision engine.  Imports are inside the
    # function so that the brain package can be loaded even when the
    # learning subpackage is absent (e.g. older test fixtures).
    decision_engine: Any = DefaultDecisionEngine()
    try:
        from modules.ai.learning import PolicyBiasLayer, PolicyOverrideLayer
        decision_engine = PolicyOverrideLayer(decision_engine)
        decision_engine = PolicyBiasLayer(decision_engine)
    except Exception:
        # If the learning package fails to import we silently keep the
        # bare engine — the rest of the brain must still work.
        pass

    return MerchantBrain(
        classifier     = DefaultIntentClassifier(),
        state_store    = DefaultStateStore(),
        facts_loader   = DefaultFactsLoader(),
        decision_engine= decision_engine,
        policy_gate    = RealPolicyGate(),    # Phase 2: real rules
        executor       = DefaultActionExecutor(),
        composer       = DefaultComposer(),
        memory_updater = DefaultMemoryUpdater(),
        suggestion_engine = DefaultSuggestionEngine(),
        sales_context_loader = DefaultSalesContextLoader(),
    )


# Module-level singleton — created lazily on first use
_brain_instance: Optional[MerchantBrain] = None


def get_brain() -> MerchantBrain:
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = build_default_brain()
    return _brain_instance
