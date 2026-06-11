"""
brain/compose/responder.py
───────────────────────────
DefaultComposer: maps (Decision, ActionResult, BrainContext) → Arabic reply text.

Single contract: DecisionEngine → Composer. The Composer is the ONLY place
that turns a Decision into text. It never re-decides — if it disagrees with
the Decision (e.g. ACTION_GREET arrives but the customer was already
greeted) it downgrades to LLM with explicit context, never silently sends
a static template.

State → action → template mapping (authoritative reference):

  ACTION_GREET            → templates.greeting             [discovery, !greeted]
  ACTION_FAQ_REPLY        → templates.faq_*                [any]
  ACTION_SEARCH_PRODUCTS  → templates.product_results      [discovery..deciding]
                          / templates.narrow_choices       [many results]
                          / templates.no_products          [empty catalog]
  ACTION_PROPOSE_DRAFT_ORDER
                          → templates.collect_order_details [needs_collection]
                          / templates.draft_order_created   [success]
                          / templates.order_intent_captured [intent_only]
  ACTION_SEND_PAYMENT_LINK→ templates.payment_link         [checkout]
  ACTION_TRACK_ORDER      → templates.order_status         [any]
  ACTION_SUGGEST_COUPON   → templates.coupon_offer         [deciding/exploring]
  ACTION_RECOMMEND_ADDON  → templates.addon_recommendations[deciding/ordering]
  ACTION_WEB_SEARCH       → templates.web_search_summary   [discovery]
  ACTION_CLARIFY          → templates.clarify              [any]
  ACTION_NARROW           → templates.narrow_choices       [exploring]
  ACTION_HANDOFF          → templates.handoff              → support
  ACTION_LLM_REPLY        → _llm_compose (BrainReplyState) [fallback]

Every template returns ONE message; Composer never chains templates.
"""
from __future__ import annotations

from dataclasses import asdict
import logging
import os
import sys
from typing import Any, Dict, List

