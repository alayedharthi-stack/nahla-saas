"""
brain/decision/policy.py
─────────────────────────
PolicyGate implementations.

PassThroughPolicyGate  — Phase 1 no-op (kept for testing / emergency bypass).
RealPolicyGate         — Phase 2 production gate with real business rules.

RealPolicyGate rules (applied in order, first match modifies decision):
  1. Working-hours gate  — if store has hours config and it's outside those
                           hours, downgrade order/payment actions to LLM_REPLY
                           with a "closed" context so it can apologise.
  2. Coupon frequency cap — if a coupon was sent to this customer within 24 h
                            and a new suggest_coupon is requested, block it.
  3. Price-range gate     — if slots carry a price_range and the product in
                            focus is outside that range, steer back to search.
  4. Auto-escalate        — if the customer has sent INTENT_GENERAL 3 times
                            in a row and is still in STAGE_DISCOVERY, upgrade
                            to ACTION_HANDOFF so a human can step in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..types import BrainContext, Decision, INTENT_GENERAL
from .actions import (
    ACTION_LLM_REPLY,
    ACTION_HANDOFF,
    ACTION_SUGGEST_COUPON,
)

logger = logging.getLogger("nahla.brain.policy")


# ── Phase 1 pass-through ──────────────────────────────────────────────────────

class PassThroughPolicyGate:
    """Implements PolicyGate protocol — Phase 1 no-op."""

    def gate(self, decision: Decision, ctx: BrainContext) -> Decision:
        return decision


# ── Phase 2 real gate ─────────────────────────────────────────────────────────

class RealPolicyGate:
    """
    Implements PolicyGate protocol — enforces merchant business rules.

    Inject via build_default_brain() to activate.  The gate NEVER raises —
    any failure returns the original decision unchanged (fail-open).
    """

    # Number of consecutive GENERAL turns before auto-escalating
    ESCALATE_AFTER_N_GENERAL = 3

    def gate(self, decision: Decision, ctx: BrainContext) -> Decision:
        try:
            decision = self._working_hours(decision, ctx)
            decision = self._coupon_cap(decision, ctx)
            decision = self._price_range(decision, ctx)
            decision = self._auto_escalate(decision, ctx)
        except Exception as exc:
            logger.warning("[PolicyGate] unexpected error: %s — returning original decision", exc)
        return decision

    # ── Rule 1: working hours ─────────────────────────────────────────────────
    # Orders and payment links are ALWAYS allowed regardless of working hours —
    # the store is online and processes orders asynchronously. The merchant is
    # happy to receive orders at any time.
    # Working-hours gate only applies to live-human actions (ACTION_HANDOFF).
    # If the store has a human-support team with limited hours, we don't want
    # to promise immediate human response when no one is available.

    def _working_hours(self, decision: Decision, ctx: BrainContext) -> Decision:
        if ctx.facts.within_working_hours is False and decision.action == ACTION_HANDOFF:
            logger.info(
                "[PolicyGate] outside working hours — handoff not available, routing to llm_reply",
                decision.action,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={"policy_reason": "outside_working_hours_handoff"},
                reason="policy: human support not available outside working hours — LLM apologises",
                confidence=decision.confidence,
            )
        return decision

    # ── Rule 2: coupon frequency cap ──────────────────────────────────────────

    def _coupon_cap(self, decision: Decision, ctx: BrainContext) -> Decision:
        if decision.action != ACTION_SUGGEST_COUPON:
            return decision

        db = getattr(ctx, "_db", None)
        if not db:
            return decision

        try:
            from database.models import ConversationTrace
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            recent = (
                db.query(ConversationTrace)
                .filter(
                    ConversationTrace.tenant_id == ctx.tenant_id,
                    ConversationTrace.customer_phone == ctx.customer_phone,
                    ConversationTrace.response_type == ACTION_SUGGEST_COUPON,
                    ConversationTrace.created_at >= cutoff,
                )
                .first()
            )
            if recent:
                logger.info(
                    "[PolicyGate] coupon already sent to %s within 24h — blocking",
                    ctx.customer_phone[-4:],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"policy_reason": "coupon_cap_24h"},
                    reason="policy: coupon already sent in last 24h",
                    confidence=decision.confidence,
                )
        except Exception as exc:
            logger.debug("[PolicyGate._coupon_cap] error: %s", exc)

        return decision

    # ── Rule 3: price-range gate ──────────────────────────────────────────────

    def _price_range(self, decision: Decision, ctx: BrainContext) -> Decision:
        from .actions import ACTION_PROPOSE_DRAFT_ORDER, ACTION_SEARCH_PRODUCTS

        if decision.action != ACTION_PROPOSE_DRAFT_ORDER:
            return decision

        price_range = ctx.intent.slots.get("price_range", {})
        max_price   = price_range.get("max")
        product     = ctx.state.current_product_focus or {}
        product_price = product.get("price") or product.get("sale_price")

        if max_price and product_price and float(product_price) > float(max_price):
            logger.info(
                "[PolicyGate] product price %.2f exceeds slot max %.2f — steering to search",
                float(product_price), float(max_price),
            )
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={
                    "query": ctx.intent.slots.get("product_query", ctx.message),
                    "price_max": max_price,
                    "policy_reason": "product_above_price_range",
                },
                reason="policy: product above customer's stated price range — search cheaper options",
                confidence=0.80,
            )
        return decision

    # ── Rule 4: auto-escalate on repeated confusion ───────────────────────────

    def _auto_escalate(self, decision: Decision, ctx: BrainContext) -> Decision:
        from ..state.stages import STAGE_DISCOVERY, STAGE_EXPLORING

        if decision.action == ACTION_HANDOFF:
            return decision   # already escalating

        if ctx.state.stage not in (STAGE_DISCOVERY, STAGE_EXPLORING):
            return decision

        if ctx.intent.name != INTENT_GENERAL:
            return decision

        # Count consecutive GENERAL intents in history
        general_streak = 0
        for turn in reversed(ctx.history[-6:]):
            if turn.get("direction") != "in":
                continue
            # We don't have intent per history turn yet — use last_intent from state
            break   # Phase 2 stub — needs intent stored per turn in memory updater

        # Use state.last_intent as a proxy: if 3+ turns all general intent, escalate
        if ctx.state.turn >= self.ESCALATE_AFTER_N_GENERAL and ctx.state.last_intent == INTENT_GENERAL:
            logger.info(
                "[PolicyGate] auto-escalate: %d turns in GENERAL at stage=%s",
                ctx.state.turn, ctx.state.stage,
            )
            return Decision(
                action=ACTION_HANDOFF,
                args={"policy_reason": "repeated_confusion"},
                reason="policy: customer stuck in general intent — escalate to human",
                confidence=0.70,
            )
        return decision
