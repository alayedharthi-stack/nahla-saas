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
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_OUT_OF_SCOPE,
    ACTION_PAYMENT_TRANSFER_PROMISE,
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_PRODUCT_MEDIA_IDENTITY,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SELECT_PURCHASE_CHANNEL,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SOCIAL_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_PAYMENT_CONTINUATION_REPLY,
    ACTION_VARIANT_PRICING,
    ACTION_WEB_SEARCH,
)

logger = logging.getLogger("nahla.brain.executor")


# ── Inline simple handlers ────────────────────────────────────────────────────

class _GreetHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "greet"})


class _HandoffHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "handoff"})


# Direct-compose actions: the responder produces the text deterministically,
# so the executor only needs to acknowledge success. No LLM call, no tool
# invocation, no catalog lookup. Same shape as ``_GreetHandler``.
class _OutOfScopeHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "out_of_scope"})


class _SocialReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(
            success=True,
            data={
                "type": "social_reply",
                "social_category": str((decision.args or {}).get("social_category") or "general_courtesy"),
            },
        )


class _PlatformReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(
            success=True,
            data={
                "type": "platform_reply",
                "platform_topic": str((decision.args or {}).get("platform_topic") or "general_platform"),
            },
        )


class _SendPaymentLinkHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

        runtime = build_commerce_runtime(ctx)
        runtime_result = await runtime.execute(
            "send_payment_link",
            {
                "checkout_url": decision.args.get("checkout_url") or ctx.state.checkout_url or "",
                "order_id": decision.args.get("draft_order_id") or ctx.state.draft_order_id or "",
                "amount": decision.args.get("amount") or 0,
                "description": decision.args.get("description") or "",
            },
        )
        url = str(runtime_result.payload.get("checkout_url") or "").strip()
        return ActionResult(
            success=bool(url),
            data={"checkout_url": url, "type": "payment_link"},
            error=None if url else "no_checkout_url",
        )


class _SuggestCouponHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

        # Pull the product the customer is currently considering so we can
        # pass its price as cart_total to the offer engine (smarter discount).
        product_focus: dict = ctx.state.current_product_focus or {}
        product_title: str  = str(product_focus.get("title") or "").strip()
        product_price: float | None = None
        _raw_price = product_focus.get("price")
        if _raw_price is not None:
            try:
                product_price = float(_raw_price)
            except (TypeError, ValueError):
                pass

        runtime = build_commerce_runtime(ctx)
        payload: dict = {
            "discount_pct": (ctx.sales_context.offer_signals or {}).get("recommended_discount_pct", 0)
            if ctx.sales_context
            else 0,
            "segment": (ctx.sales_context.customer_profile or {}).get("segment", "")
            if ctx.sales_context
            else "",
        }
        if product_price:
            payload["cart_total"] = product_price

        runtime_result = await runtime.execute("apply_coupon", payload)
        coupon    = runtime_result.payload.get("coupon") or {}
        raw_dec   = runtime_result.payload.get("decision") or {}
        code      = str(coupon.get("coupon_code") or coupon.get("discount_code") or "").strip()

        # Build a human-readable Arabic coupon block.
        if code:
            # Try to include the discount percentage from the decision object.
            disc_val = raw_dec.get("discount_value")
            disc_type = str(raw_dec.get("discount_type") or "").lower()
            if disc_val and disc_type == "percentage":
                pct_label = f" ({int(disc_val)}% خصم)"
            elif disc_val and disc_type == "fixed":
                pct_label = f" (خصم {disc_val:.0f} ريال)"
            else:
                pct_label = ""

            if product_title:
                block = f"كود خصم{pct_label} خاص بك على *{product_title}*: `{code}`"
            else:
                block = f"كود خصم{pct_label} خاص بك: `{code}`"

            # Validity hint
            validity = raw_dec.get("validity_days")
            if validity:
                block += f"\n⏳ صالح لـ {validity} يوم"
        else:
            block = ""

        return ActionResult(
            success=bool(block),
            data={
                "coupon_block":   block,
                "product":        decision.args.get("product") or (product_focus or None),
                "coupon_payload": coupon,
                "discount_value": raw_dec.get("discount_value"),
                "discount_type":  raw_dec.get("discount_type"),
            },
        )


