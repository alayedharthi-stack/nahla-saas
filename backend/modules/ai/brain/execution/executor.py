"""
brain/execution/executor.py
────────────────────────────
DefaultActionExecutor: dispatcher that routes a Decision to the correct
handler and returns an ActionResult.

Adding a new action:
  1. Import its handler class here.
  2. Register it in _REGISTRY below.
  3. The rest of the pipeline needs no changes.
"""
from __future__ import annotations

import logging
from typing import Dict, Type, Any

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)

logger = logging.getLogger("nahla.brain.executor")


# ── Inline simple handlers ────────────────────────────────────────────────────

class _GreetHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "greet"})


class _HandoffHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "handoff"})


class _SendPaymentLinkHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        url = decision.args.get("checkout_url") or ctx.state.checkout_url or ""
        return ActionResult(
            success=bool(url),
            data={"checkout_url": url, "type": "payment_link"},
            error=None if url else "no_checkout_url",
        )


class _SuggestCouponHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from core.store_knowledge import CouponContextBuilder  # lazy
        import os, sys
        _BACKEND = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../../..")
        )
        if _BACKEND not in sys.path:
            sys.path.insert(0, _BACKEND)

        try:
            builder = CouponContextBuilder(ctx._db, ctx.tenant_id)   # type: ignore[attr-defined]
            block   = builder.build_context_block()
        except Exception:
            block = ""

        return ActionResult(
            success=bool(block),
            data={"coupon_block": block, "product": decision.args.get("product")},
        )


class _ClarifyHandler:
    """Ask the customer one focused clarifying question."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        question = decision.args.get("question", "ما الذي تبحث عنه بالضبط؟")
        return ActionResult(success=True, data={"question": question, "type": "clarify"})


class _NarrowHandler:
    """Present a short list of product choices to help the customer decide."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        products = decision.args.get("products", [])
        return ActionResult(success=True, data={"products": products, "type": "narrow"})


class _LLMReplyHandler:
    """Route to the existing generate_orchestrate_response pipeline."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        policy_reason = decision.args.get("policy_reason", "")
        return ActionResult(
            success=True,
            data={"type": "llm_fallback", "policy_reason": policy_reason},
        )


# ── Registry ──────────────────────────────────────────────────────────────────

class DefaultActionExecutor:
    """Implements ActionExecutor protocol."""

    def __init__(self) -> None:
        from .search import ProductSearchHandler
        from .orders import DraftOrderHandler, TrackOrderHandler

        self._handlers: Dict[str, Any] = {
            ACTION_GREET:               _GreetHandler(),
            ACTION_SEARCH_PRODUCTS:     ProductSearchHandler(),
            ACTION_PROPOSE_DRAFT_ORDER: DraftOrderHandler(),
            ACTION_SEND_PAYMENT_LINK:   _SendPaymentLinkHandler(),
            ACTION_SUGGEST_COUPON:      _SuggestCouponHandler(),
            ACTION_TRACK_ORDER:         TrackOrderHandler(),
            ACTION_HANDOFF:             _HandoffHandler(),
            ACTION_CLARIFY:             _ClarifyHandler(),
            ACTION_NARROW:              _NarrowHandler(),
            ACTION_LLM_REPLY:           _LLMReplyHandler(),
        }

    async def execute(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        handler = self._handlers.get(decision.action)
        if not handler:
            logger.error("[Executor] unknown action: %s — falling back to LLM", decision.action)
            handler = self._handlers[ACTION_LLM_REPLY]

        logger.debug(
            "[Executor] tenant=%s action=%s args=%s",
            ctx.tenant_id, decision.action, decision.args,
        )
        try:
            return await handler.handle(decision, ctx)
        except Exception as exc:
            logger.exception("[Executor] handler %s failed: %s", decision.action, exc)
            return ActionResult(success=False, error=str(exc))
