"""
brain/compose/responder.py
───────────────────────────
DefaultComposer: maps (Decision, ActionResult, BrainContext) → Arabic reply text.

For deterministic actions (greet, search, order, …) we use templates.
For ACTION_LLM_REPLY we use a thin MerchantBrain LLM path with explicit
BrainReplyState, and keep the legacy orchestrator only as an internal
emergency fallback.

This keeps the LLM in a well-defined "composer" role rather than being
the entire brain.
"""
from __future__ import annotations

from dataclasses import asdict
import logging
import os
import sys

logger = logging.getLogger("nahla.brain.responder")

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
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
from ..execution.faq import (
    TOPIC_IDENTITY,
    TOPIC_OWNER_CONTACT,
    TOPIC_SHIPPING,
    TOPIC_STORE_INFO,
)
from . import templates as T
from .prompt_builder import build_brain_reply_prompt


class DefaultComposer:
    """Implements Composer protocol."""

    async def compose(
        self,
        decision: Decision,
        result: ActionResult,
        ctx: BrainContext,
    ) -> str:
        action = decision.action
        data   = result.data or {}

        # ── Greet ──────────────────────────────────────────────────────────
        if action == ACTION_GREET:
            return T.greeting(store_name=ctx.facts.store_name)

        # ── FAQ ────────────────────────────────────────────────────────────
        if action == ACTION_FAQ_REPLY:
            payload = data.get("payload", {}) or {}
            topic = data.get("topic", "")
            if topic == TOPIC_IDENTITY:
                return T.faq_identity(store_name=ctx.facts.store_name)
            if topic == TOPIC_SHIPPING:
                return self._with_follow_up(
                    T.faq_shipping(
                        shipping_policy=payload.get("shipping_policy", ""),
                        shipping_methods=payload.get("shipping_methods", []),
                        shipping_notes=payload.get("shipping_notes", ""),
                        support_hours=payload.get("support_hours", ""),
                    ),
                    ctx,
                )
            if topic == TOPIC_STORE_INFO:
                return self._with_follow_up(
                    T.faq_store_info(
                        store_name=payload.get("store_name", ""),
                        store_url=payload.get("store_url", ""),
                        store_description=payload.get("store_description", ""),
                    ),
                    ctx,
                )
            if topic == TOPIC_OWNER_CONTACT:
                return self._with_follow_up(
                    T.faq_owner_contact(
                        contact_phone=payload.get("contact_phone", ""),
                        contact_email=payload.get("contact_email", ""),
                        store_url=payload.get("store_url", ""),
                    ),
                    ctx,
                )
            return T.generic_fallback()

        # ── Search ─────────────────────────────────────────────────────────
        if action == ACTION_SEARCH_PRODUCTS:
            if not result.success or data.get("message") == "no_products_in_catalog":
                return T.no_products()
            # If many results and no specific intent, present as narrow choices
            if data.get("suggest_narrow") and data.get("products"):
                return T.narrow_choices(products=data["products"][:3])
            return T.product_results(
                product_lines=data.get("product_lines", ""),
                query=data.get("query", ""),
                count=data.get("count", 0),
            )

        # ── Draft order ────────────────────────────────────────────────────
        if action == ACTION_PROPOSE_DRAFT_ORDER:
            if not result.success:
                return T.generic_fallback()
            if data.get("needs_collection"):
                return T.collect_order_details(
                    product=data.get("product", {}),
                    question=data.get("question", ""),
                    missing_fields=data.get("missing_fields", []),
                )
            if data.get("intent_only"):
                return T.order_intent_captured(product=data.get("product", {}))
            return T.draft_order_created(
                product=data.get("product", {}),
                reference=str(data.get("reference", "")),
                checkout_url=data.get("checkout_url", ""),
                total=float(data.get("total") or 0),
                currency=data.get("currency", "SAR"),
            )

        # ── Payment link ───────────────────────────────────────────────────
        if action == ACTION_SEND_PAYMENT_LINK:
            return T.payment_link(checkout_url=data.get("checkout_url", ""))

        # ── Track order ────────────────────────────────────────────────────
        if action == ACTION_TRACK_ORDER:
            if not result.success or data.get("message") == "no_orders_found":
                return T.no_orders()
            return self._with_follow_up(
                T.order_status(
                    reference=str(data.get("reference", "")),
                    status=data.get("status", ""),
                    total=float(data.get("total") or 0),
                    currency=data.get("currency", "SAR"),
                ),
                ctx,
            )

        # ── Coupon ─────────────────────────────────────────────────────────
        if action == ACTION_SUGGEST_COUPON:
            if not result.success or not data.get("coupon_block"):
                return T.generic_fallback()
            return self._with_follow_up(
                T.coupon_offer(
                    coupon_block=data.get("coupon_block", ""),
                    product=data.get("product"),
                ),
                ctx,
            )

        # ── Clarify ────────────────────────────────────────────────────────
        if action == ACTION_CLARIFY:
            return T.clarify(question=data.get("question", ""))

        # ── Narrow choices ─────────────────────────────────────────────────
        if action == ACTION_NARROW:
            return T.narrow_choices(products=data.get("products", []))

        # ── Handoff ────────────────────────────────────────────────────────
        if action == ACTION_HANDOFF:
            return T.handoff()

        # ── LLM fallback ───────────────────────────────────────────────────
        if action == ACTION_LLM_REPLY:
            return await self._llm_compose(ctx, result)

        return T.generic_fallback()

    def _with_follow_up(self, text: str, ctx: BrainContext) -> str:
        suggestion = getattr(ctx, "suggestion", None)
        if not suggestion or not suggestion.needs_follow_up_question:
            return text

        follow_up = (suggestion.follow_up_question or "").strip()
        if not follow_up or follow_up in text:
            return text

        return f"{text}\n\n{follow_up}"

    # ── LLM delegation ───────────────────────────────────────────────────────

    async def _llm_compose(self, ctx: BrainContext, result: ActionResult) -> str:
        """Use the thin MerchantBrain LLM path, with legacy fallback on hard errors.

        The preferred path injects a short prompt + explicit BrainReplyState.
        We keep the legacy orchestrator call only as an emergency fallback when
        the new path fails unexpectedly, not as the default path.
        """
        import asyncio  # noqa: PLC0415

        _TIMEOUT = 25  # seconds

        try:
            _BACKEND = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../../../../..")
            )
            if _BACKEND not in sys.path:
                sys.path.insert(0, _BACKEND)

            from modules.ai.orchestrator.adapter import generate_ai_reply  # noqa: PLC0415

            reply_state = ctx.reply_state
            if reply_state is None:
                logger.warning(
                    "[Composer._llm_compose] missing reply_state | tenant=%s",
                    ctx.tenant_id,
                )
                return T.generic_fallback()

            prompt = build_brain_reply_prompt(reply_state)
            locale = str(ctx.profile.get("preferred_language") or "ar")

            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    generate_ai_reply,
                    tenant_id=ctx.tenant_id,
                    customer_phone=ctx.customer_phone,
                    message=ctx.message,
                    store_name=ctx.facts.store_name,
                    channel="whatsapp",
                    locale=locale,
                    context_metadata={
                        "brain_state": asdict(reply_state),
                        "suggestion": asdict(ctx.suggestion) if ctx.suggestion else {},
                    },
                    prompt_overrides={"__full_system_prompt": prompt},
                    provider_hint="anthropic",
                ),
                timeout=_TIMEOUT,
            )

            reply_text = (payload.reply_text or "").strip()
            if reply_text:
                result.data["chosen_path"] = "llm"
                result.data["llm_provider"] = payload.provider_used
                result.data["model_used"] = payload.metadata.get("model", payload.provider_used)
                result.data["prompt_mode"] = "merchant_brain_thin"
                return reply_text

            logger.warning(
                "[Composer._llm_compose] thin path returned empty reply | tenant=%s",
                ctx.tenant_id,
            )
            return await self._legacy_llm_compose(ctx, result, timeout_seconds=15)
        except asyncio.TimeoutError:
            logger.warning(
                "[Composer._llm_compose] thin LLM timed out after %ds | tenant=%s",
                _TIMEOUT, ctx.tenant_id,
            )
            result.data["chosen_path"] = "llm_timeout"
            return (
                "عذراً، تأخّر الرد قليلاً. "
                "هل يمكنك إعادة سؤالك؟ أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
            )
        except Exception as exc:
            logger.error("[Composer._llm_compose] thin path error: %s", exc)
            return await self._legacy_llm_compose(ctx, result, timeout_seconds=15)

    async def _legacy_llm_compose(
        self,
        ctx: BrainContext,
        result: ActionResult,
        timeout_seconds: int = 15,
    ) -> str:
        """Emergency fallback while the thin path rolls out."""
        import asyncio  # noqa: PLC0415

        try:
            _BACKEND = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../../../../..")
            )
            if _BACKEND not in sys.path:
                sys.path.insert(0, _BACKEND)

            from modules.ai.orchestrator.adapter import generate_orchestrate_response  # noqa: PLC0415

            legacy = await asyncio.wait_for(
                generate_orchestrate_response(
                    tenant_id=ctx.tenant_id,
                    customer_phone=ctx.customer_phone,
                    message=ctx.message,
                    conversation_id=ctx.conversation_id,
                ),
                timeout=timeout_seconds,
            )
            reply_text = (legacy.get("reply", "") or "").strip()
            if reply_text:
                result.data["chosen_path"] = "llm_legacy_fallback"
                result.data["model_used"] = legacy.get("model", "legacy_orchestrator")
                result.data["prompt_mode"] = "legacy_orchestrator_fallback"
                return reply_text
        except Exception as exc:
            logger.error("[Composer._legacy_llm_compose] error: %s", exc)

        result.data["chosen_path"] = "llm_fallback_failed"
        return T.generic_fallback()