class _ClarifyHandler:
    """Ask the customer one focused clarifying question."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        question = decision.args.get("question", "ما الذي تبحث عنه بالضبط؟")
        try:
            from ..commerce.product_ordering_prompt import resolve_product_clarify_question  # noqa: PLC0415

            question = resolve_product_clarify_question(ctx, str(question or ""))
        except Exception:
            logger.exception(
                "[EXECUTOR] product_ordering_prompt failed",
            )
        try:
            from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                apply_resolved_product_clarify_guard,
            )
            question = apply_resolved_product_clarify_guard(
                ctx,
                str(question or ""),
                source="executor_clarify",
                query=str((decision.args or {}).get("query") or ""),
            )
        except Exception:
            logger.exception(
                "[EXECUTOR] resolved_product_clarify_guard failed",
            )
        return ActionResult(success=True, data={"question": question, "type": "clarify"})


class _VariantPricingHandler:
    """Deterministic reply — variant, unit, and price stay bound."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        args = dict(decision.args or {})
        data = {
            "type": "variant_pricing",
            "reply_text": str(args.get("reply_text") or "").strip(),
            "variant_binding": args.get("variant_binding") or {},
            "price_trace": args.get("price_trace") or {},
            "variant_trace": args.get("variant_trace") or {},
            "quantity_trace": args.get("quantity_trace") or {},
        }
        facts = args.get("catalog_fact_products")
        if isinstance(facts, list) and facts:
            data["catalog_fact_products"] = list(facts)
        return ActionResult(success=True, data=data)


class _PaymentTransferPromiseHandler:
    """Deterministic ack when customer promises a future transfer."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from core.payment_intent import PAYMENT_TRANSFER_PROMISE_REPLY_AR  # noqa: PLC0415

        return ActionResult(
            success=True,
            data={
                "type": "payment_transfer_promise",
                "reply_text": PAYMENT_TRANSFER_PROMISE_REPLY_AR,
            },
        )


class _ProductMediaIdentityHandler:
    """Deterministic product identity from inbound media evidence."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        args = dict(decision.args or {})
        return ActionResult(
            success=True,
            data={
                "type": "product_media_identity",
                "reply_text": str(args.get("reply_text") or "").strip(),
                "media_identity_status": args.get("media_identity_status"),
                "matched_product_id": args.get("matched_product_id"),
                "matched_product_title": args.get("matched_product_title"),
                "match_confidence": args.get("match_confidence"),
                "block_purchase_flow": bool(args.get("block_purchase_flow")),
            },
        )


class _NarrowHandler:
    """Present a short list of product choices to help the customer decide."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        products = decision.args.get("products", [])
        return ActionResult(success=True, data={"products": products, "type": "narrow"})


class _RecommendAddonHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

        runtime = build_commerce_runtime(ctx)
        result = await runtime.execute(
            "recommend_addon",
            {
                "product_id": (
                    (ctx.state.current_product_focus or {}).get("external_id")
                    or (ctx.state.current_product_focus or {}).get("id")
                ),
                "query": decision.args.get("query") or "",
            },
        )
        return ActionResult(
            success=result.ok,
            data={
                "products": result.payload.get("products", []),
                "recommended_products": result.payload.get("products", []),
                "type": "recommend_addon",
            },
            error=result.error,
        )


class _WebSearchHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

        runtime = build_commerce_runtime(ctx)
        result = await runtime.execute(
            "web_search",
            {"query": decision.args.get("query") or ctx.message},
        )
        return ActionResult(
            success=result.ok,
            data={
                "summary": result.payload.get("summary", ""),
                "results": result.payload.get("results", []),
                "citations": result.payload.get("citations", []),
                "type": "web_search",
            },
            error=result.error,
        )


class _LLMReplyHandler:
    """Route to the existing generate_orchestrate_response pipeline."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        policy_reason = decision.args.get("policy_reason", "")
        return ActionResult(
            success=True,
            data={"type": "llm_fallback", "policy_reason": policy_reason},
        )


