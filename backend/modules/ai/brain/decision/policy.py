"""
brain/decision/policy.py
─────────────────────────
PolicyGate implementations.

PassThroughPolicyGate  — Phase 1 no-op (kept for testing / emergency bypass).
RealPolicyGate         — Phase 2 production gate with real business rules.

RealPolicyGate rules (applied in order, first match modifies decision):
  1. Working-hours gate   — if store has hours config and it's outside those
                            hours, downgrade order/payment actions to LLM_REPLY.
  2. Coupon frequency cap — block a second coupon within N hours (merchant-
                            configurable via ai_settings.coupon_cap_hours,
                            default 24 h).
  3. Price-range gate     — steer back to search when product exceeds budget.
  4. Max-order-value gate — block orders above merchant's configured ceiling
                            (ai_settings.max_order_value, 0 = unlimited).
  5. Auto-escalate        — transfer to human after N consecutive GENERAL turns
                            (ai_settings.auto_escalate_after_n, default 3).
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

    # Fallback constants (used when brain_profile is unavailable)
    _DEFAULT_COUPON_CAP_HOURS    = 24
    _DEFAULT_ESCALATE_AFTER_N    = 3

    def _brain_profile(self, ctx: BrainContext) -> dict:
        """Extract brain_profile from merchant_context (populated by build_merchant_context)."""
        mc = getattr(ctx, "merchant_context", None) or {}
        return mc.get("brain_profile", {})

    def gate(self, decision: Decision, ctx: BrainContext) -> Decision:
        try:
            decision = self._working_hours(decision, ctx)
            decision = self._coupon_cap(decision, ctx)
            decision = self._price_range(decision, ctx)
            decision = self._max_order_value(decision, ctx)
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

        # Merchant-configurable cap window (defaults to 24 h)
        bp = self._brain_profile(ctx)
        cap_hours = int(bp.get("coupon_cap_hours") or self._DEFAULT_COUPON_CAP_HOURS)
        cap_hours = max(1, cap_hours)

        try:
            from database.models import ConversationTrace
            cutoff = datetime.now(timezone.utc) - timedelta(hours=cap_hours)
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
                    "[PolicyGate] coupon already sent to %s within %dh — blocking",
                    ctx.customer_phone[-4:], cap_hours,
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"policy_reason": f"coupon_cap_{cap_hours}h"},
                    reason=f"policy: coupon already sent in last {cap_hours}h",
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

    # ── Rule 4: max order value gate ──────────────────────────────────────────

    def _max_order_value(self, decision: Decision, ctx: BrainContext) -> Decision:
        """Block CREATE_ORDER / PROPOSE_DRAFT_ORDER when product price exceeds
        the merchant's configured maximum order value (0 or None = unlimited)."""
        from .actions import ACTION_PROPOSE_DRAFT_ORDER, ACTION_SEARCH_PRODUCTS

        if decision.action not in (ACTION_PROPOSE_DRAFT_ORDER,):
            return decision

        bp = self._brain_profile(ctx)
        max_val = bp.get("max_order_value")
        if not max_val:
            return decision  # unlimited

        product = ctx.state.current_product_focus or {}
        product_price = product.get("price") or product.get("sale_price")

        try:
            if product_price and float(product_price) > float(max_val):
                logger.info(
                    "[PolicyGate] max_order_value: product %.2f > limit %.2f — blocking order",
                    float(product_price), float(max_val),
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"policy_reason": "product_above_max_order_value"},
                    reason=f"policy: product price {product_price} exceeds merchant max_order_value {max_val}",
                    confidence=decision.confidence,
                )
        except (TypeError, ValueError):
            pass

        return decision

    # ── Rule 5: auto-escalate on repeated confusion ───────────────────────────

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

        # Merchant-configurable threshold (defaults to 3)
        bp = self._brain_profile(ctx)
        escalate_n = int(bp.get("auto_escalate_after_n") or self._DEFAULT_ESCALATE_AFTER_N)
        escalate_n = max(1, escalate_n)

        # Use state.last_intent as a proxy: if N+ turns all general intent, escalate
        if ctx.state.turn >= escalate_n and ctx.state.last_intent == INTENT_GENERAL:
            logger.info(
                "[PolicyGate] auto-escalate: %d turns in GENERAL at stage=%s (threshold=%d)",
                ctx.state.turn, ctx.state.stage, escalate_n,
            )
            return Decision(
                action=ACTION_HANDOFF,
                args={"policy_reason": "repeated_confusion"},
                reason="policy: customer stuck in general intent — escalate to human",
                confidence=0.70,
            )
        return decision
