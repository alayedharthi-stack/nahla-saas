"""
brain/decision/policy.py
─────────────────────────
PolicyGate implementations.

PassThroughPolicyGate  — Phase 1 no-op (kept for testing / emergency bypass).
RealPolicyGate         — Phase 2 production gate with real business rules.

RealPolicyGate rules (applied in order, first match modifies decision):
  0. Block list           — silently hand off blocked/abusive customer phones
                            (store_settings.blocked_customers list).
  1. Working-hours gate   — if store has hours config and it's outside those
                            hours, downgrade order/payment actions to LLM_REPLY.
  2. Coupon frequency cap — block a second coupon within N hours (merchant-
                            configurable via ai_settings.coupon_cap_hours,
                            default 24 h).
  3. Price-range gate     — steer back to search when product exceeds budget.
  4. Max-order-value gate — block orders above merchant's configured ceiling
                            (ai_settings.max_order_value, 0 = unlimited).
  5. Auto-escalate        — opt-in only. Transfers to human ONLY when:
                              (a) merchant has explicitly enabled it via
                                  ai_settings.auto_escalate_enabled = True, AND
                              (b) general_streak >= ai_settings.auto_escalate_after_n, AND
                              (c) the customer's last message carries an explicit
                                  frustration / unmet-need signal (e.g. "ما فهمت",
                                  "كلموني", "تواصلوا معي", "موظف", "إنسان").
                            Plain consecutive GENERAL turns (small talk, jokes,
                            unusual product questions) NEVER trigger handoff —
                            the brain handles them through the LLM. Default OFF.
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
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_RECOMMEND_ADDON,
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
            decision = self._block_list(decision, ctx)
            decision = self._working_hours(decision, ctx)
            decision = self._coupon_cap(decision, ctx)
            decision = self._price_range(decision, ctx)
            decision = self._max_order_value(decision, ctx)
            decision = self._auto_escalate(decision, ctx)
            # Human-Priority clamp MUST run last — it always reads the final
            # action a previous rule may have produced. Putting it first
            # would let a later rule (e.g. auto-escalate) re-introduce a
            # sales-heavy action after we already softened it.
            decision = self._human_priority_clamp(decision, ctx)
        except Exception as exc:
            logger.warning("[PolicyGate] unexpected error: %s — returning original decision", exc)
        return decision

    # ── Rule 6 (LAST): human-priority clamp ───────────────────────────────────

    # Actions the AI is NOT allowed to take while the customer is waiting on
    # a human reply. These are the "aggressive sales" / "transactional" paths
    # — they compete with the agent or push the conversation forward in a
    # direction the agent might want to handle differently. We deliberately
    # KEEP ACTION_HANDOFF in here as an allowed action (it's still useful
    # to escalate further e.g. to a supervisor), and KEEP ACTION_LLM_REPLY
    # / ACTION_FAQ_REPLY / ACTION_SEARCH_PRODUCTS / ACTION_GREET / clarify /
    # narrow which are informational and safe.
    _HUMAN_PRIORITY_BLOCKED_ACTIONS = frozenset({
        ACTION_PROPOSE_DRAFT_ORDER,
        ACTION_SEND_PAYMENT_LINK,
        ACTION_SUGGEST_COUPON,
        ACTION_RECOMMEND_ADDON,
    })

    def _human_priority_clamp(self, decision: Decision, ctx: BrainContext) -> Decision:
        """Clamp the AI to non-aggressive, non-transactional actions while
        the customer is in a "human requested but not picked up yet" state.

        Behaviour matrix:
          * ``ctx.human_priority is False`` → no-op.
          * Action is in :data:`_HUMAN_PRIORITY_BLOCKED_ACTIONS` → downgrade
            to :data:`ACTION_LLM_REPLY` and stamp
            ``args['human_priority']=True`` so the composer knows to add
            a brief reassurance line and skip any "shall I prepare the
            order?" closer.
          * Action is anything else (greet, FAQ, search, clarify, llm_reply,
            handoff, …) → keep as-is but still stamp
            ``args['human_priority']=True`` so the composer adds the
            reassurance suffix.

        Why "stamp + keep" instead of "downgrade everything to llm_reply":
        the customer may still legitimately ask a factual question ("هل
        المنتج متوفر؟") and downgrading every action would force the LLM
        to re-derive answers the executor already produced. We want the
        AI to KEEP being helpful — just not closing the sale.
        """
        if not getattr(ctx, "human_priority", False):
            return decision

        original_action = decision.action
        new_args = dict(decision.args or {})
        new_args["human_priority"] = True

        if original_action in self._HUMAN_PRIORITY_BLOCKED_ACTIONS:
            logger.info(
                "[HUMAN_PRIORITY] clamp tenant=%s phone=%s action_in=%s → llm_reply "
                "reason=block_aggressive_sales",
                ctx.tenant_id, (ctx.customer_phone or "")[-4:], original_action,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args=new_args,
                reason=(
                    f"human_priority:clamp original={original_action} — "
                    "customer is waiting on a human; switch to informational "
                    "reply only, no sales push, no payment link, no coupon"
                ),
                confidence=decision.confidence,
            )

        logger.info(
            "[HUMAN_PRIORITY] clamp tenant=%s phone=%s action=%s pass=true "
            "(reassurance suffix will be appended by composer)",
            ctx.tenant_id, (ctx.customer_phone or "")[-4:], original_action,
        )
        return Decision(
            action=original_action,
            args=new_args,
            reason=decision.reason,
            confidence=decision.confidence,
        )

    # ── Rule 0: block list ────────────────────────────────────────────────────

    def _block_list(self, decision: Decision, ctx: BrainContext) -> Decision:
        """Silently hand off customers whose phone number is in the merchant's
        block list (store_settings.blocked_customers).

        Blocked customers still receive a response (we hand off to human
        monitoring rather than going silent), but the AI stops engaging.
        """
        if decision.action == ACTION_HANDOFF:
            return decision  # already heading to human

        bp = self._brain_profile(ctx)
        blocked: list = bp.get("blocked_customers") or []
        if not blocked:
            return decision

        phone = str(ctx.customer_phone or "").strip()
        if not phone:
            return decision

        # Normalise for comparison: strip spaces/dashes, lower-case.
        def _norm(p: str) -> str:
            return "".join(c for c in p if c.isdigit() or c == "+")

        phone_norm = _norm(phone)
        for entry in blocked:
            if phone_norm and _norm(str(entry)) == phone_norm:
                logger.info(
                    "[PolicyGate] block_list: customer %s is blocked — routing to handoff",
                    phone[-4:],
                )
                return Decision(
                    action=ACTION_HANDOFF,
                    args={"policy_reason": "customer_blocked"},
                    reason="policy: customer phone is on merchant's block list",
                    confidence=1.0,
                )
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
            # Off-hours change (May 2026):
            # Previously this rule silently downgraded HANDOFF → LLM_REPLY,
            # which meant the customer got an apology line BUT the
            # conversation never got pinned to a human in the inbox.
            # Merchants then woke up to "we had three customers asking
            # for support overnight and saw nothing in the dashboard"
            # complaints. The handoff request itself is too valuable to
            # drop — we now keep ACTION_HANDOFF (so the webhook creates
            # the HandoffSession + raises needs_human / handoff_active)
            # and tag ``args["after_hours"]=True`` so the responder can
            # ship the polite "the team will reply during work hours"
            # variant instead of the regular "I'll alert the team now"
            # acknowledgement.
            logger.info(
                "[PolicyGate] outside working hours — handoff registered "
                "with after_hours=True (was: downgraded to llm_reply)"
            )
            return Decision(
                action=ACTION_HANDOFF,
                args={**(decision.args or {}), "after_hours": True,
                      "policy_reason": "outside_working_hours_handoff"},
                reason=(decision.reason or "")
                       + " | policy: after_hours — keep handoff but use polite copy",
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
    #
    # IMPORTANT: this used to fire on `general_streak >= 3` ALONE, which meant a
    # customer who simply joked, made small talk, or asked unusual product
    # questions ("متى انتاجه؟", a couple of laughing emojis, etc.) would silently
    # be handed off to a human after three turns — and the customer would see
    # "وصل طلبك! سأعيد توجيهك لفريق الدعم..." even though no order existed.
    #
    # The new contract is:
    #
    #   * OFF by default. The merchant must explicitly opt-in via
    #     ``ai_settings.auto_escalate_enabled = True``.
    #   * Even when enabled, a high general_streak isn't enough. The customer's
    #     last message must carry a real escalation signal (frustration,
    #     unmet-need, an explicit ask for a person).
    #
    # If neither condition holds we leave the decision alone and let the brain
    # / LLM handle the conversation. Handoff stays available as an explicit
    # path through INTENT_TALK_HUMAN (rules.py) — which is exactly how a real
    # customer asks for a human.

    # Lower-cased substrings that indicate the customer wants out of the AI
    # conversation. Kept deliberately small + unambiguous: a casual mention of
    # the words inside an unrelated sentence is rare in practice and the
    # consequences of a false positive (silent handoff) are bad UX.
    _ESCALATION_SIGNALS_AR = (
        "موظف", "إنسان", "انسان", "بشري", "كلموني", "اتصلوا بي",
        "تواصلوا معي", "محد رد", "ما حد رد", "ما فهمت", "مو فاهم",
        "مش فاهم", "غير واضح", "خدمة العملاء", "اتكلم مع",
    )
    _ESCALATION_SIGNALS_EN = (
        "human agent", "real person", "speak to someone", "talk to someone",
        "customer service", "no one is answering", "i don't understand",
    )

    @classmethod
    def _has_escalation_signal(cls, message: str) -> bool:
        if not message:
            return False
        m = message.strip().lower()
        if not m:
            return False
        for token in cls._ESCALATION_SIGNALS_AR:
            if token in m:
                return True
        for token in cls._ESCALATION_SIGNALS_EN:
            if token in m:
                return True
        return False

    def _auto_escalate(self, decision: Decision, ctx: BrainContext) -> Decision:
        from ..state.stages import STAGE_DISCOVERY, STAGE_EXPLORING

        if decision.action == ACTION_HANDOFF:
            return decision   # already escalating

        if ctx.state.stage not in (STAGE_DISCOVERY, STAGE_EXPLORING):
            return decision

        if ctx.intent.name != INTENT_GENERAL:
            return decision

        bp = self._brain_profile(ctx)
        # Strict opt-in: missing/false flag means "never auto-escalate".
        if not bool(bp.get("auto_escalate_enabled")):
            return decision

        escalate_n_raw = bp.get("auto_escalate_after_n")
        try:
            escalate_n = int(escalate_n_raw) if escalate_n_raw is not None else self._DEFAULT_ESCALATE_AFTER_N
        except (TypeError, ValueError):
            escalate_n = self._DEFAULT_ESCALATE_AFTER_N
        escalate_n = max(1, escalate_n)

        general_streak = getattr(ctx.state, "general_streak", 0) or 0
        if general_streak < escalate_n:
            return decision

        # Even with the opt-in flag, require an explicit escalation signal in
        # the customer's most recent message. A long banter session is NOT a
        # reason to hand off — handoff has to be the customer's choice or the
        # outcome of an obvious failure mode.
        last_message = (getattr(ctx, "message", "") or "")
        if not self._has_escalation_signal(last_message):
            logger.info(
                "[PolicyGate] auto-escalate SUPPRESSED | streak=%d threshold=%d "
                "stage=%s reason=no_escalation_signal_in_message",
                general_streak, escalate_n, ctx.state.stage,
            )
            return decision

        logger.info(
            "[PolicyGate] auto-escalate FIRED | streak=%d threshold=%d stage=%s "
            "trigger=explicit_signal",
            general_streak, escalate_n, ctx.state.stage,
        )
        return Decision(
            action=ACTION_HANDOFF,
            args={"policy_reason": "repeated_confusion_with_signal"},
            reason=f"policy: {general_streak} GENERAL intents + explicit escalation signal",
            confidence=0.70,
        )