logger = logging.getLogger("nahla.brain.responder")

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SOCIAL_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_WEB_SEARCH,
    ACTION_OUT_OF_SCOPE,
    ACTION_PAYMENT_TRANSFER_PROMISE,
    ACTION_VARIANT_PRICING,
)
from ..execution.faq import (
    TOPIC_IDENTITY,
    TOPIC_LOCATION,
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

        # ── State guard (defense-in-depth) ─────────────────────────────────
        # The DecisionEngine already gates greetings on `state.greeted` and
        # the in-progress sales stage, but a stray ACTION_GREET produced by
        # learned-policy layers, partial state loss, or future decoration
        # would still land here. We refuse to send the full greeting
        # template twice in the same conversation, period.
        #
        # However — when the customer EXPLICITLY says salaam/hello after
        # being greeted, downgrading to LLM is the wrong move (it makes
        # the bot ignore the salutation). The DecisionEngine signals this
        # case by passing `re_greet=True` in `decision.args` and we honour
        # it here with a short re-greeting template instead.
        re_greet_requested = bool(decision.args.get("re_greet"))
        if action == ACTION_GREET and self._should_skip_greet(ctx) and not re_greet_requested:
            logger.info(
                "[Composer] downgrading ACTION_GREET → LLM | "
                "tenant=%s greeted=%s stage=%s",
                ctx.tenant_id,
                getattr(ctx.state, "greeted", False),
                getattr(ctx.state, "stage", ""),
            )
            decision.action = ACTION_LLM_REPLY
            decision.reason = (
                f"composer_guard:greet_blocked greeted={ctx.state.greeted} "
                f"stage={ctx.state.stage}; "
                "answer the customer's actual message without re-greeting"
            )
            action = ACTION_LLM_REPLY

        # ── Greet ──────────────────────────────────────────────────────────
        if action == ACTION_GREET:
            from .greeting_etiquette import (  # noqa: PLC0415
                apply_greeting_etiquette,
                customer_message_for_etiquette,
            )

            variant = self._variant_idx(ctx)
            persona = getattr(ctx.facts, "assistant_name", "") or ""
            if re_greet_requested:
                text = T.re_greeting(
                    store_name=ctx.facts.store_name,
                    assistant_name=persona,
                    variant=variant,
                )
                if self._is_duplicate(text, ctx):
                    text = T.re_greeting(
                        store_name=ctx.facts.store_name,
                        assistant_name=persona,
                        variant=(variant + 1) % 3,
                    )
            else:
                text = T.greeting(
                    store_name=ctx.facts.store_name,
                    assistant_name=persona,
                    variant=variant,
                )
                if self._is_duplicate(text, ctx):
                    text = T.greeting(
                        store_name=ctx.facts.store_name,
                        assistant_name=persona,
                        variant=(variant + 1) % 3,
                    )
            return apply_greeting_etiquette(
                text,
                customer_message_for_etiquette(ctx),
                ctx.state,
                tenant_id=getattr(ctx, "tenant_id", None),
            )

        # ── FAQ ────────────────────────────────────────────────────────────
        if action == ACTION_FAQ_REPLY:
            payload = data.get("payload", {}) or {}
            topic = data.get("topic", "")
            if topic == TOPIC_IDENTITY:
                return self._with_follow_up(
                    T.faq_identity(
                        store_name=ctx.facts.store_name,
                        assistant_name=getattr(ctx.facts, "assistant_name", "") or "",
                    ),
                    ctx,
                )
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
            if topic == TOPIC_LOCATION:
                return self._with_follow_up(
                    T.faq_location(
                        store_name=payload.get("store_name", ""),
                        maps_url=payload.get("maps_url", ""),
                    ),
                    ctx,
                    topic=TOPIC_LOCATION,
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
            return T.generic_fallback(variant=self._variant_idx(ctx))

        # ── Search ─────────────────────────────────────────────────────────
        if action == ACTION_SEARCH_PRODUCTS:
            # Handle "rejected product → suggest alternatives" flow
            rejected = decision.args.get("rejected_product")
            if rejected:
                alts = decision.args.get("alternatives") or data.get("products") or []
                from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                    apply_display_slice,
                    resolve_product_breadth_from_context,
                )
                breadth = resolve_product_breadth_from_context(ctx, decision)
                alts, breadth_meta = apply_display_slice(alts, breadth)
                result.data["pending_candidates"] = alts
                result.data["product_breadth"] = breadth.to_log_dict()
                result.data["product_breadth_meta"] = breadth_meta
                logger.error(
                    "[ORDER FLOW] product_unavailable_alternatives fired | "
                    "rejected=%r alts_count=%d",
                    rejected.get("title"), len(alts),
                )
                return T.product_unavailable_alternatives(
                    rejected_title=rejected.get("title", ""),
                    alternatives=alts,
                )

            if not result.success or data.get("message") == "no_products_in_catalog":
                query = str((decision.args or {}).get("query") or "").strip()
                inquiry_query = ""
                try:
                    from ..product_discovery_gate import (  # noqa: PLC0415
                        extract_inquiry_product_query,
                    )
                    inquiry_query = extract_inquiry_product_query(ctx.message or "")
                except Exception:  # noqa: BLE001  # noqa: silent-ok — product_discovery_gate optional; generic clarify if unavailable
                    pass
                try:
                    from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                        compose_resolved_product_search_miss,
                        extract_resolved_product_subject,
                        log_clarification_leak,
                    )
                    from ..commerce.catalog_search_evidence import (  # noqa: PLC0415
                        should_use_search_miss_template,
                    )
                    subject = extract_resolved_product_subject(
                        ctx, query=query, inquiry_query=inquiry_query,
                    )
                    if subject and should_use_search_miss_template(ctx, query, subject):
                        log_clarification_leak(
                            tenant_id=getattr(ctx, "tenant_id", None),
                            source="search_miss_compose",
                            normalized_subject=subject,
                            resolved_query=query or subject,
                            preview=str(ctx.message or "")[:80],
                            blocked_text=(
                                "search_miss_type_clarify:"
                                f"{data.get('message') or result.error or 'unknown'}"
                            ),
                        )
                        return compose_resolved_product_search_miss(
                            subject,
                            variant=self._variant_idx(ctx),
                        )
                    if subject:
                        logger.info(
                            "[CATALOG_SEARCH_GATE] search_miss_skip_template "
                            "tenant=%s subject=%r query=%r → llm_compose",
                            getattr(ctx, "tenant_id", None),
                            subject[:40],
                            (query or "")[:40],
                        )
                        result.data["chosen_path"] = "catalog_miss_llm_fallback"
                        return await self._llm_compose(ctx, result)
                except Exception:
                    logger.exception(
                        "[RESPONDER] resolved_product_search_miss compose failed",
                    )
                variant = self._variant_idx(ctx)
                text = T.no_products(variant=variant)
                if self._is_duplicate(text, ctx):
                    text = T.no_products(variant=(variant + 1) % 3)
                return text
            # Validate every product before we show it: if a product that
            # the executor already filtered as orderable somehow lacks
            # can_checkout=True, log it as a catalog bug and exclude it.
            # This prevents "product listed then immediately rejected" UX.
            raw_products = list(data.get("products") or [])
            safe_products: list = []
            for _p in raw_products:
                if _p.get("can_checkout", _p.get("orderable", True)):
                    safe_products.append(_p)
                else:
                    logger.warning(
                        "[CATALOG] listed product failed validation | bug=True "
                        "name=%r external_id=%s can_checkout=%s orderable=%s "
                        "— removed from displayed list",
                        _p.get("title"), _p.get("external_id"),
                        _p.get("can_checkout"), _p.get("orderable"),
                    )

            if not safe_products:
                return T.no_products(variant=self._variant_idx(ctx))

            from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                apply_display_slice,
                log_product_breadth,
                resolve_product_breadth_from_context,
            )
            breadth = resolve_product_breadth_from_context(ctx, decision)
            candidates, breadth_meta = apply_display_slice(safe_products, breadth)
            log_product_breadth(
                tenant_id=getattr(ctx, "tenant_id", None),
                breadth=breadth,
                total=len(safe_products),
                shown=len(candidates),
                action=action,
            )
            result.data["product_breadth"] = breadth.to_log_dict()
            result.data["product_breadth_meta"] = breadth_meta

            # INVARIANT: pending_candidates = EXACTLY the products shown in
            # the numbered list.  The customer reads "1. بنطلون" and expects
            # sending "1" to give them بنطلون — any mismatch causes the
            # "بلوزة غير متوفر" bug.
            # WA quick-reply buttons are capped at 3 (platform limit).
            wa_buttons = []
            for i, p in enumerate(candidates[:3], 1):
                raw_title = str(p.get("title") or "")
                price_str = f" {p['price']} ر" if p.get("price") else ""
                title = (raw_title[:17] + price_str)[:20] if price_str else raw_title[:20]
                wa_buttons.append({
                    "type": "reply",
                    "reply": {"id": f"pick_{i}", "title": title or str(i)},
                })
            result.data["pending_buttons"] = wa_buttons
            result.data["pending_candidates"] = candidates
            variant = self._variant_idx(ctx)
            text = T.narrow_choices(
                products=candidates,
                variant=variant,
                show_more_hint=bool(breadth_meta.get("show_more_hint")),
            )
            if self._is_duplicate(text, ctx):
                text = T.narrow_choices(
                    products=candidates,
                    variant=(variant + 1) % 3,
                    show_more_hint=bool(breadth_meta.get("show_more_hint")),
                )
            return text

        # ── Draft order / active-order location update ─────────────────────
        if action in (ACTION_PROPOSE_DRAFT_ORDER, ACTION_ORDER_CONTEXT_UPDATE):
            if not result.success:
                return T.generic_fallback(variant=self._variant_idx(ctx))
            # The product reference we have can't be resolved on the store
            # (wrong id, deleted, not synced). Ask the customer to choose
            # again — never silently push a doomed order to Salla.
            if data.get("product_unsyncable"):
                _unsync_prod = data.get("product") or {}
                logger.error(
                    "[ORDER FLOW] product_unsyncable fired | "
                    "title=%r external_id=%r message=%r action=%s",
                    _unsync_prod.get("title"), _unsync_prod.get("external_id"),
                    data.get("message"), action,
                )
                return T.product_unsyncable(product=_unsync_prod)
            if data.get("needs_prediction_confirm"):
                result.data["pending_buttons"] = [
                    {"type": "reply", "reply": {"id": "pred_ok",     "title": "نكمل عليه"}},
                    {"type": "reply", "reply": {"id": "pred_change", "title": "أبغى أغير"}},
                ]
                return T.confirm_predicted_options(
                    product=data.get("product", {}),
                    predicted_options=data.get("predicted_options", {}),
                    selected_options=data.get("selected_options", {}),
                    prediction_source=data.get("prediction_source", ""),
                )
            if data.get("needs_options"):
                _missing_groups = data.get("missing_option_groups", []) or []
                if not _missing_groups:
                    logger.warning(
                        "[RESPONDER GUARD] needs_options=True but missing_groups "
                        "is empty — suppressing options prompt",
                    )
                    return None
                if _missing_groups:
                    _first = _missing_groups[0] or {}
                    _values = [v for v in (_first.get("values") or []) if (v.get("name") or "").strip()]
                    _btn_values = _values[:3]  # WhatsApp limit: max 3 buttons
                    if _btn_values:
                        wa_buttons = []
                        for i, v in enumerate(_btn_values, 1):
                            title = ((v.get("name") or "").strip())[:20]
                            if not title:
                                continue
                            wa_buttons.append({
                                "type": "reply",
                                "reply": {"id": f"opt_{i}", "title": title},
                            })
                        if wa_buttons:
                            result.data["pending_buttons"] = wa_buttons
                return T.ask_product_options(
                    product=data.get("product", {}),
                    missing_option_groups=_missing_groups,
                    selected_options=data.get("selected_options", {}),
                )
            if data.get("salla_escalate"):
                return T.salla_escalate_message(product=data.get("product", {}))
            if data.get("salla_retry"):
                return T.salla_retry_message(
                    product=data.get("product", {}),
                    code=data.get("salla_address_code", ""),
                )
            if data.get("needs_collection"):
                return T.collect_order_details(
                    product=data.get("product", {}),
                    question=data.get("question", ""),
                    missing_fields=data.get("missing_fields", []),
                    is_first_ask=data.get("is_first_ask", True),
                )
            if data.get("intent_only"):
                return T.order_intent_captured(product=data.get("product", {}))
            _order_ref = str(
                data.get("reference") or data.get("order_id") or ""
            ).strip()
            if not _order_ref:
                return T.order_intent_captured(product=data.get("product", {}))
            return T.draft_order_created(
                product=data.get("product", {}),
                reference=_order_ref,
                checkout_url=data.get("checkout_url", ""),
                total=float(data.get("total") or 0),
                currency=data.get("currency", "SAR"),
            )

        # ── Address stashed before product pick ───────────────────────────
        # Customer dropped a TAPA / Maps link / city before picking a
        # product. We saved the value on `state.pending_*`; tell them so
        # they don't repeat it, then nudge them to choose a product.
        if action == ACTION_STASH_ADDRESS_PRE_PRODUCT:
            stash = data.get("stash_address") or {}
            return T.address_stashed_pre_product(
                short_code=stash.get("short_address_code", ""),
                google_maps_url=stash.get("google_maps_url", ""),
                city=stash.get("city", ""),
            )

        # ── Payment link ───────────────────────────────────────────────────
        if action == ACTION_SEND_PAYMENT_LINK:
            return T.payment_link(checkout_url=data.get("checkout_url", ""))

        # ── Track order ────────────────────────────────────────────────────
        if action == ACTION_TRACK_ORDER:
            try:
                from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                    should_use_generative_tracking_follow_up,
                )
                if should_use_generative_tracking_follow_up(
                    ctx.message or "",
                    history=ctx.history,
                    state=ctx.state,
                ):
                    return await self._llm_compose(ctx, result)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — tracking follow-up gate best-effort; fall through to template
                pass
            if not result.success or data.get("message") == "no_orders_found":
                try:
                    from core.order_creation_evidence import (  # noqa: PLC0415
                        resolve_track_order_fallback,
                    )

                    _honest = resolve_track_order_fallback(
                        state=ctx.state,
                        history=ctx.history,
                    )
                    if _honest:
                        return _honest
                except Exception:  # noqa: BLE001  # noqa: silent-ok — track evidence fallback best-effort
                    pass
                return T.no_orders()
            return self._with_follow_up(
                T.order_status(
                    reference=str(data.get("reference", "")),
                    status=data.get("status", ""),
                    status_label_ar=data.get("status_label_ar", ""),
                    total=float(data.get("total") or 0),
                    currency=data.get("currency", "SAR"),
                    item_titles=data.get("item_titles") or [],
                ),
                ctx,
            )

        # ── Coupon ─────────────────────────────────────────────────────────
        if action == ACTION_SUGGEST_COUPON:
            if not result.success or not data.get("coupon_block"):
                return T.generic_fallback(variant=self._variant_idx(ctx))
            return self._with_follow_up(
                T.coupon_offer(
                    coupon_block=data.get("coupon_block", ""),
                    product=data.get("product"),
                ),
                ctx,
            )

        # ── Addon recommendation ───────────────────────────────────────────
        if action == ACTION_RECOMMEND_ADDON:
            if not result.success:
                return T.generic_fallback(variant=self._variant_idx(ctx))
            return self._with_follow_up(
                T.addon_recommendations(products=data.get("products", [])),
                ctx,
            )

        # ── Web search ─────────────────────────────────────────────────────
        if action == ACTION_WEB_SEARCH:
            if not result.success:
                return await self._llm_compose(ctx, result)
            return T.web_search_summary(
                summary=data.get("summary", ""),
                citations=data.get("citations", []),
            )

        # ── Hard-only out-of-scope deflection (May 2026 #3) ────────────────
        # The decision engine now only emits ACTION_OUT_OF_SCOPE for
        # the HARD tier (electricity, real estate, programming, legal
        # cases, financial investing, drug dosages, war, etc.).
        # Everything else — including honey-adjacent KB questions and
        # casual chitchat — falls through to ACTION_LLM_REPLY and is
        # handled by the merchant brain with full KB + catalogue +
        # sales_context. So this branch is intentionally short and
        # boring: one polite redirect, no LLM call, no jokes.
        if action == ACTION_OUT_OF_SCOPE:
            return T.hard_out_of_scope_reply(variant=self._variant_idx(ctx))

        # ── Social / courtesy — occasion/safety templates only (P1-F) ─────
        if action == ACTION_SOCIAL_REPLY:
            category = str((decision.args or {}).get("social_category") or "general_courtesy")
            v_main = self._variant_idx(ctx)
            v_secondary = (len(ctx.history or []) // 3) % 5
            reply = T.social_reply(
                category=category,
                variant=v_main,
                sub_variant=v_secondary,
                inbound_text=(ctx.message or ""),
            )
            return self._apply_gender_hint(reply, ctx)

        # ── Platform / SaaS inquiry ────────────────────────────────────────
        # Gateway: if onboarding docs exist in manual_knowledge_base, we
        # delegate to the same thin LLM path as commerce — but prompt_builder
        # swaps in an excerpt-only KB slice + anti-catalog guardrails. When
        # the slice is empty we keep the deterministic canned fallback.
        if action == ACTION_PLATFORM_REPLY:
            rs = ctx.reply_state
            if rs and getattr(rs, "platform_kb_excerpt", "").strip():
                return await self._llm_compose(ctx, result)
            topic = str((decision.args or {}).get("platform_topic") or "general_platform")
            return T.platform_reply(topic=topic, variant=self._variant_idx(ctx))

        # ── Clarify ────────────────────────────────────────────────────────
        if action == ACTION_CLARIFY:
            question = str(data.get("question") or "").strip()
            try:
                from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                    apply_resolved_product_clarify_guard,
                )
                question = apply_resolved_product_clarify_guard(
                    ctx,
                    question,
                    source="compose_clarify",
                    query=str((decision.args or {}).get("query") or ""),
                )
            except Exception:
                logger.exception(
                    "[RESPONDER] resolved_product_clarify_guard failed",
                )
            return T.clarify(question=question)

        # ── Variant-bound pricing (deterministic) ──────────────────────────
        if action == ACTION_VARIANT_PRICING:
            reply = str(data.get("reply_text") or "").strip()
            if reply:
                result.data["chosen_path"] = "variant_pricing"
                return reply
            return T.clarify(question="أي خيار/حجم تقصد؟")

        # ── Future transfer promise (awaiting receipt) ─────────────────────
        if action == ACTION_PAYMENT_TRANSFER_PROMISE:
            reply = str(data.get("reply_text") or "").strip()
            if reply:
                result.data["chosen_path"] = "payment_transfer_promise"
                return reply
            from core.payment_intent import PAYMENT_TRANSFER_PROMISE_REPLY_AR  # noqa: PLC0415
            result.data["chosen_path"] = "payment_transfer_promise"
            return PAYMENT_TRANSFER_PROMISE_REPLY_AR

        # ── Narrow choices ─────────────────────────────────────────────────
        if action == ACTION_NARROW:
            from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                apply_display_slice,
                resolve_product_breadth_from_context,
            )
            breadth = resolve_product_breadth_from_context(ctx, decision)
            products, breadth_meta = apply_display_slice(
                list(data.get("products") or []),
                breadth,
            )
            result.data["pending_candidates"] = products
            variant = self._variant_idx(ctx)
            text = T.narrow_choices(
                products=products,
                variant=variant,
                show_more_hint=bool(breadth_meta.get("show_more_hint")),
            )
            if self._is_duplicate(text, ctx):
                text = T.narrow_choices(
                    products=products,
                    variant=(variant + 1) % 3,
                    show_more_hint=bool(breadth_meta.get("show_more_hint")),
                )
            return text

        # ── Handoff ────────────────────────────────────────────────────────
        # ``after_hours`` is propagated by ``PolicyGate._working_hours``
        # when the customer requests a human outside the merchant's
        # configured working hours. We keep the action as HANDOFF (so
        # the webhook still registers the handoff session + needs_human
        # flags) but use a different copy variant that tells the
        # customer the team will reply during working hours — no
        # "I'll alert the team now" implication.
        if action == ACTION_HANDOFF:
            args = decision.args or {}
            after_hours = bool(args.get("after_hours"))
            if after_hours:
                return T.handoff_after_hours()
            variant = self._variant_idx(ctx)
            text = T.handoff(variant=variant)
            if self._is_duplicate(text, ctx):
                text = T.handoff(variant=(variant + 1) % 3)
            return text

        # ── LLM fallback ───────────────────────────────────────────────────
        if action == ACTION_LLM_REPLY:
            text = await self._llm_compose(ctx, result)
            _topic = str((decision.args or {}).get("topic") or "").strip()
            if _topic == "social_persona_ack":
                text = self._social_persona_emergency_fallback_if_needed(
                    text, ctx, result,
                )
            return self._apply_established_greeting_etiquette(text, ctx, decision)

        variant = self._variant_idx(ctx)
        return T.generic_fallback(variant=variant)

    # ── Variant + dedup helpers ───────────────────────────────────────────────

    @staticmethod
    def _variant_idx(ctx: BrainContext) -> int:
        """Deterministic variant index — rotates 0/1/2 with turn count."""
        return len(ctx.history or []) % 3

    @staticmethod
    def _last_outbound(ctx: BrainContext) -> str:
        """Last message the bot sent in this conversation."""
        for turn in reversed(ctx.history or []):
            if turn.get("direction") in ("out", "outbound"):
                return str(turn.get("body") or "")
        return ""

    @staticmethod
    def _is_duplicate(text: str, ctx: BrainContext) -> bool:
        """True if text's first 70 chars match the last outbound message."""
        last = DefaultComposer._last_outbound(ctx)
        if not last:
            return False
        return text[:70].strip() == last[:70].strip()

    # ── Gender-awareness layer (May 2026 — light add-on) ─────────────────────
    #
    # Wired into ACTION_SOCIAL_REPLY only. Intentionally a thin method
    # on the composer (not a standalone util) so logs and tests can
    # observe the input/output via the existing composer surface, and
    # any future call site needs to opt in by name. Three rules:
    #
    #   1. Detect the gender hint from the CURRENT inbound message
    #      and the customer's profile name (when available), seeded
    #      with the sticky prior hint persisted on state.
    #   2. Mutate the state IN PLACE only when the new hint is at
    #      least as confident as the prior — never downgrade a
    #      strong classification with a noisy turn.
    #   3. Apply the conjugator with the hint. The conjugator is a
    #      no-op for male / unknown / low-confidence hints, so the
    #      masculine template (Arabic's unmarked default) passes
    #      through unchanged.
    #
    # Errors anywhere here are swallowed and the original reply is
    # returned. A gender misclassification must NEVER produce a
    # silent reply or a 500.

    def _social_persona_emergency_fallback_if_needed(
        self,
        text: str,
        ctx: BrainContext,
        result: ActionResult,
    ) -> str:
        """Mirror reciprocal only when LLM compose is empty or hard-failed."""
        cleaned = (text or "").strip()
        path = str(result.data.get("chosen_path") or "")
        if cleaned and path != "llm_fallback_failed":
            return cleaned
        mirrored = T.social_mirror_fallback_reply(ctx.message or "")
        if mirrored:
            result.data["chosen_path"] = "social_mirror_emergency_fallback"
            return self._apply_gender_hint(mirrored, ctx)
        return cleaned

    def _apply_gender_hint(self, reply: str, ctx: BrainContext) -> str:
        if not reply:
            return reply
        try:
            from ...gender import (  # noqa: PLC0415
                GenderHint, apply_gender_to_social_reply, detect_gender,
            )
        except Exception:  # noqa: BLE001
            return reply

        try:
            state = ctx.state
            prior = GenderHint(
                value=(state.customer_gender_hint or "unknown"),
                confidence=float(state.customer_gender_confidence or 0.0),
                source="context",
            )
            # Customer name resolution — best-effort. The profile
            # dict shape varies by loader; try the two most common
            # keys.
            customer_name = ""
            if isinstance(ctx.profile, dict):
                customer_name = str(
                    ctx.profile.get("name")
                    or ctx.profile.get("customer_name")
                    or ctx.profile.get("display_name")
                    or ""
                )

            hint = detect_gender(
                message=ctx.message or "",
                customer_name=customer_name or None,
                prior_hint=prior,
            )

            # Sticky update: persist on state only when the new hint
            # carries equal-or-better confidence than what we had.
            # This prevents a low-signal turn from erasing a strong
            # prior classification.
            if (
                hint.value in ("male", "female")
                and hint.confidence >= prior.confidence
            ):
                state.customer_gender_hint = hint.value
                state.customer_gender_confidence = float(hint.confidence)

            return apply_gender_to_social_reply(reply, hint)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GenderHint] swallowed exception (returning unmodified "
                "reply): %s", exc,
            )
            return reply

    def _should_skip_greet(self, ctx: BrainContext) -> bool:
        """True when sending the greeting template would be wrong.

        Two cases trigger the skip:
          - state already says greeted (one greeting per conversation, ever)
          - the customer is past discovery (deciding/ordering/checkout/etc.)
            and re-greeting would erase their context

        Kept tiny and pure so the rule is auditable from a log line.
        """
        from ..state.stages import (  # noqa: PLC0415 — local import to avoid cycle
            STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT,
            STAGE_COMPLETE, STAGE_SUPPORT,
        )
        state = ctx.state
        if getattr(state, "greeted", False):
            return True
        if getattr(state, "stage", "") in (
            STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT,
            STAGE_COMPLETE, STAGE_SUPPORT,
        ):
            return True
        return False

    def _with_follow_up(
        self,
        text: str,
        ctx: BrainContext,
        *,
        topic: str = "",
    ) -> str:
        # Order-flow resume hint takes priority over generic suggestion
        # follow-ups: when the customer asks a side question ("كم
        # التوصيل؟") mid-order, we answer the FAQ AND remind them where
        # we left off so the conversation doesn't lose momentum.
        # Location/branch turns must never carry an order nudge — the
        # maps CTA is the only asset on those replies.
        if topic != TOPIC_LOCATION:
            resume = self._order_resume_hint(ctx)
            if resume and resume not in text:
                text = f"{text}\n\n{resume}"
                return text

        suggestion = getattr(ctx, "suggestion", None)
        if not suggestion or not suggestion.needs_follow_up_question:
            return text

        follow_up = (suggestion.follow_up_question or "").strip()
        if not follow_up or follow_up in text:
            return text

        return f"{text}\n\n{follow_up}"

    @staticmethod
    def _order_resume_hint(ctx: BrainContext) -> str:
        """Return a short Arabic prompt to resume an in-progress order,
        or '' when no order is active. Triggered after FAQ replies so
        customers don't lose track of the order flow when they ask a
        side question (delivery / store info / etc.)."""
        try:
            prep = getattr(ctx.state, "order_prep", None)
            focus = getattr(ctx.state, "current_product_focus", None) or {}
            if not prep or not (focus or getattr(prep, "product_id", "")):
                return ""

            product_title = (focus or {}).get("title") or getattr(prep, "product_name", "") or "المنتج"

            # 1. Pending product options take priority — they're the most
            # specific blocker.
            pending_options = []
            try:
                meta = list(getattr(prep, "product_options_meta", None) or [])
                picked = dict(getattr(prep, "product_options", None) or {})
                for g in meta:
                    name = (g.get("name") or "").strip()
                    if not name:
                        continue
                    if not g.get("required", True):
                        continue
                    if name.lower() in picked:
                        continue
                    pending_options.append(name)
            except Exception:
                pending_options = []

            if pending_options:
                if len(pending_options) == 1:
                    return f"نكمل اختيار {pending_options[0]} لـ *{product_title}*؟ 👇"
                joined = "، ".join(pending_options[:-1]) + f" و{pending_options[-1]}"
                return f"نكمل اختيار {joined} لـ *{product_title}*؟ 👇"

            # 2. Otherwise hint the most likely missing checkout slot.
            missing = list(getattr(prep, "missing_fields", None) or [])
            slot_labels = {
                "customer_first_name": "اسمك",
                "customer_last_name":  "اسمك",
                "customer_name":       "اسمك",
                "city":                "المدينة",
                "address":             "العنوان أو الرمز الوطني",
                "address_line":        "العنوان أو الرمز الوطني",
                "short_address_code":  "الرمز الوطني (أو رابط الموقع)",
                "google_maps_url":     "رابط الموقع",
            }
            for slot in missing:
                label = slot_labels.get(slot)
                if label:
                    return f"نكمل بعدها {label} لإتمام طلب *{product_title}*؟"

            # 3. Order ready to create — prompt confirmation.
            return f"نكمل إنشاء طلب *{product_title}* الآن؟"
        except Exception:
            return ""

    @staticmethod
    def _apply_established_greeting_etiquette(
        text: str,
        ctx: BrainContext,
        decision: Decision,
    ) -> str:
        """Prepend level-matched salam return on established persona greetings.

        Ritual reciprocity stays deterministic; the LLM body is personality.
        """
        args = decision.args or {}
        from ..persona_expression import (  # noqa: PLC0415
            PERSONA_KIND_GREETING,
            PERSONA_TOPIC_SOCIAL,
        )

        if (
            str(args.get("topic") or "") != PERSONA_TOPIC_SOCIAL
            or str(args.get("persona_kind") or "") != PERSONA_KIND_GREETING
        ):
            return text
        from .greeting_etiquette import (  # noqa: PLC0415
            apply_greeting_etiquette,
            customer_message_for_etiquette,
        )

        return apply_greeting_etiquette(
            text,
            customer_message_for_etiquette(ctx),
            ctx.state,
            tenant_id=getattr(ctx, "tenant_id", None),
        )

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
                # The pipeline always builds reply_state before calling us.
                # If we land here something earlier failed silently. We
                # rebuild a minimal one from ctx instead of falling back to
                # a static template — the user's contract is "LLM ALWAYS
                # gets intent + state + product + goal", and a static
                # template breaks that contract.
                logger.error(
                    "[Composer._llm_compose] reply_state missing — "
                    "rebuilding minimal one | tenant=%s intent=%s stage=%s",
                    ctx.tenant_id,
                    getattr(ctx.intent, "name", "?"),
                    getattr(ctx.state, "stage", "?"),
                )
                reply_state = self._minimal_reply_state(ctx)

            prompt = build_brain_reply_prompt(reply_state)
            locale = str(ctx.profile.get("preferred_language") or "ar")
            history_messages = _as_ai_history(ctx.history, ctx.message)

            try:
                from modules.ai.brain.truth_surface import (  # noqa: PLC0415
                    run_truth_surface_shadow_audit,
                    run_uts_v1_shadow,
                )
                from modules.ai.brain.truth_surface.flags import (  # noqa: PLC0415
                    is_truth_surface_shadow_enabled,
                    is_uts_v1_enforce_enabled,
                    is_uts_v1_shadow_enabled,
                )

                if (
                    is_truth_surface_shadow_enabled()
                    or is_uts_v1_shadow_enabled()
                    or is_uts_v1_enforce_enabled()
                ):
                    run_truth_surface_shadow_audit(
                        reply_state,
                        tenant_id=ctx.tenant_id,
                        history_messages=history_messages,
                        goal_regimen_bundle=getattr(ctx, "goal_regimen_bundle", None),
                        sales_context=ctx.sales_context,
                        full_merchant_context=(
                            ctx.merchant_context
                            if isinstance(getattr(ctx, "merchant_context", None), dict)
                            else None
                        ),
                    )
                    run_uts_v1_shadow(
                        reply_state,
                        tenant_id=ctx.tenant_id,
                        goal_regimen_bundle=getattr(ctx, "goal_regimen_bundle", None),
                        history_messages=history_messages,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — shadow must never break compose
                pass

            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    generate_ai_reply,
                    tenant_id=ctx.tenant_id,
                    customer_phone=ctx.customer_phone,
                    message=ctx.message,
                    store_name=ctx.facts.store_name,
                    channel="whatsapp",
                    locale=locale,
                    history=history_messages,
                    context_metadata={
                        "brain_state": asdict(reply_state),
                        "suggestion": asdict(ctx.suggestion) if ctx.suggestion else {},
                        "sales_context": ctx.sales_context.to_dict() if ctx.sales_context else {},
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
            # The downstream "clear-intent fallback" safety net
            # (modules.ai.postprocess.safety_nets) will REWRITE this
            # line into an intent-aware nudge whenever the customer's
            # message had a recognisable intent (offers, price,
            # honey product, store link, shipping, payment, ordering
            # verb). We keep this generic copy ONLY as the absolute
            # last-resort wording — never the customer-facing line
            # for clear questions.
            return (
                "عذراً، تأخّر الرد قليلاً. "
                "هل يمكنك إعادة سؤالك؟ أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
            )
        except Exception as exc:
            logger.error("[Composer._llm_compose] thin path error: %s", exc)
            return await self._legacy_llm_compose(ctx, result, timeout_seconds=15)

    def _minimal_reply_state(self, ctx: BrainContext):
        """Build a degraded BrainReplyState from ctx alone.

        Used only as a safety net when the pipeline did not attach a full
        reply_state. We still surface the four fields the user demanded
        (intent, stage, current product, response goal) so the LLM stays
        grounded.
        """
        from ..types import BrainReplyState  # noqa: PLC0415
        return BrainReplyState(
            store_name=getattr(ctx.facts, "store_name", "") or "",
            stage=getattr(ctx.state, "stage", "discovery"),
            customer_goal=getattr(ctx.state, "customer_goal", "") or "",
            selected_product=getattr(ctx.state, "current_product_focus", None),
            recommended_next_step=getattr(ctx.state, "recommended_next_step", "") or "",
            intent_name=getattr(ctx.intent, "name", "") or "",
            response_goal="answer the customer's last message in line with the current stage",
        )

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
        return T.generic_fallback(variant=self._variant_idx(ctx))


def _as_ai_history(history: List[Dict[str, Any]], current_message: str) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for turn in (history or [])[-20:]:
        direction = str(turn.get("direction") or "").strip()
        body = str(turn.get("body") or "").strip()
        if not body:
            continue
        if direction in {"in", "inbound"}:
            role = "user"
        elif direction in {"out", "outbound"}:
            role = "assistant"
        else:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n{body}"
        else:
            messages.append({"role": role, "content": body})

    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": current_message})
    elif messages[-1]["content"] != current_message:
        messages.append({"role": "user", "content": current_message})
    return messages
