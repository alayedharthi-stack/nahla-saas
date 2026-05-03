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
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_WEB_SEARCH,
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

        # ── State guard (defense-in-depth) ─────────────────────────────────
        # The DecisionEngine already gates greetings on `state.greeted` and
        # the in-progress sales stage, but a stray ACTION_GREET produced by
        # learned-policy layers, partial state loss, or future decoration
        # would still land here. We refuse to send the greeting template
        # twice in the same conversation, period — and we refuse to send it
        # at all once the customer is in deciding/ordering/checkout.
        if action == ACTION_GREET and self._should_skip_greet(ctx):
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
            variant = self._variant_idx(ctx)
            text = T.greeting(store_name=ctx.facts.store_name, variant=variant)
            if self._is_duplicate(text, ctx):
                text = T.greeting(store_name=ctx.facts.store_name, variant=(variant + 1) % 3)
            return text

        # ── FAQ ────────────────────────────────────────────────────────────
        if action == ACTION_FAQ_REPLY:
            payload = data.get("payload", {}) or {}
            topic = data.get("topic", "")
            if topic == TOPIC_IDENTITY:
                return self._with_follow_up(
                    T.faq_identity(store_name=ctx.facts.store_name),
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

            # INVARIANT: pending_candidates = EXACTLY the products shown in
            # the numbered list.  The customer reads "1. بنطلون" and expects
            # sending "1" to give them بنطلون — any mismatch causes the
            # "بلوزة غير متوفر" bug.
            # WA quick-reply buttons are capped at 3 (platform limit) but
            # the candidates list holds ALL shown products.
            candidates = safe_products   # all of them — shown & stored
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
            # Store ALL candidates so pick-by-number always resolves correctly.
            result.data["pending_candidates"] = candidates
            variant = self._variant_idx(ctx)
            text = T.narrow_choices(products=candidates, variant=variant)
            if self._is_duplicate(text, ctx):
                text = T.narrow_choices(products=candidates, variant=(variant + 1) % 3)
            return text

        # ── Draft order ────────────────────────────────────────────────────
        if action == ACTION_PROPOSE_DRAFT_ORDER:
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
            if data.get("needs_options"):
                _missing_groups = data.get("missing_option_groups", []) or []
                # WhatsApp quick-reply buttons for the FIRST pending group.
                # Buttons are limited to 3 by WhatsApp; if the group has
                # more values, take the first 3 as buttons (the text body
                # still shows the full numbered list so nothing is hidden).
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
            return T.draft_order_created(
                product=data.get("product", {}),
                reference=str(data.get("reference", "")),
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
            if not result.success or data.get("message") == "no_orders_found":
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

        # ── Clarify ────────────────────────────────────────────────────────
        if action == ACTION_CLARIFY:
            return T.clarify(question=data.get("question", ""))

        # ── Narrow choices ─────────────────────────────────────────────────
        if action == ACTION_NARROW:
            variant = self._variant_idx(ctx)
            text = T.narrow_choices(products=data.get("products", []), variant=variant)
            if self._is_duplicate(text, ctx):
                text = T.narrow_choices(products=data.get("products", []), variant=(variant + 1) % 3)
            return text

        # ── Handoff ────────────────────────────────────────────────────────
        if action == ACTION_HANDOFF:
            variant = self._variant_idx(ctx)
            text = T.handoff(variant=variant)
            if self._is_duplicate(text, ctx):
                text = T.handoff(variant=(variant + 1) % 3)
            return text

        # ── LLM fallback ───────────────────────────────────────────────────
        if action == ACTION_LLM_REPLY:
            return await self._llm_compose(ctx, result)

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

    def _with_follow_up(self, text: str, ctx: BrainContext) -> str:
        # Order-flow resume hint takes priority over generic suggestion
        # follow-ups: when the customer asks a side question ("كم
        # التوصيل؟") mid-order, we answer the FAQ AND remind them where
        # we left off so the conversation doesn't lose momentum.
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
