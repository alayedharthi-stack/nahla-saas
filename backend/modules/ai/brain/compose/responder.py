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

_CATALOG_QA_QUESTION_KINDS = frozenset({"price", "availability"})


def catalog_compose_products_for_search_turn(
    *,
    question_kind: str,
    category_filtered_facts: list[Dict[str, Any]],
    display_candidates: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Truth-only compose rows (price/availability) vs orderable display slice (browse)."""
    if question_kind in _CATALOG_QA_QUESTION_KINDS:
        return list(category_filtered_facts)
    return list(display_candidates)


def _trusted_search_compose_candidates(
    data: Dict[str, Any],
    decision: Any,
) -> List[Dict[str, Any]]:
    """Compose-local search candidates. Never mutates executor ``products``.

    A confirmed identity singleton is stored on ``data["product"]`` with
    ``products=[]`` so pipeline 6b does not restamp browse focus. Compose
    may use that singleton only when presentation identity is grounded
    AND the row already has structured catalog identity.
    """
    products = [p for p in (data.get("products") or []) if isinstance(p, dict)]
    if products:
        return products
    args = getattr(decision, "args", None) or {}
    grounded = (
        data.get("presentation_identity_grounded") is True
        or args.get("presentation_identity_grounded") is True
    )
    if not grounded:
        return []
    product = data.get("product")
    from ..commerce.commerce_focus_owner import (  # noqa: PLC0415
        has_structured_catalog_identity,
    )

    if not has_structured_catalog_identity(product):
        return []
    return [product]


from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CATALOG_NAVIGATE,
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
    ACTION_SELECT_PURCHASE_CHANNEL,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SOCIAL_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SUGGEST_COUPON,
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_TRACK_ORDER,
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_PAYMENT_CONTINUATION_REPLY,
    ACTION_WEB_SEARCH,
    ACTION_OUT_OF_SCOPE,
    ACTION_PAYMENT_TRANSFER_PROMISE,
    ACTION_PRODUCT_MEDIA_IDENTITY,
    ACTION_VARIANT_PRICING,
)
from ..execution.faq import (
    TOPIC_IDENTITY,
    TOPIC_LOCATION,
    TOPIC_OWNER_CONTACT,
    TOPIC_SHIPPING,
    TOPIC_STORE_ABOUT,
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
        text = await self._compose_impl(decision, result, ctx)
        try:
            from core.outbound_text_policy import attach_compose_provenance  # noqa: PLC0415

            attach_compose_provenance(result, decision=decision, ctx=ctx, text=text)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — policy tag must not block compose
            pass
        return text

    async def _compose_impl(
        self,
        decision: Decision,
        result: ActionResult,
        ctx: BrainContext,
    ) -> str:
        action = decision.action
        if not isinstance(result.data, dict):
            result.data = {}
        data = result.data

        if action == ACTION_SELECT_PURCHASE_CHANNEL:
            topic = ""
            if isinstance(data, dict):
                topic = str(data.get("execution_topic") or "").strip()
            if not topic:
                topic = str((decision.args or {}).get("topic") or "").strip()
            decision.args = dict(decision.args or {})
            if topic:
                decision.args["topic"] = topic
            if isinstance(data, dict) and data.get("accepted") is False:
                decision.args["topic"] = "purchase_channel_selection"
                decision.args["response_goal"] = "help_customer_choose_purchase_channel"
            elif topic == "whatsapp_quick_order":
                decision.args.setdefault(
                    "response_goal", "collect_product_for_whatsapp_order"
                )
            elif topic == "online_store_redirect":
                decision.args.setdefault(
                    "response_goal", "guide_customer_to_online_store"
                )
            elif topic == "showroom_visit":
                decision.args.setdefault(
                    "response_goal", "guide_customer_to_showroom"
                )
            decision.action = ACTION_LLM_REPLY
            action = ACTION_LLM_REPLY

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
            from ..cost.intent_cost_policy import should_avoid_llm_for_intent  # noqa: PLC0415

            if should_avoid_llm_for_intent(getattr(ctx.intent, "name", "")):
                re_greet_requested = True
            else:
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
            from ..cost.intent_cost_policy import is_routine_llm_avoid_enabled  # noqa: PLC0415

            persona = getattr(ctx.facts, "assistant_name", "") or ""
            if is_routine_llm_avoid_enabled():
                from ..persona.integration import try_enforce_persona_compose  # noqa: PLC0415
                from ..persona.surface_resolver import resolve_greet_surface  # noqa: PLC0415

                _greet_surface = resolve_greet_surface(ctx, re_greet=re_greet_requested)
                _persona_result = None
                if _greet_surface:
                    _persona_result = await try_enforce_persona_compose(
                        ctx,
                        surface=_greet_surface,
                        action_result=result,
                    )
                if _persona_result and (_persona_result.text or "").strip():
                    text = _persona_result.text
                else:
                    from .persona_template_engine import pick_persona_greeting  # noqa: PLC0415

                    text = pick_persona_greeting(ctx, re_greet=re_greet_requested)
            else:
                from ..persona.integration import try_enforce_persona_compose  # noqa: PLC0415
                from ..persona.surface_resolver import resolve_greet_surface  # noqa: PLC0415
                from ..persona_expression import (  # noqa: PLC0415
                    PERSONA_KIND_GREETING,
                    PERSONA_TOPIC_SOCIAL,
                    is_established_greet_persona_compose_enabled,
                )

                _greet_surface = resolve_greet_surface(ctx, re_greet=re_greet_requested)
                _persona_result = None
                if _greet_surface:
                    _persona_result = await try_enforce_persona_compose(
                        ctx,
                        surface=_greet_surface,
                        action_result=result,
                    )
                if _persona_result and (_persona_result.text or "").strip():
                    text = _persona_result.text
                elif is_established_greet_persona_compose_enabled():
                    _greet_decision = Decision(
                        action=ACTION_LLM_REPLY,
                        args={
                            "topic": PERSONA_TOPIC_SOCIAL,
                            "persona_kind": PERSONA_KIND_GREETING,
                            "block_commerce_escalation": True,
                        },
                        reason="greet — persona_social compose (default path)",
                    )
                    result.data["chosen_path"] = "greet_persona_compose"
                    text = await self._llm_compose(
                        ctx, result, decision=_greet_decision,
                    )
                else:
                    variant = self._variant_idx(ctx)
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
            try:
                from core.outbound_text_policy import mark_compose_template  # noqa: PLC0415

                mark_compose_template(result, layer="faq_template")
            except Exception:  # noqa: BLE001  # noqa: silent-ok — policy tag must not block FAQ compose
                pass
            # FAQReplyHandler stores structured facts on result.data["payload"].
            # This bind was removed in 91462b70 and caused NameError on FAQ topics.
            raw_payload = data.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            topic = data.get("topic", "")
            if topic == TOPIC_IDENTITY:
                return self._with_follow_up(
                    T.faq_identity(
                        store_name=ctx.facts.store_name,
                        assistant_name=getattr(ctx.facts, "assistant_name", "") or "",
                    ),
                    ctx,
                    result=result,
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
                    result=result,
                )
            if topic == TOPIC_STORE_INFO:
                return self._with_follow_up(
                    T.faq_store_info(
                        store_name=payload.get("store_name", ""),
                        store_url=payload.get("store_url", ""),
                        store_description=payload.get("store_description", ""),
                    ),
                    ctx,
                    result=result,
                )
            if topic == TOPIC_STORE_ABOUT:
                # Reuse existing faq_store_info surface (no new prose template).
                return self._with_follow_up(
                    T.faq_store_info(
                        store_name=payload.get("store_name", ""),
                        store_url="",
                        store_description=payload.get("store_description", ""),
                    ),
                    ctx,
                    result=result,
                )
            if topic == TOPIC_LOCATION:
                return self._with_follow_up(
                    T.faq_location(
                        store_name=payload.get("store_name", ""),
                        maps_url=payload.get("maps_url", ""),
                    ),
                    ctx,
                    topic=TOPIC_LOCATION,
                    result=result,
                )
            if topic == TOPIC_OWNER_CONTACT:
                return self._with_follow_up(
                    T.faq_owner_contact(
                        contact_phone=payload.get("contact_phone", ""),
                        contact_email=payload.get("contact_email", ""),
                        store_url=payload.get("store_url", ""),
                        social_links=payload.get("social_links", {}) or {},
                    ),
                    ctx,
                    result=result,
                )
            if topic == "cash_on_delivery":
                # Pack B: do not emit settings-default payment invention.
                # Structured evidence is attached for persona compose; this
                # FAQ branch must not be the primary customer wording path.
                from ..commerce.cod_policy_evidence import (  # noqa: PLC0415
                    load_cod_policy_evidence,
                    merchant_capability_facts_for_compose,
                )

                mc = getattr(ctx, "merchant_context", None) or {}
                facts = getattr(ctx, "facts", None)
                evidence = load_cod_policy_evidence(
                    merchant_context=mc if isinstance(mc, dict) else {},
                    merchant_capabilities=dict(
                        getattr(facts, "merchant_capabilities", None) or {}
                    ),
                    payment_methods=list(
                        getattr(facts, "payment_methods", None) or []
                    ),
                    payment_methods_source=str(
                        getattr(facts, "payment_methods_source", "") or ""
                    ),
                    salla_payments_status=str(
                        getattr(facts, "salla_payments_status", "") or ""
                    ),
                )
                result.data["cod_policy_evidence"] = {
                    "status": evidence.status,
                    "cash_on_delivery_enabled": evidence.cash_on_delivery_enabled,
                    "available_methods": list(evidence.available_methods),
                    "source": evidence.source,
                }
                result.data["merchant_capability_facts"] = (
                    merchant_capability_facts_for_compose(evidence)
                )
                # Empty string → fall through to persona/LLM compose with facts.
                return ""
            return T.generic_fallback(variant=self._variant_idx(ctx))

        # ── Catalog Navigator (owned presentation) ───────────────────────
        if action == ACTION_CATALOG_NAVIGATE:
            discovery_text = str(data.get("discovery_presentation_text") or data.get("product_lines") or "").strip()
            chosen_path = str(data.get("chosen_path") or (decision.args or {}).get("chosen_path") or "").strip()
            discovery_kind = str(data.get("discovery_output_kind") or "").strip().lower()
            if discovery_kind == "native_catalog":
                from ..catalog.navigation import PATH_NATIVE_CATALOG  # noqa: PLC0415

                entry = dict(data.get("native_catalog_entry") or {})
                result.data["native_catalog_entry"] = entry
                result.data["chosen_path"] = chosen_path or PATH_NATIVE_CATALOG
                result.data["turn_owner"] = str(data.get("turn_owner") or "catalog_navigation")
                result.data["owner_locked"] = bool(data.get("owner_locked"))
                result.data["owner_replaced"] = False
                result.data["navigator_owner"] = True
                # Customer-facing catalog copy is sent only after Meta accepts
                # ``catalog_message`` in the webhook wire layer.
                return ""
            if chosen_path and (
                discovery_text
                or chosen_path == "catalog_navigation_top_products_fallback"
            ):
                result.data["chosen_path"] = chosen_path
                result.data["turn_owner"] = str(data.get("turn_owner") or "catalog_navigation")
                result.data["owner_locked"] = bool(data.get("owner_locked"))
                result.data["owner_replaced"] = False
                result.data["navigator_owner"] = True
                from ..catalog.navigation import PATH_GROUP_PRODUCTS, PATH_GROUPS, PATH_TOP_FALLBACK  # noqa: PLC0415

                if discovery_kind == "collections" and chosen_path == PATH_GROUPS:
                    page_collections = list(data.get("collections") or [])
                    from ..catalog.collections_pagination import build_collection_quick_buttons  # noqa: PLC0415

                    buttons = build_collection_quick_buttons(
                        page_collections,
                        collections_next_available=bool(data.get("collections_next_available")),
                        collections_at_end=bool(data.get("collections_at_end")),
                    )
                    if buttons:
                        result.data["pending_buttons"] = buttons
                    result.data["pending_collections"] = page_collections
                elif discovery_kind == "products":
                    raw_products = list(data.get("products") or [])
                    from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                        apply_display_slice,
                        resolve_product_breadth_from_context,
                    )
                    from ..catalog.collections_pagination import (  # noqa: PLC0415
                        BUTTON_BACK_GROUPS,
                        BUTTON_MORE_PRODUCTS,
                    )

                    breadth = resolve_product_breadth_from_context(ctx, decision)
                    candidates, breadth_meta = apply_display_slice(raw_products, breadth)
                    result.data["product_breadth"] = breadth.to_log_dict()
                    result.data["product_breadth_meta"] = breadth_meta
                    result.data["pending_candidates"] = candidates
                    if chosen_path == PATH_GROUP_PRODUCTS:
                        nav_buttons: list[dict[str, Any]] = []
                        if bool(data.get("next_page_available")):
                            nav_buttons.append({
                                "type": "reply",
                                "reply": {"id": BUTTON_MORE_PRODUCTS, "title": "المزيد"},
                            })
                        nav_buttons.append({
                            "type": "reply",
                            "reply": {"id": BUTTON_BACK_GROUPS, "title": "رجوع للأقسام"},
                        })
                        if nav_buttons:
                            result.data["pending_buttons"] = nav_buttons[:3]
                if chosen_path == PATH_TOP_FALLBACK:
                    from ..persona.catalog_product_answer import (  # noqa: PLC0415
                        build_catalog_navigation_emergency_outcome,
                        try_compose_catalog_navigation_browse_answer,
                    )
                    from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                    nav_kwargs = {
                        "tenant_id": int(getattr(ctx, "tenant_id", 0) or 0),
                        "customer_phone": str(
                            getattr(ctx, "customer_phone", "") or "",
                        ),
                        "inbound_text": str(getattr(ctx, "message", "") or ""),
                        "products": list(data.get("products") or []),
                        "chosen_path": PATH_TOP_FALLBACK,
                        "navigator_no_groups_fallback": bool(
                            data.get("navigator_no_groups_fallback"),
                        ),
                        "decision_args": dict(decision.args or {}),
                        "ai_settings": _ai_settings_from_ctx(ctx),
                    }
                    try:
                        _nav_text, _nav_result, _nav_event = (
                            await try_compose_catalog_navigation_browse_answer(
                                **nav_kwargs,
                            )
                        )
                        if not (
                            (_nav_text or "").strip()
                            and isinstance(_nav_event, dict)
                            and _nav_event.get("compose_source")
                        ):
                            _nav_text, _nav_result, _nav_event = (
                                build_catalog_navigation_emergency_outcome(
                                    **nav_kwargs,
                                    reason="invalid_compose_outcome",
                                )
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "[RESPONDER] catalog_navigation_browse persona compose failed",
                        )
                        _nav_text, _nav_result, _nav_event = (
                            build_catalog_navigation_emergency_outcome(
                                **nav_kwargs,
                                reason=f"responder_exception:{type(exc).__name__}",
                            )
                        )
                    result.data.update(_nav_event)
                    result.data["chosen_path"] = PATH_TOP_FALLBACK
                    return (_nav_text or "").strip()
                return discovery_text
            return discovery_text or "ما ظهر عندي أقسام واضحة حالياً."

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

            discovery_text = str(data.get("discovery_presentation_text") or "").strip()
            selection_text = str(data.get("selection_presentation_text") or "").strip()
            if selection_text:
                result.data["chosen_path"] = "selection_context_presentation"
                return selection_text
            discovery_kind = str(data.get("discovery_output_kind") or "").strip().lower()
            if discovery_text and discovery_kind in {
                "products",
                "collections",
                "empty",
            }:
                result.data["chosen_path"] = f"discovery_presentation_{discovery_kind}"
                if discovery_kind == "products":
                    raw_products = list(data.get("products") or [])
                    safe_products = [
                        p for p in raw_products
                        if p.get("can_checkout", p.get("orderable", True))
                    ]
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
                    from ..commerce.product_presentation_selection import (  # noqa: PLC0415
                        apply_search_product_presentation,
                        build_standard_pick_buttons,
                        presentation_context_from_brain,
                    )

                    apply_search_product_presentation(
                        result.data,
                        candidates=candidates,
                        build_buttons=build_standard_pick_buttons,
                        **presentation_context_from_brain(
                            ctx,
                            decision,
                            resolved_product=(
                                data.get("product")
                                or getattr(
                                    getattr(ctx, "state", None),
                                    "current_product_focus",
                                    None,
                                )
                            ),
                        ),
                    )
                return discovery_text

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
                from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                    extract_resolved_product_subject,
                    log_clarification_leak,
                )
                from ..persona.catalog_product_answer import (  # noqa: PLC0415
                    build_catalog_search_miss_emergency_outcome,
                    try_compose_catalog_search_miss_answer,
                )
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                try:
                    subject = extract_resolved_product_subject(
                        ctx, query=query, inquiry_query=inquiry_query,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[RESPONDER] catalog_search_miss subject resolution failed",
                    )
                    subject = ""
                resolved_subject = subject or query
                try:
                    log_clarification_leak(
                        tenant_id=getattr(ctx, "tenant_id", None),
                        source="search_miss_compose",
                        normalized_subject=resolved_subject,
                        resolved_query=query or resolved_subject,
                        preview=str(ctx.message or "")[:80],
                        blocked_text=(
                            "search_miss_type_clarify:"
                            f"{data.get('message') or result.error or 'unknown'}"
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[RESPONDER] catalog_search_miss telemetry failed",
                    )
                try:
                    ai_settings = _ai_settings_from_ctx(ctx)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[RESPONDER] catalog_search_miss AI settings failed",
                    )
                    ai_settings = {}

                result.data["chosen_path"] = "catalog_miss_resolved_subject"
                result.data["compose_route_attempted"] = True
                miss_kwargs = {
                    "tenant_id": int(getattr(ctx, "tenant_id", 0) or 0),
                    "customer_phone": str(
                        getattr(ctx, "customer_phone", "") or "",
                    ),
                    "inbound_text": str(getattr(ctx, "message", "") or ""),
                    "resolved_subject": resolved_subject,
                    "catalog_search_query": query,
                    "chosen_path": "catalog_miss_resolved_subject",
                    "ai_settings": ai_settings,
                }
                try:
                    _miss_text, _miss_result, _miss_event = (
                        await try_compose_catalog_search_miss_answer(
                            **miss_kwargs,
                        )
                    )
                    if not (
                        (_miss_text or "").strip()
                        and isinstance(_miss_event, dict)
                        and _miss_event.get("compose_source")
                    ):
                        _miss_text, _miss_result, _miss_event = (
                            build_catalog_search_miss_emergency_outcome(
                                **miss_kwargs,
                                reason="invalid_compose_outcome",
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "[RESPONDER] catalog_search_miss persona compose failed",
                    )
                    _miss_text, _miss_result, _miss_event = (
                        build_catalog_search_miss_emergency_outcome(
                            **miss_kwargs,
                            reason=f"compose_exception:{type(exc).__name__}",
                        )
                    )
                result.data.update(_miss_event)
                result.data["chosen_path"] = "catalog_miss_resolved_subject"
                result.data["compose_route_attempted"] = True
                return (_miss_text or "").strip()
            # Validate every product before we show it: if a product that
            # the executor already filtered as orderable somehow lacks
            # can_checkout=True, log it as a catalog bug and exclude it.
            # This prevents "product listed then immediately rejected" UX.
            # Confirmed identity singletons live on data["product"] with
            # products=[] — recover them locally without mutating executor state.
            raw_products = _trusted_search_compose_candidates(data, decision)
            _search_query = str(
                (decision.args or {}).get("query") or data.get("query") or ""
            ).strip()
            _search_source = str(
                (decision.args or {}).get("source") or ""
            ).strip().lower()
            from ..persona.catalog_product_answer import (  # noqa: PLC0415
                catalog_fact_product_rows,
                classify_catalog_question_kind,
                try_compose_catalog_product_answer,
            )
            from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
                active_category_from_state,
                filter_products_for_browse_turn,
                resolve_browse_category_scope,
            )

            _question_kind = classify_catalog_question_kind(
                str(getattr(ctx, "message", "") or ""),
                query=_search_query,
                decision_args=dict(decision.args or {}),
            )
            _catalog_fact_products: list = []
            if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                _catalog_fact_products = list(data.get("catalog_fact_products") or [])
            _category_scope = (
                resolve_browse_category_scope(
                    ctx.message or "",
                    _search_query,
                    active_category=active_category_from_state(
                        getattr(ctx, "state", None),
                    ),
                    source=_search_source,
                )
                or str((decision.args or {}).get("category_scope") or "").strip()
            )
            _browse_filter_kwargs = {
                "message": ctx.message or "",
                "query": _search_query,
                "source": _search_source,
                "last_browse_query": str(
                    getattr(getattr(ctx, "state", None), "last_browse_query", "") or ""
                ),
                "state": getattr(ctx, "state", None),
                "db": getattr(ctx, "_db", None),
                "tenant_id": getattr(ctx, "tenant_id", None),
            }

            safe_products: list = []
            for _p in raw_products:
                if _p.get("can_checkout", _p.get("orderable", True)):
                    safe_products.append(_p)
                elif _question_kind in _CATALOG_QA_QUESTION_KINDS:
                    logger.info(
                        "[CATALOG] non-orderable product kept for Q&A facts only | "
                        "name=%r external_id=%s can_checkout=%s",
                        _p.get("title"),
                        _p.get("external_id"),
                        _p.get("can_checkout"),
                    )
                else:
                    logger.warning(
                        "[CATALOG] listed product failed validation | bug=True "
                        "name=%r external_id=%s can_checkout=%s orderable=%s "
                        "— removed from displayed list",
                        _p.get("title"), _p.get("external_id"),
                        _p.get("can_checkout"), _p.get("orderable"),
                    )

            _pre_category_count = len(safe_products)
            safe_products = filter_products_for_browse_turn(
                safe_products,
                **_browse_filter_kwargs,
            )
            _category_filter_dropped = max(0, _pre_category_count - len(safe_products))

            if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                _pre_facts_count = len(raw_products) + len(_catalog_fact_products)
                facts_products = filter_products_for_browse_turn(
                    list(raw_products) + list(_catalog_fact_products),
                    **_browse_filter_kwargs,
                )
                _facts_category_dropped = max(0, _pre_facts_count - len(facts_products))
            else:
                facts_products = list(safe_products)
                _facts_category_dropped = _category_filter_dropped

            if _question_kind not in _CATALOG_QA_QUESTION_KINDS and not safe_products:
                return T.no_products(variant=self._variant_idx(ctx))

            from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                apply_display_slice,
                log_product_breadth,
                resolve_product_breadth_from_context,
            )
            breadth = resolve_product_breadth_from_context(ctx, decision)
            if safe_products:
                candidates, breadth_meta = apply_display_slice(safe_products, breadth)
                log_product_breadth(
                    tenant_id=getattr(ctx, "tenant_id", None),
                    breadth=breadth,
                    total=len(safe_products),
                    shown=len(candidates),
                    action=action,
                )
            else:
                candidates = []
                breadth_meta = {}
            result.data["product_breadth"] = breadth.to_log_dict()
            result.data["product_breadth_meta"] = breadth_meta

            compose_products = catalog_compose_products_for_search_turn(
                question_kind=_question_kind,
                category_filtered_facts=facts_products,
                display_candidates=candidates,
            )
            _catalog_fact_rows: list[dict[str, Any]] = []
            if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                _catalog_fact_rows = catalog_fact_product_rows(compose_products)
                if _catalog_fact_rows:
                    result.data["catalog_fact_products"] = _catalog_fact_rows

            _catalog_text: str | None = None
            _catalog_event: dict | None = None
            try:
                from ..persona.catalog_product_answer import (  # noqa: PLC0415
                    build_catalog_product_answer_emergency_outcome,
                    try_compose_catalog_product_answer,
                )
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                _catalog_kwargs = {
                    "tenant_id": int(getattr(ctx, "tenant_id", 0) or 0),
                    "customer_phone": str(getattr(ctx, "customer_phone", "") or ""),
                    "inbound_text": str(getattr(ctx, "message", "") or ""),
                    "products": list(compose_products),
                    "catalog_search_query": _search_query,
                    "search_result_count": len(facts_products),
                    "category_scope": _category_scope,
                    "allowed_category": _category_scope,
                    "question_kind": _question_kind,
                    "category_filter_dropped": _facts_category_dropped,
                    "display_count": len(candidates),
                    "decision_args": dict(decision.args or {}),
                    "ai_settings": _ai_settings_from_ctx(ctx),
                }
                if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                    _catalog_text, _catalog_result, _catalog_event = (
                        await try_compose_catalog_product_answer(
                            **_catalog_kwargs,
                        )
                    )
                    if not (
                        (_catalog_text or "").strip()
                        and isinstance(_catalog_event, dict)
                        and _catalog_event.get("compose_source")
                    ):
                        _catalog_text, _catalog_result, _catalog_event = (
                            build_catalog_product_answer_emergency_outcome(
                                **_catalog_kwargs,
                                reason="invalid_compose_outcome",
                            )
                        )
                else:
                    _catalog_text, _catalog_result, _catalog_event = (
                        await try_compose_catalog_product_answer(
                            **_catalog_kwargs,
                        )
                    )
                    if _catalog_result is not None and (_catalog_text or "").strip():
                        if isinstance(_catalog_event, dict):
                            result.data.update(_catalog_event)
            except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — catalog persona optional
                logger.exception("[RESPONDER] catalog_product_answer compose failed")
                if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                    _catalog_text, _catalog_result, _catalog_event = (
                        build_catalog_product_answer_emergency_outcome(
                            **_catalog_kwargs,
                            reason=f"responder_exception:{type(exc).__name__}",
                        )
                    )

            if (
                _question_kind in _CATALOG_QA_QUESTION_KINDS
                and isinstance(_catalog_event, dict)
                and (_catalog_text or "").strip()
            ):
                result.data.update(_catalog_event)
                _persist_fact_rows = _catalog_fact_rows or catalog_fact_product_rows(
                    compose_products,
                )
                if _persist_fact_rows:
                    result.data["catalog_fact_products"] = _persist_fact_rows
                _card_rows = list(candidates or compose_products or [])
                if len(_card_rows) == 1:
                    from ..commerce.product_presentation_selection import (  # noqa: PLC0415
                        apply_search_product_presentation,
                        build_standard_pick_buttons,
                        presentation_context_from_brain,
                    )

                    _resolved = data.get("product")
                    if not _resolved and compose_products:
                        _resolved = compose_products[0]
                    if not _resolved:
                        _resolved = getattr(
                            getattr(ctx, "state", None), "current_product_focus", None,
                        )
                    apply_search_product_presentation(
                        result.data,
                        candidates=_card_rows,
                        build_buttons=build_standard_pick_buttons,
                        **presentation_context_from_brain(
                            ctx,
                            decision,
                            resolved_product=_resolved,
                        ),
                    )
                return (_catalog_text or "").strip()

            if (_catalog_text or "").strip() and isinstance(_catalog_event, dict):
                if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                    _persist_fact_rows = _catalog_fact_rows or catalog_fact_product_rows(
                        compose_products,
                    )
                    if _persist_fact_rows:
                        result.data["catalog_fact_products"] = _persist_fact_rows
                if _question_kind not in _CATALOG_QA_QUESTION_KINDS:
                    from ..commerce.product_presentation_selection import (  # noqa: PLC0415
                        apply_search_product_presentation,
                        build_standard_pick_buttons,
                        presentation_context_from_brain,
                        resolve_browse_presentation_candidates,
                    )

                    _resolved = (
                        data.get("product")
                        or getattr(getattr(ctx, "state", None), "current_product_focus", None)
                    )
                    _stamp_rows = resolve_browse_presentation_candidates(
                        display_candidates=candidates,
                        compose_products=compose_products,
                        executor_products=list(data.get("products") or []),
                        resolved_product=_resolved if isinstance(_resolved, dict) else None,
                        catalog_product_ids=list(
                            result.data.get("catalog_product_ids") or []
                        ),
                    )
                    if _stamp_rows:
                        apply_search_product_presentation(
                            result.data,
                            candidates=_stamp_rows,
                            build_buttons=build_standard_pick_buttons,
                            **presentation_context_from_brain(
                                ctx,
                                decision,
                                resolved_product=(
                                    _resolved if isinstance(_resolved, dict) else None
                                ),
                            ),
                        )
                elif _question_kind in _CATALOG_QA_QUESTION_KINDS:
                    _card_rows = list(candidates or compose_products or [])
                    if len(_card_rows) == 1:
                        # Single resolved product on availability/price Q&A:
                        # platform owns card/CTA; LLM keeps the prose.
                        from ..commerce.product_presentation_selection import (  # noqa: PLC0415
                            apply_search_product_presentation,
                            build_standard_pick_buttons,
                            presentation_context_from_brain,
                        )

                        _resolved = data.get("product")
                        if not _resolved and compose_products:
                            _resolved = compose_products[0]
                        if not _resolved:
                            _resolved = getattr(
                                getattr(ctx, "state", None), "current_product_focus", None,
                            )
                        apply_search_product_presentation(
                            result.data,
                            candidates=_card_rows,
                            build_buttons=build_standard_pick_buttons,
                            **presentation_context_from_brain(
                                ctx,
                                decision,
                                resolved_product=_resolved,
                            ),
                        )
                return (_catalog_text or "").strip()

            if not candidates:
                return T.no_products(variant=self._variant_idx(ctx))

            if _question_kind in _CATALOG_QA_QUESTION_KINDS:
                from ..persona.catalog_product_answer import (  # noqa: PLC0415
                    build_catalog_product_answer_emergency_outcome,
                )
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                _fallback_text, _fallback_result, _fallback_event = (
                    build_catalog_product_answer_emergency_outcome(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        products=list(compose_products),
                        catalog_search_query=_search_query,
                        search_result_count=len(facts_products),
                        category_scope=_category_scope,
                        allowed_category=_category_scope,
                        question_kind=_question_kind,
                        category_filter_dropped=_facts_category_dropped,
                        display_count=len(candidates),
                        decision_args=dict(decision.args or {}),
                        ai_settings=_ai_settings_from_ctx(ctx),
                        reason="compose_unavailable",
                    )
                )
                result.data.update(_fallback_event)
                return (_fallback_text or "").strip()

            # INVARIANT: pending_candidates = EXACTLY the products shown in
            # the numbered list when multi-candidate choices are used.
            # Single resolved product → rich card (no pick_N re-select).
            from ..commerce.product_presentation_selection import (  # noqa: PLC0415
                PRESENTATION_SINGLE_RICH,
                apply_search_product_presentation,
                build_standard_pick_buttons,
                presentation_context_from_brain,
                resolve_browse_presentation_candidates,
            )

            _resolved = (
                data.get("product")
                or getattr(getattr(ctx, "state", None), "current_product_focus", None)
            )
            _stamp_rows = resolve_browse_presentation_candidates(
                display_candidates=candidates,
                compose_products=compose_products,
                executor_products=list(data.get("products") or []),
                resolved_product=_resolved if isinstance(_resolved, dict) else None,
                catalog_product_ids=list(result.data.get("catalog_product_ids") or []),
            )
            _pres = apply_search_product_presentation(
                result.data,
                candidates=_stamp_rows or list(candidates or []),
                build_buttons=build_standard_pick_buttons,
                **presentation_context_from_brain(
                    ctx,
                    decision,
                    resolved_product=_resolved if isinstance(_resolved, dict) else None,
                ),
            )
            if _pres.kind == PRESENTATION_SINGLE_RICH:
                # Card/caption carries product facts; avoid pick_N list text.
                title = str((_pres.resolved_product or {}).get("title") or "").strip()
                return title
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
            if data.get("catalog_checkout_safe"):
                from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                    build_catalog_checkout_safe_reply_for_ctx,
                )

                result.data["chosen_path"] = "catalog_checkout_safe"
                return build_catalog_checkout_safe_reply_for_ctx(ctx)
            # The product reference we have can't be resolved on the store
            # (wrong id, deleted, not synced). Ask the customer to choose
            # again — never silently push a doomed order to Salla.
            if data.get("product_unsyncable"):
                _unsync_prod = data.get("product") or {}
                _keep_catalog = False
                try:
                    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
                        is_catalog_line_items_authoritative,
                    )

                    _keep_catalog = is_catalog_line_items_authoritative(ctx)
                except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard is best-effort
                    _keep_catalog = False
                if _keep_catalog:
                    logger.warning(
                        "[ORDER FLOW] product_unsyncable suppressed — catalog line items authoritative | "
                        "title=%r external_id=%r",
                        _unsync_prod.get("title"),
                        _unsync_prod.get("external_id"),
                    )
                    data.pop("product_unsyncable", None)
                else:
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
            if data.get("checkout_continue_after_address"):
                return await self._llm_compose(ctx, result, decision=decision)
            if data.get("salla_retry"):
                try:
                    from modules.ai.order_flow_v2.flags import (  # noqa: PLC0415
                        should_skip_legacy_order_flow_reply,
                    )

                    if should_skip_legacy_order_flow_reply():
                        return str(data.get("existing_reply") or "")
                except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not break compose
                    pass
                return T.salla_retry_message(
                    product=data.get("product", {}),
                    code=data.get("salla_address_code", ""),
                )
            if data.get("needs_collection"):
                legacy_reply = T.collect_order_details(
                    product=data.get("product", {}),
                    question=data.get("question", ""),
                    missing_fields=data.get("missing_fields", []),
                    is_first_ask=data.get("is_first_ask", True),
                )
                from core.reply_instruction import (  # noqa: PLC0415
                    build_order_slot_instruction,
                    is_operational_constrained_compose_enabled,
                )

                if is_operational_constrained_compose_enabled():
                    from core.constrained_operational_compose import (  # noqa: PLC0415
                        compose_constrained_operational_reply,
                    )

                    _missing = list(data.get("missing_fields") or [])
                    _slot = str(
                        data.get("next_slot")
                        or getattr(decision, "next_slot", None)
                        or ((decision.args or {}).get("next_slot") if decision is not None else "")
                        or (_missing[0] if _missing else "")
                        or ""
                    )
                    _instr = build_order_slot_instruction(
                        slot=_slot,
                        legacy_copy=legacy_reply,
                        product=data.get("product", {}),
                        is_first_ask=bool(data.get("is_first_ask", True)),
                        inbound_text=(ctx.message or ""),
                        next_missing_field=_slot or "none",
                        missing_fields=_missing,
                    )
                    _hist: list = []
                    for _row in (ctx.history or [])[-6:]:
                        _body = str(_row.get("body") or "").strip()
                        if not _body:
                            continue
                        _dir = str(_row.get("direction") or "inbound")
                        _role = (
                            "assistant"
                            if _dir in ("out", "outbound")
                            else "user"
                        )
                        _hist.append({"role": _role, "content": _body})
                    _slot_reply, _slot_meta = await compose_constrained_operational_reply(
                        db=None,
                        tenant_id=ctx.tenant_id,
                        phone=ctx.customer_phone,
                        instruction=_instr,
                        inbound_text=(ctx.message or ""),
                        history=_hist,
                    )
                    result.data["constrained_compose_meta"] = _slot_meta
                    return _slot_reply
                return legacy_reply
            if data.get("intent_only"):
                try:
                    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
                        is_catalog_line_items_authoritative,
                    )

                    if is_catalog_line_items_authoritative(ctx):
                        from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                            build_catalog_checkout_safe_reply_for_ctx,
                        )

                        return build_catalog_checkout_safe_reply_for_ctx(ctx)
                except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard is best-effort
                    pass
                return T.order_intent_captured(product=data.get("product", {}))
            _order_ref = str(
                data.get("reference") or data.get("order_id") or ""
            ).strip()
            if not _order_ref:
                try:
                    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
                        is_catalog_line_items_authoritative,
                    )

                    if is_catalog_line_items_authoritative(ctx):
                        from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                            build_catalog_checkout_safe_reply_for_ctx,
                        )

                        return build_catalog_checkout_safe_reply_for_ctx(ctx)
                except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard is best-effort
                    pass
                return T.order_intent_captured(product=data.get("product", {}))
            return T.draft_order_created(
                product=data.get("product", {}),
                reference=_order_ref,
                checkout_url=data.get("checkout_url", ""),
                total=data.get("total"),
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
        if action == ACTION_CUSTOMER_LEDGER_REPLY:
            reply = str(data.get("reply") or "").strip()
            if reply:
                return reply
            from core.customer_commerce_answerer import (  # noqa: PLC0415
                render_customer_commerce_reply,
                TOPIC_ORDER_HISTORY_COUNT,
            )
            from core.customer_commerce_ledger import (  # noqa: PLC0415
                CustomerCommerceProfile,
                CustomerIdentity,
                OrderCounts,
            )

            return render_customer_commerce_reply(
                str(decision.args.get("ledger_topic") or TOPIC_ORDER_HISTORY_COUNT),
                CustomerCommerceProfile(
                    customer_identity=CustomerIdentity(phone=ctx.customer_phone or ""),
                    order_counts=OrderCounts(),
                ),
            )

        if action == ACTION_PAYMENT_CONTINUATION_REPLY:
            reply = str(data.get("reply") or "").strip()
            if reply:
                return reply
            from core.payment_continuation_policy import (  # noqa: PLC0415
                resolve_payment_continuation_reply,
            )

            return resolve_payment_continuation_reply(
                getattr(ctx, "db", None) or getattr(ctx, "_db", None),
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

        if action == ACTION_TRACK_ORDER:
            try:
                from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                    should_use_generative_tracking_follow_up,
                )
                _track_reason = str(data.get("selected_reason") or "").strip()
                _bound_open = _track_reason in {
                    "latest_open_order",
                    "active_whatsapp_draft",
                }
                if (
                    not _bound_open
                    and should_use_generative_tracking_follow_up(
                        ctx.message or "",
                        history=ctx.history,
                        state=ctx.state,
                    )
                ):
                    return await self._llm_compose(ctx, result)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — tracking follow-up gate best-effort; fall through to template
                pass
            msg_key = str(data.get("message") or "").strip()
            if msg_key == "need_order_number":
                result.data["chosen_path"] = "track_order_need_order_number"
                result.data.pop("pending_candidates", None)
                return await self._compose_track_order_need_identifiers(ctx, result)
            if msg_key == "order_not_found":
                result.data["chosen_path"] = "track_order_not_found"
                result.data.pop("pending_candidates", None)
                return await self._compose_track_order_not_found(ctx, result)
            if not result.success or msg_key == "no_orders_found":
                try:
                    from core.order_creation_evidence import (  # noqa: PLC0415
                        resolve_track_order_fallback,
                    )

                    _honest = resolve_track_order_fallback(
                        state=ctx.state,
                        history=ctx.history,
                        db=getattr(ctx, "db", None),
                        tenant_id=getattr(ctx, "tenant_id", None),
                        conversation_id=getattr(ctx, "conversation_id", None),
                    )
                    if _honest:
                        return _honest
                except Exception:  # noqa: BLE001  # noqa: silent-ok — track evidence fallback best-effort
                    pass
                result.data["chosen_path"] = "track_order_need_order_number"
                result.data.pop("pending_candidates", None)
                return await self._compose_track_order_need_identifiers(ctx, result)
            result.data["chosen_path"] = "track_order_status"
            result.data.pop("pending_candidates", None)
            return self._with_follow_up(
                T.order_status(
                    reference=str(data.get("reference", "")),
                    status=data.get("status", ""),
                    status_label_ar=data.get("status_label_ar", ""),
                    total=data.get("total"),
                    currency=data.get("currency", "SAR"),
                    item_titles=data.get("item_titles") or [],
                    tracking_number=str(data.get("tracking_number") or ""),
                    tracking_url=str(data.get("tracking_url") or ""),
                    carrier=str(data.get("carrier") or data.get("shipping_provider") or ""),
                    shipping_status=str(data.get("shipping_status") or ""),
                    shipment_status=str(data.get("shipment_status") or ""),
                ),
                ctx,
                result=result,
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
                result=result,
            )

        # ── Addon recommendation ───────────────────────────────────────────
        if action == ACTION_RECOMMEND_ADDON:
            if not result.success:
                return T.generic_fallback(variant=self._variant_idx(ctx))
            return self._with_follow_up(
                T.addon_recommendations(products=data.get("products", [])),
                ctx,
                result=result,
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
            from ..persona_expression import is_template_only_social_category  # noqa: PLC0415
            from ..cost.intent_cost_policy import is_routine_llm_avoid_enabled  # noqa: PLC0415

            if is_template_only_social_category(category):
                reply = ""
                if category == "dua":
                    from ..persona.integration import try_enforce_persona_compose  # noqa: PLC0415

                    _dua_persona = await try_enforce_persona_compose(
                        ctx,
                        surface="dua",
                        action_result=result,
                    )
                    if _dua_persona and (_dua_persona.text or "").strip():
                        reply = _dua_persona.text
                if not (reply or "").strip():
                    v_main = self._variant_idx(ctx)
                    v_secondary = (len(ctx.history or []) // 3) % 5
                    reply = T.social_reply(
                        category=category,
                        variant=v_main,
                        sub_variant=v_secondary,
                        inbound_text=(ctx.message or ""),
                    )
            elif is_routine_llm_avoid_enabled():
                from ..persona.integration import try_enforce_persona_compose  # noqa: PLC0415
                from ..persona.surface_resolver import resolve_social_surface  # noqa: PLC0415

                _social_surface = resolve_social_surface(
                    category,
                    inbound_text=(ctx.message or ""),
                )
                _persona_result = None
                if _social_surface:
                    _persona_result = await try_enforce_persona_compose(
                        ctx,
                        surface=_social_surface,
                        action_result=result,
                    )
                if _persona_result and (_persona_result.text or "").strip():
                    reply = _persona_result.text
                else:
                    from .persona_template_engine import pick_persona_social_reply  # noqa: PLC0415

                    reply = pick_persona_social_reply(
                        ctx,
                        category,
                        inbound_text=(ctx.message or ""),
                    )
                try:
                    from ..postprocess.conversation_recovery import (  # noqa: PLC0415
                        is_generic_ack_stub_text,
                    )

                    if is_generic_ack_stub_text(reply):
                        reply = ""
                except Exception:  # noqa: BLE001  # noqa: silent-ok — social stub strip must not break compose
                    pass
            else:
                result.data["chosen_path"] = "social_persona_compose"
                reply = await self._compose_social_persona_ack(
                    ctx, result, social_category=category,
                )
                return self._apply_gender_hint(reply, ctx)
            try:
                from ..postprocess.social_reply_context_guard import (  # noqa: PLC0415
                    apply_social_reply_context_guard,
                )

                _srcg = apply_social_reply_context_guard(
                    reply,
                    inbound_text=(ctx.message or ""),
                    tenant_id=getattr(ctx, "tenant_id", None),
                )
                reply = _srcg.reply
            except Exception:  # noqa: BLE001  # noqa: silent-ok — social context guard best-effort
                pass
                result.data["chosen_path"] = "social_persona_compose_from_empty_template"
                reply = await self._compose_social_persona_ack(
                    ctx, result, social_category=category,
                )
                return reply
            if not (reply or "").strip():
                shc = getattr(ctx, "social_human_context", None)
                if (
                    shc
                    and shc.is_pure_social_turn
                    and (
                        shc.block_commerce_tail
                        or shc.category == "wellbeing_check"
                    )
                ):
                    result.data["chosen_path"] = "social_persona_compose_from_empty_template"
                    reply = await self._compose_social_persona_ack(
                        ctx,
                        result,
                        social_category=category or shc.category,
                    )
                    return reply
                result.data["chosen_path"] = "social_persona_compose_from_ack_stub_avoidance"
                reply = await self._compose_social_persona_ack(
                    ctx, result, social_category=category,
                )
                return reply
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
                # Project live variant/catalog rows into the grounding channel.
                _vp_facts = data.get("catalog_fact_products")
                if isinstance(_vp_facts, list) and _vp_facts:
                    result.data["catalog_fact_products"] = list(_vp_facts)
                return reply
            return T.clarify(question="أي خيار/حجم تقصد؟")

        # ── Product media identity (OCR + vision + catalog) ────────────────
        if action == ACTION_PRODUCT_MEDIA_IDENTITY:
            reply = str(data.get("reply_text") or "").strip()
            if reply:
                result.data["chosen_path"] = "product_media_identity"
                return reply
            return T.clarify(question="أرسل صورة أو اسم المنتج للتحقق.")

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

        # ── Customer-request coupon (canary) — structured facts, LLM wording ─
        if action == ACTION_CUSTOMER_COUPON_REQUEST:
            return await self._llm_compose(ctx, result, decision=decision)

        # ── LLM fallback ───────────────────────────────────────────────────
        if action == ACTION_LLM_REPLY:
            _reply_state = getattr(ctx, "reply_state", None)
            _known = dict(getattr(_reply_state, "known_facts", None) or {})
            _discovery_facts = dict(_known.get("general_offer_discovery_facts") or {})
            _product_sale_facts = dict(_known.get("product_sale_offer_facts") or {})
            _conditional_cc_facts = dict(_known.get("customer_conditional_coupon_facts") or {})
            _trusted_tc_facts = dict(_known.get("trusted_coupon_offer_facts") or {})

            if _conditional_cc_facts:
                try:
                    from ..persona.customer_conditional_coupon_answer import (  # noqa: PLC0415
                        try_compose_customer_conditional_coupon_answer,
                    )
                    from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                    _cc_ai_settings = _ai_settings_from_ctx(ctx)
                    _cc_text, _cc_result, _cc_event = (
                        await try_compose_customer_conditional_coupon_answer(
                            tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                            customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                            inbound_text=str(getattr(ctx, "message", "") or ""),
                            customer_conditional_coupon_facts=_conditional_cc_facts,
                            ai_settings=_cc_ai_settings,
                        )
                    )
                    if _cc_result is not None and (_cc_text or "").strip():
                        if isinstance(_cc_event, dict):
                            result.data.update(_cc_event)
                        result.data["customer_conditional_coupon_compose_active"] = True
                        return (_cc_text or "").strip()
                except Exception:
                    logger.exception(
                        "[RESPONDER] customer_conditional_coupon_answer compose failed",
                    )

            if _discovery_facts:
                from ..persona.general_offer_discovery_answer import (  # noqa: PLC0415
                    build_general_offer_discovery_facts_bundle,
                    build_general_offer_discovery_event_metadata,
                    general_offer_discovery_emergency_fallback,
                    try_compose_general_offer_discovery_answer,
                )
                from ..persona.fact_bound_composer import canonical_facts_hash  # noqa: PLC0415
                from ..persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                def _discovery_failure(**kwargs):  # type: ignore[no-untyped-def]
                    bundle = build_general_offer_discovery_facts_bundle(
                        inbound_text=kwargs["inbound_text"],
                        tenant_id=kwargs["tenant_id"],
                        customer_phone=kwargs["customer_phone"],
                        general_offer_discovery_facts=kwargs["facts"],
                        merchant_persona=dict(kwargs.get("ai_settings") or {}),
                    )
                    fb = PersonaComposeResult(
                        text=general_offer_discovery_emergency_fallback(bundle),
                        source="fallback_deterministic",
                        surface="general_offer_discovery_answer",
                        facts_hash=canonical_facts_hash(bundle.verified_facts),
                        guard_passed=True,
                        fallback_reason=str(kwargs.get("fallback_reason") or "compose_empty"),
                        language=bundle.language,
                    )
                    meta = build_general_offer_discovery_event_metadata(
                        fb,
                        tenant_id=int(kwargs["tenant_id"]),
                        compose_facts=kwargs["facts"],
                    )
                    meta["compose_source"] = "fallback_deterministic"
                    return fb.text.strip(), meta

                try:
                    _tc_ai_settings = _ai_settings_from_ctx(ctx)
                    _disc_text, _disc_result, _disc_event = (
                        await try_compose_general_offer_discovery_answer(
                            tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                            customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                            inbound_text=str(getattr(ctx, "message", "") or ""),
                            general_offer_discovery_facts=_discovery_facts,
                            ai_settings=_tc_ai_settings,
                        )
                    )
                    if _disc_result is not None and (_disc_text or "").strip():
                        if isinstance(_disc_event, dict):
                            result.data.update(_disc_event)
                        result.data["general_offer_discovery_compose_active"] = True
                        return (_disc_text or "").strip()
                    _fb_text, _fb_event = _discovery_failure(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        facts=_discovery_facts,
                        ai_settings=_tc_ai_settings,
                        fallback_reason="compose_empty",
                    )
                    result.data.update(_fb_event)
                    result.data["general_offer_discovery_compose_active"] = True
                    return _fb_text
                except Exception:
                    logger.exception("[RESPONDER] general_offer_discovery compose failed")
                    _fb_text, _fb_event = _discovery_failure(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        facts=_discovery_facts,
                        ai_settings=_ai_settings_from_ctx(ctx),
                        fallback_reason="compose_exception",
                    )
                    result.data.update(_fb_event)
                    result.data["general_offer_discovery_compose_active"] = True
                    return _fb_text

            if _product_sale_facts:
                from ..persona.product_sale_offer_answer import (  # noqa: PLC0415
                    build_product_sale_offer_event_metadata,
                    build_product_sale_offer_facts_bundle,
                    product_sale_offer_emergency_fallback,
                    try_compose_product_sale_offer_answer,
                )
                from ..persona.fact_bound_composer import canonical_facts_hash  # noqa: PLC0415
                from ..persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                def _product_sale_failure(**kwargs):  # type: ignore[no-untyped-def]
                    bundle = build_product_sale_offer_facts_bundle(
                        inbound_text=kwargs["inbound_text"],
                        tenant_id=kwargs["tenant_id"],
                        customer_phone=kwargs["customer_phone"],
                        product_sale_offer_facts=kwargs["facts"],
                        merchant_persona=dict(kwargs.get("ai_settings") or {}),
                    )
                    fb = PersonaComposeResult(
                        text=product_sale_offer_emergency_fallback(bundle),
                        source="fallback_deterministic",
                        surface="product_sale_offer_answer",
                        facts_hash=canonical_facts_hash(bundle.verified_facts),
                        guard_passed=True,
                        fallback_reason=str(kwargs.get("fallback_reason") or "compose_empty"),
                        language=bundle.language,
                    )
                    meta = build_product_sale_offer_event_metadata(
                        fb,
                        tenant_id=int(kwargs["tenant_id"]),
                        compose_facts=kwargs["facts"],
                    )
                    meta["compose_source"] = "fallback_deterministic"
                    return fb.text.strip(), meta

                try:
                    _tc_ai_settings = _ai_settings_from_ctx(ctx)
                    _ps_text, _ps_result, _ps_event = await try_compose_product_sale_offer_answer(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        product_sale_offer_facts=_product_sale_facts,
                        ai_settings=_tc_ai_settings,
                    )
                    if _ps_result is not None and (_ps_text or "").strip():
                        if isinstance(_ps_event, dict):
                            result.data.update(_ps_event)
                        result.data["product_sale_offer_compose_active"] = True
                        return (_ps_text or "").strip()
                    _fb_text, _fb_event = _product_sale_failure(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        facts=_product_sale_facts,
                        ai_settings=_tc_ai_settings,
                        fallback_reason="compose_empty",
                    )
                    result.data.update(_fb_event)
                    result.data["product_sale_offer_compose_active"] = True
                    return _fb_text
                except Exception:
                    logger.exception("[RESPONDER] product_sale_offer compose failed")
                    _fb_text, _fb_event = _product_sale_failure(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        facts=_product_sale_facts,
                        ai_settings=_ai_settings_from_ctx(ctx),
                        fallback_reason="compose_exception",
                    )
                    result.data.update(_fb_event)
                    result.data["product_sale_offer_compose_active"] = True
                    return _fb_text

            if _trusted_tc_facts:
                try:
                    from ..persona.trusted_coupon_offer_answer import (  # noqa: PLC0415
                        try_compose_trusted_coupon_offer_answer,
                        trusted_coupon_offer_compose_failure_response,
                    )
                    from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                    _tc_ai_settings = _ai_settings_from_ctx(ctx)
                    _tc_text, _tc_result, _tc_event = (
                        await try_compose_trusted_coupon_offer_answer(
                            tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                            customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                            inbound_text=str(getattr(ctx, "message", "") or ""),
                            trusted_coupon_offer_facts=_trusted_tc_facts,
                            ai_settings=_tc_ai_settings,
                        )
                    )
                    if _tc_result is not None and (_tc_text or "").strip():
                        if isinstance(_tc_event, dict):
                            result.data.update(_tc_event)
                        result.data["trusted_coupon_offer_compose_active"] = True
                        return (_tc_text or "").strip()

                    _fb_reason = "compose_empty"
                    _fb_text, _fb_event = trusted_coupon_offer_compose_failure_response(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        trusted_coupon_offer_facts=_trusted_tc_facts,
                        ai_settings=_tc_ai_settings,
                        fallback_reason=_fb_reason,
                        llm_candidate_present=bool((_tc_text or "").strip()),
                    )
                    result.data.update(_fb_event)
                    result.data["trusted_coupon_offer_compose_active"] = True
                    return _fb_text
                except Exception:
                    logger.exception(
                        "[RESPONDER] trusted_coupon_offer_answer compose failed",
                    )
                    from ..persona.trusted_coupon_offer_answer import (  # noqa: PLC0415
                        trusted_coupon_offer_compose_failure_response,
                    )
                    from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                    _fb_text, _fb_event = trusted_coupon_offer_compose_failure_response(
                        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                        inbound_text=str(getattr(ctx, "message", "") or ""),
                        trusted_coupon_offer_facts=_trusted_tc_facts,
                        ai_settings=_ai_settings_from_ctx(ctx),
                        fallback_reason="compose_exception",
                    )
                    result.data.update(_fb_event)
                    result.data["trusted_coupon_offer_compose_active"] = True
                    return _fb_text
            _sel_price = str((decision.args or {}).get("selection_presentation_text") or "").strip()
            if _sel_price and str((decision.args or {}).get("topic") or "") == "selection_context_price":
                result.data["chosen_path"] = "selection_context_price"
                return _sel_price
            _topic = str((decision.args or {}).get("topic") or "").strip()
            if _topic == "support_complaint_refund":
                from ..commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                    COMPLAINT_INTAKE_REPLY_AR,
                )

                result.data["chosen_path"] = "support_complaint_refund"
                return COMPLAINT_INTAKE_REPLY_AR
            if _topic.startswith("merchant_knowledge_"):
                result.data["chosen_path"] = _topic
                result.data["knowledge_kind"] = str(
                    (decision.args or {}).get("knowledge_kind") or ""
                )
                result.data["merchant_policy_status"] = str(
                    (decision.args or {}).get("merchant_policy_status") or "UNKNOWN"
                )
                if (decision.args or {}).get("doc_ref"):
                    result.data["doc_ref"] = str((decision.args or {}).get("doc_ref"))
                result.data["retrieval_count"] = int(
                    (decision.args or {}).get("retrieval_count") or 0
                )
                result.data["retrieval_attempted"] = bool(
                    (decision.args or {}).get("retrieval_attempted")
                )
                if (decision.args or {}).get("faq_visibility"):
                    result.data["faq_visibility"] = str(
                        (decision.args or {}).get("faq_visibility")
                    )
            if _topic == "product_knowledge_facts":
                from ..persona.kb_product_answer import try_compose_kb_product_answer  # noqa: PLC0415
                from ..persona.integration import _ai_settings_from_ctx  # noqa: PLC0415

                _kb_text, _kb_result, _kb_event = await try_compose_kb_product_answer(
                    tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                    customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                    inbound_text=str(getattr(ctx, "message", "") or ""),
                    decision_args=dict(decision.args or {}),
                    ai_settings=_ai_settings_from_ctx(ctx),
                )
                if _kb_result is not None and (_kb_text or "").strip():
                    if isinstance(_kb_event, dict):
                        result.data.update(_kb_event)
                    return (_kb_text or "").strip()
            from ..persona.integration import (  # noqa: PLC0415
                try_enforce_phatic_llm_persona_compose,
            )

            _phatic_persona = await try_enforce_phatic_llm_persona_compose(
                ctx,
                decision=decision,
                action_result=result,
            )
            if _phatic_persona and (_phatic_persona.text or "").strip():
                return self._apply_established_greeting_etiquette(
                    _phatic_persona.text,
                    ctx,
                    decision,
                )
            if _conditional_cc_facts and not result.data.get(
                "customer_conditional_coupon_compose_active"
            ):
                result.data["customer_conditional_coupon_general_llm_fallthrough"] = True
                result.data.setdefault(
                    "facts_snapshot_id",
                    str(_conditional_cc_facts.get("facts_snapshot_id") or ""),
                )
                result.data.setdefault(
                    "response_mode",
                    "customer_conditional_coupon_general_llm",
                )
            text = await self._llm_compose(ctx, result, decision=decision)
            if result.data.get("customer_conditional_coupon_general_llm_fallthrough"):
                from ..persona.customer_conditional_coupon_provenance import (  # noqa: PLC0415
                    stamp_customer_conditional_coupon_general_llm_compose_metadata,
                )

                stamp_customer_conditional_coupon_general_llm_compose_metadata(
                    result.data,
                    llm_candidate=text or "",
                )
            if _topic == "social_persona_ack":
                text = self._social_persona_emergency_fallback_if_needed(
                    text, ctx, result,
                )
            elif not (text or "").strip() and self._understood_social_religious_media(ctx):
                _cat = str(
                    (decision.args or {}).get("social_category")
                    or getattr(ctx, "non_commerce_category", "")
                    or "general_courtesy"
                )
                result.data["chosen_path"] = "social_persona_compose_from_empty_llm"
                text = await self._compose_social_persona_ack(
                    ctx, result, social_category=_cat,
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

    @staticmethod
    def _understood_social_religious_media(ctx: BrainContext) -> bool:
        from ..intent.non_commerce_classifier import (  # noqa: PLC0415
            inbound_has_classified_social_religious_media,
        )

        nc = str(getattr(ctx, "non_commerce_category", "") or "")
        if not nc and ctx.intent is not None:
            nc = str((ctx.intent.slots or {}).get("social_category") or "")
        return inbound_has_classified_social_religious_media(
            ctx.message or "",
            block_commerce=bool(getattr(ctx, "block_commerce_escalation", False)),
            nc_category=nc,
        )

    async def _compose_social_persona_ack(
        self,
        ctx: BrainContext,
        result: ActionResult,
        *,
        social_category: str,
    ) -> str:
        """LLM social persona compose for understood social/religious media."""
        from ..persona_expression import compose_social_persona_goal  # noqa: PLC0415

        category = (social_category or "general_courtesy").strip() or "general_courtesy"
        goal = compose_social_persona_goal(category)
        rs = ctx.reply_state
        orig_goal = None
        if rs is not None:
            orig_goal = rs.response_goal
            rs.response_goal = goal
        try:
            text = await self._llm_compose(ctx, result)
        finally:
            if rs is not None and orig_goal is not None:
                rs.response_goal = orig_goal
        text = self._social_persona_emergency_fallback_if_needed(text, ctx, result)
        return self._apply_gender_hint(text, ctx)

    def _apply_gender_hint(self, reply: str, ctx: BrainContext) -> str:
        """Persist gender evidence only — outbound fixes run post-compose."""
        if not reply:
            return reply
        try:
            from ...gender import detect_gender, persist_gender_hint  # noqa: PLC0415
            from ...gender.detector import GenderHint  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return reply

        try:
            state = ctx.state
            from core.customer_display import looks_like_phone_personalization_name  # noqa: PLC0415
            customer_name = ""
            if isinstance(ctx.profile, dict):
                raw_name = str(
                    ctx.profile.get("name")
                    or ctx.profile.get("customer_name")
                    or ""
                ).strip()
                if raw_name and not looks_like_phone_personalization_name(raw_name):
                    customer_name = raw_name

            prior_value = str(getattr(state, "customer_gender_hint", "") or "")
            prior = GenderHint(
                value=prior_value if prior_value in ("male", "female") else "unknown",
                confidence=float(getattr(state, "customer_gender_confidence", 0.0) or 0.0),
                source=str(getattr(state, "customer_gender_source", "") or "context"),
            )
            detected = detect_gender(
                message=ctx.message or "",
                customer_name=customer_name or None,
                prior_hint=prior if prior.value in ("male", "female") else None,
            )
            if detected.value in ("male", "female"):
                persist_gender_hint(state, hint=detected)
            return reply
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
        result: ActionResult | None = None,
    ) -> str:
        # Order-flow resume hint takes priority over generic suggestion
        # follow-ups: when the customer asks a side question ("كم
        # التوصيل؟") mid-order, we answer the FAQ AND remind them where
        # we left off so the conversation doesn't lose momentum.
        # Location/branch and store-link turns must never carry checkout
        # resume hints — the wire layer owns the store URL CTA.
        from ..commerce.store_inquiry_compose_guard import (  # noqa: PLC0415
            should_skip_order_resume_hint,
        )

        _intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
        if not should_skip_order_resume_hint(topic=topic, intent_name=_intent_name):
            resume_meta = self._order_resume_hint_metadata(ctx)
            if resume_meta and result is not None:
                data = getattr(result, "data", None)
                if isinstance(data, dict):
                    data["order_resume_hint"] = resume_meta
                    layers = list(data.get("_compose_metadata_layers") or [])
                    if "order_resume_hint" not in layers:
                        layers.append("order_resume_hint")
                    data["_compose_metadata_layers"] = layers
                try:
                    from core.outbound_text_policy import mark_compose_metadata  # noqa: PLC0415

                    mark_compose_metadata(result, layer="order_resume_hint")
                except Exception:  # noqa: BLE001  # noqa: silent-ok — policy tag must not block compose
                    pass

        suggestion = getattr(ctx, "suggestion", None)
        if not suggestion or not suggestion.needs_follow_up_question:
            return text

        follow_up = (suggestion.follow_up_question or "").strip()
        if not follow_up or follow_up in text:
            return text

        return f"{text}\n\n{follow_up}"

    @staticmethod
    def _order_resume_hint_metadata(ctx: BrainContext) -> Dict[str, Any]:
        """Return order-resume facts for compose — never customer-facing prose."""
        try:
            prep = getattr(ctx.state, "order_prep", None)
            focus = getattr(ctx.state, "current_product_focus", None) or {}
            if not prep or not (focus or getattr(prep, "product_id", "")):
                return {}

            product_title = (focus or {}).get("title") or getattr(prep, "product_name", "") or "المنتج"
            base: Dict[str, Any] = {
                "active_order_context": True,
                "resume_candidate": product_title,
                "resume_hint_source": "order_prep",
            }

            pending_options: List[str] = []
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
                base["pending_slot"] = "product_options"
                base["pending_options"] = pending_options
                return base

            missing = list(getattr(prep, "missing_fields", None) or [])
            slot_map = {
                "customer_first_name": "customer_name",
                "customer_last_name":  "customer_name",
                "customer_name":       "customer_name",
                "city":                "city",
                "address":             "delivery_address",
                "address_line":        "delivery_address",
                "short_address_code":  "delivery_address",
                "google_maps_url":     "delivery_address",
                "delivery_address":    "delivery_address",
            }
            for slot in missing:
                canonical = slot_map.get(slot)
                if canonical:
                    base["pending_slot"] = canonical
                    return base

            base["pending_slot"] = "order_confirmation"
            return base
        except Exception:
            return {}

    @staticmethod
    def _order_resume_hint(ctx: BrainContext) -> str:
        """Deprecated — Phase 2 P0: metadata only via ``_order_resume_hint_metadata``."""
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

    async def _compose_track_order_need_identifiers(
        self,
        ctx: BrainContext,
        result: ActionResult,
    ) -> str:
        from core.outbound_text_policy import mark_compose_llm  # noqa: PLC0415

        from .track_order_need_identifiers_compose import (  # noqa: PLC0415
            build_track_order_need_identifiers_compose_decision,
            extract_track_order_need_identifiers_facts,
            format_track_order_need_identifiers_facts_overlay,
            is_usable_llm_reply,
            record_fallback_metadata,
            record_llm_compose_metadata,
            unusable_llm_reply_reason,
        )

        facts = extract_track_order_need_identifiers_facts(ctx, result)
        result.data["track_order_need_identifiers"] = dict(facts)
        result.data["compose_facts_overlay"] = (
            format_track_order_need_identifiers_facts_overlay(facts)
        )
        compose_decision = build_track_order_need_identifiers_compose_decision(facts)
        mark_compose_llm(result)
        try:
            reply = await self._llm_compose(ctx, result, decision=compose_decision)
        except Exception:  # noqa: BLE001
            reply = ""
        if is_usable_llm_reply(reply):
            record_llm_compose_metadata(result, llm_candidate=str(reply or ""))
            result.data.pop("compose_facts_overlay", None)
            return str(reply)
        record_fallback_metadata(
            result,
            reason=unusable_llm_reply_reason(reply),
            llm_candidate_present=bool(str(reply or "").strip()),
        )
        result.data.pop("compose_facts_overlay", None)
        return T.track_order_need_identifiers_emergency_fallback()

    async def _compose_track_order_not_found(
        self,
        ctx: BrainContext,
        result: ActionResult,
    ) -> str:
        from core.outbound_text_policy import mark_compose_llm  # noqa: PLC0415

        from .track_order_not_found_compose import (  # noqa: PLC0415
            build_track_order_not_found_compose_decision,
            extract_track_order_not_found_facts,
            format_track_order_not_found_facts_overlay,
            is_usable_llm_reply,
            record_fallback_metadata,
            record_llm_compose_metadata,
        )

        facts = extract_track_order_not_found_facts(ctx, result)
        result.data["track_order_lookup"] = dict(facts)
        result.data["compose_facts_overlay"] = format_track_order_not_found_facts_overlay(
            facts
        )
        compose_decision = build_track_order_not_found_compose_decision(facts)
        mark_compose_llm(result)
        try:
            reply = await self._llm_compose(ctx, result, decision=compose_decision)
        except Exception:  # noqa: BLE001
            reply = ""
        if is_usable_llm_reply(reply):
            record_llm_compose_metadata(result, llm_candidate=str(reply or ""))
            result.data.pop("compose_facts_overlay", None)
            return str(reply)
        record_fallback_metadata(result, reason="compose_failed_or_empty")
        result.data.pop("compose_facts_overlay", None)
        return T.order_status_not_found()

    async def _llm_compose(
        self,
        ctx: BrainContext,
        result: ActionResult,
        *,
        decision: Decision | None = None,
    ) -> str:
        """Use the thin MerchantBrain LLM path, with legacy fallback on hard errors.

        The preferred path injects a short prompt + explicit BrainReplyState.
        We keep the legacy orchestrator call only as an emergency fallback when
        the new path fails unexpectedly, not as the default path.
        """
        import asyncio  # noqa: PLC0415
        import time as _time_dc  # noqa: PLC0415

        _TIMEOUT = 25  # seconds
        reply_state = None
        _compose_role = "default_compose"
        _compose_span = "default_compose"
        _t_llm_compose = None
        _llm_compose_recorded = False
        try:
            from core.turn_latency import (  # noqa: PLC0415
                get_compose_role,
                safe_record_llm_call,
                safe_record_ms,
            )

            _compose_role = get_compose_role() or "default_compose"
            _compose_span = _compose_role
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            get_compose_role = None  # type: ignore[assignment,misc]
            safe_record_llm_call = None  # type: ignore[assignment,misc]
            safe_record_ms = None  # type: ignore[assignment,misc]

        def _record_default_compose_llm(
            *,
            duration_ms: int,
            model: str = "",
            provider: str = "",
            fallback_reason: str = "",
            input_tokens: Any = None,
            output_tokens: Any = None,
            cached_tokens: Any = None,
            retry_count: int = 0,
        ) -> None:
            nonlocal _llm_compose_recorded
            if _llm_compose_recorded or safe_record_ms is None or safe_record_llm_call is None:
                return
            _llm_compose_recorded = True
            try:
                safe_record_ms(_compose_span, duration_ms)
                safe_record_llm_call(
                    purpose=_compose_role,
                    llm_call_role=_compose_role,
                    model=model,
                    provider=provider,
                    duration_ms=duration_ms,
                    timeout_seconds=float(_TIMEOUT),
                    fallback_reason=fallback_reason,
                    ttft_available=False,
                    retry_count=retry_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass
        try:
            from core.outbound_text_policy import mark_compose_llm  # noqa: PLC0415

            mark_compose_llm(result)
        except Exception:  # noqa: BLE001
            pass

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
            _owner_brief = None
            try:
                from modules.ai.brain.turn.compose_bridge import (  # noqa: PLC0415
                    resolve_owner_brief_dict,
                )

                _owner_brief = resolve_owner_brief_dict(
                    decision if decision is not None else Decision(action="", args={}),
                    ctx,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — brief resolve must not break compose
                _dec_args = (decision.args if decision is not None else None) or {}
                _owner_brief = _dec_args.get("owner_brief")

            if isinstance(_owner_brief, dict) and _owner_brief:
                try:
                    from modules.ai.brain.turn.owner_brief import (  # noqa: PLC0415
                        format_owner_brief_for_compose,
                    )

                    _brief_overlay = format_owner_brief_for_compose(_owner_brief)
                    if _brief_overlay:
                        prompt = f"{prompt}\n\n{_brief_overlay}"
                except Exception:  # noqa: BLE001  # noqa: silent-ok — brief overlay must not break compose
                    pass

            _dec_args = (decision.args if decision is not None else None) or {}
            _amount_facts = _dec_args.get("current_order_amount_facts")
            if isinstance(_amount_facts, dict) and _amount_facts:
                import json as _json  # noqa: PLC0415

                prompt = (
                    f"{prompt}\n\n[CURRENT_ORDER_AMOUNT_FACTS — operational only]\n"
                    f"{_json.dumps(_amount_facts, ensure_ascii=False)}"
                )
            _facts_overlay = str(
                (getattr(result, "data", None) or {}).get("compose_facts_overlay") or ""
            ).strip()
            if _facts_overlay:
                prompt = f"{prompt}\n\n{_facts_overlay}"
            try:
                from modules.ai.brain.commerce.catalog_order_facts import (  # noqa: PLC0415
                    build_catalog_order_compose_facts,
                )

                _profile = dict(getattr(ctx, "profile", None) or {})
                _cat_facts = build_catalog_order_compose_facts(
                    state=getattr(ctx, "state", None),
                    inbound_metadata=dict(_profile.get("inbound_metadata") or {}),
                )
                if _cat_facts:
                    import json as _json  # noqa: PLC0415

                    prompt = (
                        f"{prompt}\n\n[CATALOG_ORDER_FACTS — operational only]\n"
                        f"{_json.dumps(_cat_facts, ensure_ascii=False)}"
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog facts must not break compose
                pass
            _checkout_facts = dict(
                (getattr(reply_state, "known_facts", None) or {}).get(
                    "checkout_identity_shipping",
                )
                or {}
            )
            if _checkout_facts:
                import json as _json  # noqa: PLC0415

                prompt = (
                    f"{prompt}\n\n[CHECKOUT_IDENTITY_SHIPPING_FACTS — operational only]\n"
                    f"{_json.dumps(_checkout_facts, ensure_ascii=False)}"
                )
            locale = str(ctx.profile.get("preferred_language") or "ar")
            history_messages = _as_ai_history(
                ctx.history,
                ctx.message,
                fresh_social_context=bool(getattr(ctx, "fresh_social_context", False)),
            )

            try:
                from modules.ai.brain.observability.memory_selection_evidence import (  # noqa: PLC0415
                    emit_compose_memory_evidence,
                )

                _rs = reply_state
                _primary_goal = str(getattr(_rs, "primary_customer_goal", "") or "")
                _profile = dict(getattr(ctx, "profile", None) or {})
                emit_compose_memory_evidence(
                    tenant_id=ctx.tenant_id,
                    phone=ctx.customer_phone or "",
                    state=ctx.state,
                    history=ctx.history,
                    conversation_summary=str(getattr(_rs, "conversation_summary", "") or ""),
                    inbound_text=ctx.message or "",
                    intent_name=str(getattr(ctx.intent, "name", "") or ""),
                    primary_customer_goal=_primary_goal,
                    inbound_metadata=dict(_profile.get("inbound_metadata") or {}),
                    human_priority=bool(getattr(ctx, "human_priority", False)),
                    history_messages_count=len(history_messages),
                    fresh_social_context=bool(getattr(ctx, "fresh_social_context", False)),
                    fresh_social_reason=str(getattr(ctx, "fresh_social_context_reason", "") or ""),
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break compose
                pass

            from modules.ai.brain.cost.intent_cost_policy import (  # noqa: PLC0415
                emit_llm_avoidable_call,
                should_avoid_llm_for_intent,
                should_avoid_llm_for_social_category,
            )
            from modules.ai.orchestrator.llm_cost_audit import (  # noqa: PLC0415
                approx_tokens_from_chars,
                build_brain_compose_audit_extra,
            )

            _intent_name = str(
                getattr(ctx.intent, "name", "")
                or getattr(reply_state, "intent_name", "")
                or ""
            )
            _dec_args = (decision.args if decision is not None else None) or {}
            _social_cat = str(
                _dec_args.get("social_category")
                or getattr(reply_state, "social_category", "")
                or ""
            )
            _avoid = should_avoid_llm_for_intent(_intent_name) or (
                _social_cat and should_avoid_llm_for_social_category(_social_cat)
            )
            if _avoid:
                emit_llm_avoidable_call(
                    tenant_id=ctx.tenant_id,
                    conversation_id=ctx.conversation_id,
                    turn_id=getattr(ctx.state, "turn", None),
                    intent=_intent_name or None,
                    action=(decision.action if decision is not None else ACTION_LLM_REPLY),
                    reason=(decision.reason if decision is not None else None),
                    estimated_input_tokens=approx_tokens_from_chars(len(prompt)),
                    system_chars=len(prompt),
                )

            _llm_audit = build_brain_compose_audit_extra(
                reply_state=reply_state,
                prompt=prompt,
                history_messages=history_messages,
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                turn_id=getattr(ctx.state, "turn", None),
            )

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

            from ..cost.model_router import resolve_compose_model_route  # noqa: PLC0415
            from ..cost.model_router_audit import maybe_audit_model_router  # noqa: PLC0415
            from ..cost.model_router_decision import log_model_router_decision  # noqa: PLC0415
            from ..compose.prompt_state_serializer import (  # noqa: PLC0415
                explain_commerce_prompt_slim_gate,
            )

            _compose_route = resolve_compose_model_route(
                intent_name=_intent_name,
                social_category=_social_cat,
                decision_action=(decision.action if decision is not None else None),
                human_priority=bool(getattr(ctx, "human_priority", False)),
                reply_state=reply_state,
                result_data=dict(getattr(result, "data", None) or {}),
            )
            _llm_audit.update(_compose_route.to_audit_dict())
            if _compose_route.enforced and _compose_route.model:
                _llm_audit["model_override"] = _compose_route.model

            maybe_audit_model_router(
                call_site="brain.compose._llm_compose",
                intent_name=getattr(getattr(ctx, "intent", None), "name", None),
                social_category=(getattr(getattr(ctx, "intent", None), "slots", None) or {}).get(
                    "social_category",
                ),
                tenant_id=ctx.tenant_id,
                conversation_id=getattr(ctx, "conversation_id", None),
                turn_id=getattr(getattr(ctx, "state", None), "turn", None),
                extra={
                    "mode": "enforce" if _compose_route.enforced else "audit_only",
                    **_compose_route.to_audit_dict(),
                },
            )

            _prompt_overrides: dict = {
                "__full_system_prompt": prompt,
                "__llm_cost_audit": _llm_audit,
            }
            if _compose_route.enforced:
                _prompt_overrides["__model_router"] = _compose_route.to_prompt_override()

            _provider_hint = (
                _compose_route.provider_hint
                if _compose_route.enforced
                else "openai_compatible"
            )

            _slim_applied, _slim_reason, _slim_meta = explain_commerce_prompt_slim_gate(
                reply_state,
            )
            log_model_router_decision(
                tenant_id=ctx.tenant_id,
                intent=_intent_name,
                selected_tier=_compose_route.tier,
                selected_provider=_compose_route.provider,
                selected_model=_compose_route.model,
                provider_hint=_provider_hint,
                fallback_used=False,
                reason_code=_compose_route.reason,
                commerce_slim_applied=_slim_applied,
                prompt_chars=len(prompt),
                state_topic_shift=bool(_slim_meta.get("state_topic_shift")),
                checkout_relevant=bool(_slim_meta.get("checkout_relevant")),
                extra={
                    "slim_gate_reason": _slim_reason,
                    "router_enforced": _compose_route.enforced,
                },
            )

            try:
                from dataclasses import asdict as _asdict  # noqa: PLC0415

                from modules.ai.brain.cost.model_router_audit import (  # noqa: PLC0415
                    is_premium_model_allowed,
                )
                from modules.ai.brain.truth_surface.model_payload_attestation import (  # noqa: PLC0415
                    build_model_payload_attestation,
                )
                from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
                    current_trusted_context,
                )
                from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: PLC0415
                    serialize_commerce_brain_state,
                )

                _slim_state = None
                if _slim_applied:
                    _slim_state = serialize_commerce_brain_state(
                        _asdict(reply_state),
                        reply_state,
                        kb_in_prompt_block=False,
                    )
                _compose_attestation = build_model_payload_attestation(
                    stage="compose",
                    snapshot=current_trusted_context(),
                    brain_projection=getattr(ctx, "trusted_context_projection", None),
                    known_facts=dict(getattr(reply_state, "known_facts", None) or {}),
                    selected_product=getattr(reply_state, "selected_product", None),
                    history=ctx.history,
                    recent_turns=getattr(reply_state, "recent_turns", None),
                    decision_action=str(
                        (decision.action if decision is not None else "") or ""
                    ),
                    result_data=dict(getattr(result, "data", None) or {}),
                    compose_route=_compose_route,
                    premium_allowed=is_premium_model_allowed(),
                    slim_compose_state=_slim_state,
                )
                ctx.model_payload_attestation = _compose_attestation
                reply_state.model_payload_attestation = _compose_attestation
                logger.info(
                    "[MODEL_PAYLOAD_ATTESTATION] %s",
                    _compose_attestation,
                )
            except Exception as _mpa_compose_exc:  # noqa: BLE001  # noqa: silent-ok
                logger.debug(
                    "[MODEL_PAYLOAD_ATTESTATION] compose stage failed tenant=%s err=%s",
                    getattr(ctx, "tenant_id", None),
                    _mpa_compose_exc,
                )

            _t_llm_compose = _time_dc.monotonic()
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
                    prompt_overrides=_prompt_overrides,
                    provider_hint=_provider_hint,
                ),
                timeout=_TIMEOUT,
            )
            _payload_meta = getattr(payload, "metadata", None) or {}
            _usage = (
                _payload_meta.get("usage")
                if isinstance(_payload_meta.get("usage"), dict)
                else {}
            )
            _record_default_compose_llm(
                duration_ms=int(
                    (_time_dc.monotonic() - (_t_llm_compose or _time_dc.monotonic()))
                    * 1000.0
                ),
                model=str(_payload_meta.get("model") or ""),
                provider=str(getattr(payload, "provider_used", "") or ""),
                input_tokens=(
                    _usage.get("input_tokens") or _usage.get("prompt_tokens")
                ),
                output_tokens=(
                    _usage.get("output_tokens") or _usage.get("completion_tokens")
                ),
                cached_tokens=(
                    _usage.get("cached_tokens") or _usage.get("cache_read_tokens")
                ),
            )

            reply_text = (payload.reply_text or "").strip()
            if reply_text:
                from ..cost.model_router import should_block_anthropic_compose_result  # noqa: PLC0415

                if should_block_anthropic_compose_result(
                    route=_compose_route,
                    provider_used=str(payload.provider_used or ""),
                ):
                    log_model_router_decision(
                        tenant_id=ctx.tenant_id,
                        intent=_intent_name,
                        selected_tier=_compose_route.tier,
                        selected_provider=str(payload.provider_used or ""),
                        selected_model=str(
                            (payload.metadata or {}).get("model") or ""
                        ),
                        provider_hint=_provider_hint,
                        fallback_used=True,
                        reason_code="anthropic_blocked_routine_commerce",
                        commerce_slim_applied=_slim_applied,
                        prompt_chars=len(prompt),
                        state_topic_shift=bool(_slim_meta.get("state_topic_shift")),
                        checkout_relevant=bool(_slim_meta.get("checkout_relevant")),
                        extra={
                            "fallback_blocked_for_routine_commerce": True,
                            "legacy_path_used": False,
                        },
                    )
                    logger.warning(
                        "[Composer._llm_compose] anthropic reply blocked for "
                        "routine commerce | tenant=%s intent=%s",
                        ctx.tenant_id,
                        _intent_name,
                    )
                    return await self._thin_compose_retry(
                        ctx,
                        result,
                        reply_state=reply_state,
                        timeout_seconds=15,
                    )

                result.data["chosen_path"] = "llm"
                result.data["llm_provider"] = payload.provider_used
                result.data["model_used"] = payload.metadata.get("model", payload.provider_used)
                result.data["prompt_mode"] = "merchant_brain_thin"
                _chain_fallback = bool(
                    (payload.metadata or {}).get("provider_chain_fallback_used")
                )
                if _chain_fallback:
                    log_model_router_decision(
                        tenant_id=ctx.tenant_id,
                        intent=_intent_name,
                        selected_tier=_compose_route.tier,
                        selected_provider=str(payload.provider_used or ""),
                        selected_model=str(
                            (payload.metadata or {}).get("model") or ""
                        ),
                        provider_hint=_provider_hint,
                        fallback_used=True,
                        reason_code="provider_chain_fallback",
                        commerce_slim_applied=_slim_applied,
                        prompt_chars=len(prompt),
                        state_topic_shift=bool(_slim_meta.get("state_topic_shift")),
                        checkout_relevant=bool(_slim_meta.get("checkout_relevant")),
                    )
                from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
                    stamp_general_llm_compose_metadata,
                )

                stamp_general_llm_compose_metadata(
                    result.data,
                    llm_candidate=reply_text,
                    chosen_path=str(result.data.get("chosen_path") or "llm"),
                )
                return reply_text

            logger.warning(
                "[Composer._llm_compose] thin path returned empty reply | tenant=%s",
                ctx.tenant_id,
            )
            return await self._legacy_llm_compose(
                ctx, result, timeout_seconds=15, reply_state=reply_state,
            )
        except asyncio.TimeoutError:
            if _t_llm_compose is not None:
                _record_default_compose_llm(
                    duration_ms=int((_time_dc.monotonic() - _t_llm_compose) * 1000.0),
                    fallback_reason="timeout",
                )
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
            if _t_llm_compose is not None:
                _record_default_compose_llm(
                    duration_ms=int((_time_dc.monotonic() - _t_llm_compose) * 1000.0),
                    fallback_reason="error",
                )
            logger.error("[Composer._llm_compose] thin path error: %s", exc)
            return await self._legacy_llm_compose(
                ctx,
                result,
                timeout_seconds=15,
                reply_state=reply_state,
            )

    async def _thin_compose_retry(
        self,
        ctx: BrainContext,
        result: ActionResult,
        *,
        reply_state: Any = None,
        timeout_seconds: int = 15,
    ) -> str:
        """Second-chance thin compose — never uses the full legacy orchestrator."""
        import asyncio  # noqa: PLC0415

        from modules.ai.orchestrator.adapter import generate_ai_reply  # noqa: PLC0415
        from modules.ai.orchestrator.llm_cost_audit import build_brain_compose_audit_extra  # noqa: PLC0415
        from ..cost.model_router import resolve_compose_model_route  # noqa: PLC0415
        from ..cost.model_router_decision import log_model_router_decision  # noqa: PLC0415
        from ..compose.prompt_state_serializer import explain_commerce_prompt_slim_gate  # noqa: PLC0415

        rs = reply_state or ctx.reply_state or self._minimal_reply_state(ctx)
        prompt = build_brain_reply_prompt(rs)
        locale = str(ctx.profile.get("preferred_language") or "ar")
        history_messages = _as_ai_history(ctx.history, ctx.message)
        intent_name = str(
            getattr(ctx.intent, "name", "") or getattr(rs, "intent_name", "") or ""
        )
        route = resolve_compose_model_route(
            intent_name=intent_name,
            reply_state=rs,
            result_data=dict(getattr(result, "data", None) or {}),
        )
        audit = build_brain_compose_audit_extra(
            reply_state=rs,
            prompt=prompt,
            history_messages=history_messages,
            tenant_id=ctx.tenant_id,
            conversation_id=ctx.conversation_id,
            turn_id=getattr(ctx.state, "turn", None),
            source="brain.compose._thin_compose_retry",
        )
        audit.update(route.to_audit_dict())
        if route.enforced and route.model:
            audit["model_override"] = route.model
        overrides: dict = {"__full_system_prompt": prompt, "__llm_cost_audit": audit}
        if route.enforced:
            overrides["__model_router"] = route.to_prompt_override()
        provider_hint = route.provider_hint if route.enforced else "openai_compatible"
        slim_applied, slim_reason, slim_meta = explain_commerce_prompt_slim_gate(rs)
        log_model_router_decision(
            tenant_id=ctx.tenant_id,
            intent=intent_name,
            selected_tier=route.tier,
            selected_provider=route.provider,
            selected_model=route.model,
            provider_hint=provider_hint,
            fallback_used=True,
            reason_code="thin_compose_retry",
            commerce_slim_applied=slim_applied,
            prompt_chars=len(prompt),
            state_topic_shift=bool(slim_meta.get("state_topic_shift")),
            checkout_relevant=bool(slim_meta.get("checkout_relevant")),
            extra={"slim_gate_reason": slim_reason},
        )
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
                context_metadata={"brain_state": asdict(rs)},
                prompt_overrides=overrides,
                provider_hint=provider_hint,
            ),
            timeout=timeout_seconds,
        )
        reply_text = (payload.reply_text or "").strip()
        if reply_text:
            from ..cost.model_router import should_block_anthropic_compose_result  # noqa: PLC0415

            if should_block_anthropic_compose_result(
                route=route,
                provider_used=str(payload.provider_used or ""),
            ):
                log_model_router_decision(
                    tenant_id=ctx.tenant_id,
                    intent=intent_name,
                    selected_tier=route.tier,
                    selected_provider=str(payload.provider_used or ""),
                    selected_model=str((payload.metadata or {}).get("model") or ""),
                    provider_hint=provider_hint,
                    fallback_used=True,
                    reason_code="anthropic_blocked_routine_commerce",
                    commerce_slim_applied=slim_applied,
                    prompt_chars=len(prompt),
                    state_topic_shift=bool(slim_meta.get("state_topic_shift")),
                    checkout_relevant=bool(slim_meta.get("checkout_relevant")),
                    extra={
                        "fallback_blocked_for_routine_commerce": True,
                        "thin_retry_used": True,
                        "legacy_path_used": False,
                    },
                )
                result.data["chosen_path"] = "llm_fallback_failed"
                return T.generic_fallback(variant=self._variant_idx(ctx))

            result.data["chosen_path"] = "llm_thin_retry"
            result.data["llm_provider"] = payload.provider_used
            result.data["model_used"] = payload.metadata.get("model", payload.provider_used)
            result.data["prompt_mode"] = "merchant_brain_thin_retry"
            from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
                stamp_general_llm_compose_metadata,
            )

            stamp_general_llm_compose_metadata(
                result.data,
                llm_candidate=reply_text,
                chosen_path="llm_thin_retry",
            )
            return reply_text
        result.data["chosen_path"] = "llm_fallback_failed"
        return T.generic_fallback(variant=self._variant_idx(ctx))

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
        *,
        reply_state: Any = None,
    ) -> str:
        """Emergency fallback — prefer thin MerchantBrain path when router is enabled."""
        import asyncio  # noqa: PLC0415

        from ..cost.model_router import is_model_router_enabled  # noqa: PLC0415

        if is_model_router_enabled():
            logger.warning(
                "[Composer._legacy_llm_compose] router enabled — "
                "retrying thin compose (no full orchestrator) | tenant=%s",
                ctx.tenant_id,
            )
            try:
                return await self._thin_compose_retry(
                    ctx,
                    result,
                    reply_state=reply_state,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                logger.error(
                    "[Composer._legacy_llm_compose] thin retry failed: %s", exc,
                )

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


def _as_ai_history(
    history: List[Dict[str, Any]],
    current_message: str,
    *,
    fresh_social_context: bool = False,
) -> List[Dict[str, str]]:
    if fresh_social_context:
        msg = str(current_message or "").strip()
        if msg:
            return [{"role": "user", "content": msg}]
        return []

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