class _OrderContextUpdateHandler:
    """Attach map/address/shipping updates to an active order funnel."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from .orders import DraftOrderHandler

        product = decision.args.get("forced_product") or decision.args.get("product")
        if not product:
            try:
                from ..order_context_gate import _resolve_product_for_update  # noqa: PLC0415
                product = _resolve_product_for_update(ctx)
            except Exception:  # noqa: BLE001
                product = None

        draft_args = dict(decision.args or {})
        draft_args["order_context_update"] = True
        draft_args["source"] = "order_context_update"
        if product:
            draft_args["product"] = product
            draft_args["forced_product"] = product

        draft_decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args=draft_args,
            reason=decision.reason,
            confidence=decision.confidence,
        )
        return await DraftOrderHandler().handle(draft_decision, ctx)


class _StashAddressPreProductHandler:
    """Stash address signals captured BEFORE a product was picked.

    The customer typed e.g. "TAPA7401" while still browsing. We persist
    the values onto state.pending_* (the pipeline projects this onto the
    next state). On the next turn — when the customer actually picks a
    product — DraftOrderHandler reads `state.pending_*`, merges the
    values into `order_prep`, and clears the pending fields so we never
    ask for the address again.
    """
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(
            success=True,
            data={
                "type": "stash_address_pre_product",
                "stash_address": {
                    "short_address_code": str(decision.args.get("short_address_code") or "").strip(),
                    "google_maps_url":   str(decision.args.get("google_maps_url") or "").strip(),
                    "city":              str(decision.args.get("city") or "").strip(),
                },
            },
        )


# ── Registry ──────────────────────────────────────────────────────────────────

class _CustomerLedgerReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from core.customer_commerce_answerer import (  # noqa: PLC0415
            TOPIC_ORDER_HISTORY_COUNT,
            resolve_customer_commerce_reply,
        )

        topic = str((decision.args or {}).get("ledger_topic") or TOPIC_ORDER_HISTORY_COUNT)
        reply = resolve_customer_commerce_reply(
            getattr(ctx, "_db", None) or getattr(ctx, "db", None),
            topic=topic,
            tenant_id=int(ctx.tenant_id),
            conversation_id=getattr(ctx, "conversation_id", None),
            customer_id=getattr(ctx, "customer_id", None),
            phone=str(ctx.customer_phone or ""),
        )
        return ActionResult(
            success=True,
            data={
                "type": "customer_ledger_reply",
                "ledger_topic": topic,
                "reply": reply,
            },
        )


class _PaymentContinuationReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from core.payment_continuation_policy import (  # noqa: PLC0415
            resolve_payment_continuation_reply,
        )

        reply = str((decision.args or {}).get("reply") or "").strip()
        if not reply:
            reply = resolve_payment_continuation_reply(
                getattr(ctx, "_db", None) or getattr(ctx, "db", None),
                tenant_id=int(ctx.tenant_id),
                conversation_id=getattr(ctx, "conversation_id", None),
                customer_id=getattr(ctx, "customer_id", None),
                phone=str(ctx.customer_phone or ""),
                message=str(ctx.message or ""),
                state=ctx.state,
                history=getattr(ctx, "history", None),
                commerce_bundle=getattr(ctx, "commerce_bundle", None),
                intent_slots=dict(getattr(ctx.intent, "slots", None) or {}),
            )
        return ActionResult(
            success=bool(reply),
            data={
                "type": "payment_continuation_reply",
                "continuation_case": (decision.args or {}).get("continuation_case", ""),
                "reply": reply,
            },
        )


class _SelectPurchaseChannelHandler:
    """Validate chrome or structured-action selection, persist, then execute."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from ..commerce.checkout_route_owner import (  # noqa: PLC0415
            apply_selected_purchase_channel,
            extract_structured_purchase_channel_id,
        )

        args = dict(decision.args or {})
        facts = getattr(ctx, "facts", None)
        state = getattr(ctx, "state", None)
        inbound = getattr(ctx, "inbound_metadata", None)
        trusted_id = extract_structured_purchase_channel_id(
            message=getattr(ctx, "message", "") or "",
            inbound_metadata=inbound if isinstance(inbound, dict) else {},
            brain_decision_action=str(decision.action or ""),
            brain_decision_args=args,
        )
        selected_id = str(trusted_id or "").strip()
        result = apply_selected_purchase_channel(
            getattr(ctx, "_db", None) or getattr(ctx, "db", None),
            tenant_id=int(ctx.tenant_id or 0),
            phone=str(ctx.customer_phone or ""),
            selected_channel_id=selected_id,
            order_prep=getattr(state, "order_prep", None) if state is not None else None,
            merchant_sales_channels=getattr(ctx, "merchant_sales_channels", None),
            store_url=str(getattr(facts, "store_url", "") or ""),
            store_url_source=str(getattr(facts, "store_url_source", "") or ""),
            maps_url=str(getattr(facts, "maps_url", "") or ""),
        )
        topic = result.execution_topic if result.accepted else "purchase_channel_selection"
        args["topic"] = topic
        args["selected_channel_id"] = result.selected_channel_id
        args["available_purchase_channels"] = list(result.available_purchase_channel_ids)
        if result.accepted and result.cta_url:
            args["cta_url"] = result.cta_url
            args["cta_label"] = result.cta_label
        decision.args = args
        accepted = bool(result.accepted and result.persist_ok)
        return ActionResult(
            success=accepted,
            data={
                "type": "select_purchase_channel",
                "accepted": accepted,
                "selected_channel_id": result.selected_channel_id,
                "execution_topic": topic,
                "reason": result.reason,
                "checkout_channel": result.checkout_channel,
                "persist_ok": result.persist_ok,
                "executed": result.executed,
                "execution_owner": result.execution_owner,
                "cta_url": result.cta_url if accepted else "",
                "cta_label": result.cta_label if accepted else "",
            },
            error=None if accepted else result.reason or "selection_rejected",
        )


