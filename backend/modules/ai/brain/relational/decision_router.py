"""
brain/relational/decision_router.py
───────────────────────────────────
Commit 2 of the Tenant 33 #49 Conversational Commerce Architecture
rollout: a narrow, kill-switchable post-processor that lets the
``ConversationMoment`` from Commit 1 nudge the decision a soft step
before it reaches the responder.

Strict invariants pinned by the merchant directive
──────────────────────────────────────────────────
  1. SOFT preference, never hard override. The router consults a
     small (action, moment) → (preferred_action, response_goal_token)
     table and returns either the decision unchanged OR a new
     ``Decision`` with two tweaks:
       * ``decision.action`` swapped to a softer alternative
         (always ``ACTION_LLM_REPLY`` in the current table).
       * ``decision.args["preferred_response_goal"]`` set to a
         stable token the brain prompt-builder reads as a prefix
         on the existing ``response_goal``.
     NO other field of ``Decision`` is mutated.

  2. NO state mutation. The router does not touch
     ``ctx.state`` / ``ctx.profile`` / ``ctx.merchant_context``.
     It does not call ``apply_state_patch`` or any persistence
     symbol. Pure.

  3. NO safety-net suppression. That is Commit 3's scope.

  4. NO new prompt block. ``preferred_response_goal`` is a TOKEN
     (e.g. ``"complaint_recovery_shipping_delay"``) read by the
     existing ``response_goal`` builder — there is no new CX
     overlay in the JSON the LLM receives.

  5. NEVER fabricates business state. The forbidden-token list
     from :mod:`relational.contracts` is enforced by an
     architectural test against the router's source.

  6. Two independent flags:
       * ``RELATIONAL_LAYER_ENABLED``           — Commit 1 (compute + log).
       * ``RELATIONAL_DECISION_ROUTER_ENABLED`` — Commit 2 (this module).
     Either flag can be turned off independently. The router is
     inert (returns ``decision`` unchanged) if either:
       * the router flag is off, OR
       * ``ctx.relational_state`` is ``None`` / inert / moment is
         ``ConversationMoment.NONE``, OR
       * no rule fires for the (action, moment) pair.

Routing table (stable, audit-friendly)
──────────────────────────────────────
Each rule is the smallest possible re-route to close ONE production
bug from the May 2026 Tenant 33 audit. Adding a rule is a deliberate
commit + a regression test — the table is intentionally short.

  PRAISE_POST_DELIVERY:
    (ACTION_TRACK_ORDER, …)  → (ACTION_LLM_REPLY, "appreciation_acknowledgment")
        Closes the "customer praises delivery → bot runs lookup →
        'no orders for your number'" bug.

  COMPLAINT_SHIPPING_DELAY:
    (ACTION_HANDOFF, …)      → (ACTION_LLM_REPLY, "complaint_recovery_shipping_delay")
        Closes the "Hajj delay complaint → flat handoff ACK" bug.
        ``needs_human`` is set independently by the webhook's
        complaint detector — not the router's responsibility.

  COMPLAINT_PRODUCT_QUALITY:
    (ACTION_HANDOFF, …)      → (ACTION_LLM_REPLY, "complaint_recovery_product_quality")
        Same shape as the shipping rule, different goal token so
        the brain knows the axis.

  COMPLAINT_GENERIC:
    (ACTION_HANDOFF, …)      → (ACTION_LLM_REPLY, "complaint_recovery_generic")

  CONCERN_PRE_PURCHASE:
    (ACTION_SUGGEST_COUPON, …)→ (ACTION_LLM_REPLY, "trust_building")
        Closes the "first-time hesitation → instant coupon push"
        bug — coupons land emotionally wrong before trust exists.

Anything not in the table returns the decision UNCHANGED.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

from ..decision.actions import (
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from ..types import Decision
from .moments import ConversationMoment
from .state import RelationalState

logger = logging.getLogger("nahla.relational.router")


# ── Stable response-goal tokens. These are READ by the brain prompt
#    builder via ``decision.args["preferred_response_goal"]``. They
#    are TOKENS (not prose) — the brain composes the wording itself.
RESPONSE_GOAL_APPRECIATION_ACK             = "appreciation_acknowledgment"
RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING  = "complaint_recovery_shipping_delay"
RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT   = "complaint_recovery_product_quality"
RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC   = "complaint_recovery_generic"
RESPONSE_GOAL_TRUST_BUILDING               = "trust_building"


# ── The routing table. Closed and short on purpose.
#
# Key:    (current_action, moment)
# Value:  (preferred_action, preferred_response_goal_token)
#
# A ``None`` preferred_action means "don't re-route the action,
# only set the preferred goal token".
_RoutingValue = Tuple[Optional[str], str]
_RELATIONAL_ROUTING_TABLE: Dict[Tuple[str, ConversationMoment], _RoutingValue] = {
    # Praise post-delivery — never run ``track_order`` lookup on a
    # praise turn. The dry "no orders found for your number" path
    # is the exact production bug we are closing.
    (ACTION_TRACK_ORDER, ConversationMoment.PRAISE_POST_DELIVERY): (
        ACTION_LLM_REPLY, RESPONSE_GOAL_APPRECIATION_ACK,
    ),
    # Praise post-delivery + LLM_REPLY already chosen — leave the
    # action, just hint the goal so the brain frames warmly.
    # (Action stays the same; goal flips.)
    # NOTE: omitted from table because :func:`apply_relational_preference`
    # also tags the goal when no action change is needed; see the
    # default-tag branch below.

    # Complaints arriving as ``ACTION_HANDOFF`` — re-route so the
    # brain composes empathically. ``needs_human`` is independently
    # raised by the webhook complaint detector (per #46 policy).
    (ACTION_HANDOFF, ConversationMoment.COMPLAINT_SHIPPING_DELAY): (
        ACTION_LLM_REPLY, RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING,
    ),
    (ACTION_HANDOFF, ConversationMoment.COMPLAINT_PRODUCT_QUALITY): (
        ACTION_LLM_REPLY, RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT,
    ),
    (ACTION_HANDOFF, ConversationMoment.COMPLAINT_GENERIC): (
        ACTION_LLM_REPLY, RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC,
    ),

    # Pre-purchase concern — never lead with a coupon. Trust comes
    # before discount.
    (ACTION_SUGGEST_COUPON, ConversationMoment.CONCERN_PRE_PURCHASE): (
        ACTION_LLM_REPLY, RESPONSE_GOAL_TRUST_BUILDING,
    ),
}


# ── Default goal tags when the action is already the preferred one.
# These do NOT change the action — they only tag ``preferred_response_goal``
# so the brain prompt builder reads the relational frame on top of
# the existing goal text. Strictly additive.
_DEFAULT_GOAL_TAGS: Dict[ConversationMoment, str] = {
    ConversationMoment.PRAISE_POST_DELIVERY:      RESPONSE_GOAL_APPRECIATION_ACK,
    ConversationMoment.COMPLAINT_SHIPPING_DELAY:  RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING,
    ConversationMoment.COMPLAINT_PRODUCT_QUALITY: RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT,
    ConversationMoment.COMPLAINT_GENERIC:         RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC,
    ConversationMoment.CONCERN_PRE_PURCHASE:      RESPONSE_GOAL_TRUST_BUILDING,
}


# ── Kill switch ─────────────────────────────────────────────────────


def is_decision_router_enabled() -> bool:
    """``True`` when ``RELATIONAL_DECISION_ROUTER_ENABLED`` is set
    to a truthy value. Independent from ``RELATIONAL_LAYER_ENABLED``
    so operators can run telemetry-only mode (Commit 1) without the
    router (Commit 2). Both default OFF — staged rollout per
    merchant directive."""
    raw = (os.environ.get("RELATIONAL_DECISION_ROUTER_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── Public function ────────────────────────────────────────────────


def apply_relational_preference(decision: Decision, ctx: Any) -> Decision:
    """Return a (possibly new) ``Decision`` with a relational
    preference applied. NEVER raises. NEVER mutates inputs.

    Behaviour matrix
    ────────────────
        flag off                              -> decision unchanged
        no ``ctx.relational_state``           -> decision unchanged
        ``relational_state.is_inert()``       -> decision unchanged
        moment == NONE                        -> decision unchanged
        no rule matches + no default tag      -> decision unchanged
        rule matches                          -> new Decision with
                                                  swapped action +
                                                  preferred_response_goal
        only default tag matches              -> new Decision with
                                                  same action +
                                                  preferred_response_goal

    A new ``Decision`` instance is always returned via
    ``dataclasses.replace`` — the input is never mutated, so
    callers can safely log the before/after.
    """
    if decision is None:
        return decision  # type: ignore[return-value]
    try:
        if not is_decision_router_enabled():
            return decision
        rs: Optional[RelationalState] = getattr(ctx, "relational_state", None)
        if rs is None:
            return decision
        if not isinstance(rs, RelationalState):
            return decision
        if rs.moment == ConversationMoment.NONE:
            return decision

        current_action = str(decision.action or "")
        moment = rs.moment

        # Look for a re-route rule first.
        rule = _RELATIONAL_ROUTING_TABLE.get((current_action, moment))
        if rule is not None:
            preferred_action, goal_token = rule
            new_action = preferred_action or current_action
            new_args = dict(decision.args or {})
            # NEVER touch business-state args: the brain prompt
            # builder reads ``preferred_response_goal``; everything
            # else is left untouched so the rest of the decision
            # (e.g. selected product, checkout_url) stays intact.
            new_args["preferred_response_goal"] = goal_token
            new_args["relational_routing_applied"] = True
            new_args["relational_moment"] = str(moment.value)
            new_reason = (
                f"{decision.reason or ''} | "
                f"relational_router={moment.value}->{new_action}:{goal_token}"
            ).strip(" |")
            new_decision = replace(
                decision,
                action=new_action,
                args=new_args,
                reason=new_reason,
            )
            _log_router_change(
                ctx=ctx,
                moment=moment,
                before_action=current_action,
                after_action=new_action,
                goal_token=goal_token,
                kind="rule",
            )
            return new_decision

        # No re-route rule fired. If the moment has a default tag,
        # apply it as a goal hint without changing the action. The
        # brain prompt builder reads it as a prefix to the existing
        # response_goal.
        default_tag = _DEFAULT_GOAL_TAGS.get(moment)
        if default_tag is not None:
            new_args = dict(decision.args or {})
            new_args["preferred_response_goal"] = default_tag
            new_args["relational_routing_applied"] = False  # tag only
            new_args["relational_moment"] = str(moment.value)
            new_decision = replace(
                decision,
                args=new_args,
                reason=(
                    f"{decision.reason or ''} | "
                    f"relational_tag={moment.value}:{default_tag}"
                ).strip(" |"),
            )
            _log_router_change(
                ctx=ctx,
                moment=moment,
                before_action=current_action,
                after_action=current_action,
                goal_token=default_tag,
                kind="tag",
            )
            return new_decision

        return decision
    except Exception as exc:  # noqa: BLE001
        # Router failures are NEVER fatal. We log and degrade to
        # the original decision so a bug in the router cannot stall
        # production traffic.
        logger.debug(
            "[CX] decision router failed (returning decision unchanged): %s",
            exc,
        )
        return decision


def _log_router_change(
    *,
    ctx: Any,
    moment: ConversationMoment,
    before_action: str,
    after_action: str,
    goal_token: str,
    kind: str,
) -> None:
    """Single ``[CX]`` line per re-route. Stable greppable tokens —
    operators answer "why did the action change?" without parsing
    free-form prose."""
    try:
        tenant_id = getattr(ctx, "tenant_id", None)
        phone = getattr(ctx, "customer_phone", "") or ""
        masked = ("*" + phone[-4:]) if phone else ""
        logger.info(
            "[CX] router tenant=%s phone=%s kind=%s moment=%s "
            "before_action=%s after_action=%s goal=%s",
            tenant_id, masked, kind, str(moment.value),
            before_action, after_action, goal_token,
        )
    except Exception:
        pass


__all__ = [
    "apply_relational_preference",
    "is_decision_router_enabled",
    "RESPONSE_GOAL_APPRECIATION_ACK",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC",
    "RESPONSE_GOAL_TRUST_BUILDING",
]