class DefaultActionExecutor:
    """Implements ActionExecutor protocol."""

    def __init__(self) -> None:
        from .catalog_navigate import CatalogNavigateHandler
        from .customer_coupon_request import CustomerCouponRequestHandler
        from .faq import FAQReplyHandler
        from .search import ProductSearchHandler
        from .orders import DraftOrderHandler, TrackOrderHandler

        self._handlers: Dict[str, Any] = {
            ACTION_GREET:               _GreetHandler(),
            ACTION_CUSTOMER_COUPON_REQUEST: CustomerCouponRequestHandler(),
            ACTION_FAQ_REPLY:           FAQReplyHandler(),
            ACTION_SEARCH_PRODUCTS:     ProductSearchHandler(),
            ACTION_CATALOG_NAVIGATE:    CatalogNavigateHandler(),
            ACTION_PROPOSE_DRAFT_ORDER: DraftOrderHandler(),
            ACTION_SEND_PAYMENT_LINK:   _SendPaymentLinkHandler(),
            ACTION_SUGGEST_COUPON:      _SuggestCouponHandler(),
            ACTION_TRACK_ORDER:         TrackOrderHandler(),
            ACTION_CUSTOMER_LEDGER_REPLY: _CustomerLedgerReplyHandler(),
            ACTION_PAYMENT_CONTINUATION_REPLY: _PaymentContinuationReplyHandler(),
            ACTION_HANDOFF:             _HandoffHandler(),
            ACTION_CLARIFY:             _ClarifyHandler(),
            ACTION_NARROW:              _NarrowHandler(),
            ACTION_RECOMMEND_ADDON:     _RecommendAddonHandler(),
            ACTION_WEB_SEARCH:          _WebSearchHandler(),
            ACTION_LLM_REPLY:           _LLMReplyHandler(),
            ACTION_STASH_ADDRESS_PRE_PRODUCT: _StashAddressPreProductHandler(),
            # Direct-compose actions — no tool invocation, the responder
            # owns the text. Registered explicitly so the unknown-action
            # safety net doesn't accidentally fall back to the LLM and
            # waste a model call (or worse, leak an answer the brain
            # decided not to produce).
            ACTION_OUT_OF_SCOPE:        _OutOfScopeHandler(),
            ACTION_SOCIAL_REPLY:        _SocialReplyHandler(),
            ACTION_PLATFORM_REPLY:      _PlatformReplyHandler(),
            ACTION_ORDER_CONTEXT_UPDATE: _OrderContextUpdateHandler(),
            ACTION_VARIANT_PRICING:       _VariantPricingHandler(),
            ACTION_PAYMENT_TRANSFER_PROMISE: _PaymentTransferPromiseHandler(),
            ACTION_PRODUCT_MEDIA_IDENTITY: _ProductMediaIdentityHandler(),
            ACTION_SELECT_PURCHASE_CHANNEL: _SelectPurchaseChannelHandler(),
        }

    async def execute(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.brain.commerce.permission_gate import deny_reason_for_brain_action

        handler = self._handlers.get(decision.action)
        if not handler:
            logger.error("[Executor] unknown action: %s — falling back to LLM", decision.action)
            handler = self._handlers[ACTION_LLM_REPLY]

        denial = deny_reason_for_brain_action(ctx, decision.action)
        if denial:
            return ActionResult(
                success=False,
                error=denial,
                data={
                    "permission_denied": True,
                    "permission_source": getattr(ctx, "permission_source", ""),
                    "action": decision.action,
                },
            )

        # INFO-level so this always appears in Railway logs regardless of
        # the log-level setting (debug is often suppressed in prod).
        logger.info(
            "[ORDER FLOW] decision=%s | tenant=%s reason=%r "
            "confidence=%.2f forced_product=%r arg_product=%r",
            decision.action,
            ctx.tenant_id,
            decision.reason,
            decision.confidence,
            (decision.args.get("forced_product") or {}).get("title"),
            (decision.args.get("product") or {}).get("title"),
        )
        try:
            return await handler.handle(decision, ctx)
        except Exception as exc:
            logger.exception("[Executor] handler %s failed: %s", decision.action, exc)
            return ActionResult(success=False, error=str(exc))
