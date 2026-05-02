"""
brain/decision/engine.py
─────────────────────────
DefaultDecisionEngine: rule-based Commerce Decision Engine.

Decides *what action to take* given the full BrainContext (intent, state,
commerce facts). The decision is deterministic — no LLM involved here.

Rule priority (first match wins):
  1. Human handoff request → ACTION_HANDOFF
  2. Resend payment link (customer in checkout stage) → ACTION_SEND_PAYMENT_LINK
  3. Track order → ACTION_TRACK_ORDER
  4. Simple FAQ (identity / shipping / store / contact) → ACTION_FAQ_REPLY
  5. Greeting / first-turn general help → ACTION_GREET
  6. Buy / start order → ACTION_PROPOSE_DRAFT_ORDER (if product in focus)
  7. Buy / start order → ACTION_SEARCH_PRODUCTS (no product selected)
  8. Ask about product or price → ACTION_SEARCH_PRODUCTS
  9. Hesitation with product in focus and coupons available → ACTION_SUGGEST_COUPON
 10. Fallback → ACTION_LLM_REPLY
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ..types import BrainContext, Decision
from .actions import (
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
from ..types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_GREETING,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_HESITATION,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_GENERAL,
    INTENT_WHO_ARE_YOU,
    INTENT_PICK_LIST_ITEM,
)
from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING

logger = logging.getLogger("nahla.brain.decision")


class DefaultDecisionEngine:
    """Implements DecisionMaker protocol."""

    def decide(self, ctx: BrainContext) -> Decision:
        intent = ctx.intent
        state  = ctx.state
        facts  = ctx.facts
        checkout_slots = {
            "customer_first_name",
            "customer_last_name",
            "customer_name",
            "full_name",
            "city",
            "short_address_code",
            "google_maps_url",
            "location_url",
            "address",
            "address_line",
            "street",
            "district",
            "postal_code",
            "zip_code",
            "building_number",
            "additional_number",
            "latitude",
            "longitude",
        }

        # ── 0. Deterministic checkout continuation (highest priority) ────────
        # When the customer is actively in the ordering stage and sends a
        # confirmation / continuation message, NEVER let it fall through to
        # the LLM.  This block fires before anything else so that explicit
        # checkout signals are never misrouted.
        #
        # Trigger conditions (ALL must be true):
        #   • stage is ordering or deciding
        #   • a product is already in focus (current_product_focus)
        #   • the store can actually fulfil orders (facts.orderable)
        #   • the message looks like a checkout continuation (keyword list
        #     OR any message while order_prep exists — customer is answering
        #     our slot-fill questions)
        _CONFIRM_KEYWORDS = frozenset({
            # Arabic: "confirm", "place order", "done", "continue", "go ahead",
            # "yes", "agreed", "I agree", "OK", "sure", "proceed",
            "تمم", "تمام", "اطلب", "اطلبه", "اطلبها", "تأكيد", "تأكد",
            "اكمل", "أكمل", "نعم", "موافق", "موافقه", "حسنا", "حسناً",
            "حسن", "صح", "صحيح", "شوف", "ابدأ", "إبدأ", "سر", "سري",
            "قدّم", "قدم", "ارسل", "أرسل", "تقدم", "تقدّم", "أتمم",
            "وافق", "أوافق", "أوافقك", "رائع", "ممتاز", "انشئ", "أنشئ",
            "go", "ok", "okay", "yes", "confirm", "proceed", "sure",
        })
        _msg_lower  = (ctx.message or "").strip().lower()
        _msg_words  = set(_msg_lower.split())
        _is_confirm = bool(_msg_words & _CONFIRM_KEYWORDS)
        _has_prep   = bool(getattr(state, "order_prep", None))

        if (
            state.stage in (STAGE_ORDERING, STAGE_DECIDING)
            and state.current_product_focus
            and facts.orderable
            and not state.checkout_url
            and (_is_confirm or _has_prep)
            and intent.name not in (INTENT_TALK_HUMAN, INTENT_TRACK_ORDER)
        ):
            _focus_title = (state.current_product_focus or {}).get("title")
            logger.info(
                "[ORDER FLOW] FORCED action=propose_draft_order "
                "reason=rule_based_checkout | tenant=%s product=%r "
                "is_confirm=%s has_prep=%s intent=%s stage=%s",
                ctx.tenant_id, _focus_title,
                _is_confirm, _has_prep, intent.name, state.stage,
            )
            return Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={"product": state.current_product_focus},
                reason=(
                    "rule_based_checkout: confirmation keyword detected"
                    if _is_confirm
                    else "rule_based_checkout: order_prep active — continue collecting slots"
                ),
                confidence=0.97,
            )

        # ── 1. Handoff ────────────────────────────────────────────────────
        # Guard: never escalate to a human if the customer is mid-order.
        # The classifier already blocks LLM-suggested INTENT_TALK_HUMAN
        # while order flow is active; this is a second layer of defence
        # in case the rules emit it (or a different path constructs the
        # intent directly). Same guard as in the webhook's order-flow
        # recovery override — kept symmetrical on purpose.
        if intent.name == INTENT_TALK_HUMAN:
            try:
                from modules.ai.routing.conversation_mode import (  # noqa: PLC0415
                    message_has_order_recovery_signal,
                )
            except Exception:
                message_has_order_recovery_signal = lambda _t: False  # type: ignore

            _has_active_order = bool(
                getattr(state, "order_prep", None)
                or getattr(state, "current_product_focus", None)
            )
            _msg = getattr(ctx, "message", "") or ""
            if _has_active_order or message_has_order_recovery_signal(_msg):
                logger.info(
                    "[ORDER FLOW] continuing order despite previous failure | "
                    "blocking ACTION_HANDOFF — intent=%s active_order=%s",
                    intent.name, _has_active_order,
                )
                # Fall through to the regular order/checkout decision logic.
            else:
                return Decision(
                    action=ACTION_HANDOFF,
                    reason="customer requested human agent",
                )

        # ── 2. Resend payment link / retry order ──────────────────────────
        if intent.name == INTENT_PAY_NOW or (
            state.stage == STAGE_CHECKOUT and intent.name in (INTENT_PAY_NOW, INTENT_START_ORDER)
        ):
            if state.checkout_url:
                return Decision(
                    action=ACTION_SEND_PAYMENT_LINK,
                    args={"checkout_url": state.checkout_url},
                    reason="customer in checkout stage — resend payment link",
                )
            # No checkout_url yet but we are in ordering/checkout.
            # If we have a product in focus and the order_prep is complete
            # (the customer already provided name/city/address), try to
            # create the order now instead of falling through to LLM.
            if (
                state.current_product_focus
                and state.stage in (STAGE_ORDERING, STAGE_DECIDING, STAGE_CHECKOUT)
                and facts.orderable
            ):
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": state.current_product_focus},
                    reason="pay_now with product focus + no checkout_url → retry order creation",
                    confidence=0.92,
                )

        # ── 3. Track order ────────────────────────────────────────────────
        if intent.name == INTENT_TRACK_ORDER:
            return Decision(
                action=ACTION_TRACK_ORDER,
                args={"order_id": intent.slots.get("order_id", "")},
                reason="customer asked for order status",
            )

        # ── 3.4 Product name match from last search candidates ────────────────
        _in_data_collection = (
            state.stage == STAGE_ORDERING
            and bool(getattr(state.order_prep, "missing_fields", None))
        )
        # Sort candidates by affinity_score (desc) so the most-known product
        # wins ties when more than one title matches the message.
        _raw_candidates = list(state.last_search_candidates or []) or list(state.last_recommended_products or [])
        _candidates = sorted(
            _raw_candidates,
            key=lambda p: float(p.get("affinity_score") or 0.0),
            reverse=True,
        )
        if _candidates and not _in_data_collection and intent.name not in (
            INTENT_TALK_HUMAN, INTENT_ASK_SHIPPING, INTENT_ASK_STORE_INFO,
            INTENT_ASK_OWNER_CONTACT,
        ):
            _matched_product = _match_product_from_message(ctx.message, _candidates)
            if _matched_product:
                # Use can_checkout as the single source of truth; fall back to
                # orderable for older state entries that pre-date can_checkout.
                _prod_orderable = _matched_product.get(
                    "can_checkout", _matched_product.get("orderable", True)
                )
                logger.info(
                    "[ORDER FLOW] product selection validation (by name) | "
                    "name=%r external_id=%s stock_qty=%s in_stock=%s status=%s "
                    "can_checkout=%s orderable=%s tenant=%s",
                    _matched_product.get("title"),
                    _matched_product.get("external_id"),
                    _matched_product.get("stock_qty"),
                    _matched_product.get("in_stock"),
                    _matched_product.get("status"),
                    _matched_product.get("can_checkout"),
                    _matched_product.get("orderable"),
                    ctx.tenant_id,
                )
                if not _prod_orderable or not _matched_product.get("external_id"):
                    _alts = [
                        c for c in _candidates
                        if c.get("can_checkout", c.get("orderable", True))
                        and c.get("external_id")
                        and c.get("id") != _matched_product.get("id")
                    ][:3]
                    logger.warning(
                        "[ORDER FLOW] picked product NOT orderable (by name) — "
                        "suggesting %d alternatives | name=%r external_id=%s "
                        "can_checkout=%s has_external_id=%s",
                        len(_alts), _matched_product.get("title"),
                        _matched_product.get("external_id"),
                        _matched_product.get("can_checkout"),
                        bool(_matched_product.get("external_id")),
                    )
                    return Decision(
                        action=ACTION_SEARCH_PRODUCTS,
                        args={
                            "query": _matched_product.get("category") or _matched_product.get("title", ""),
                            "rejected_product": _matched_product,
                            "alternatives": _alts,
                        },
                        reason="picked product not orderable — suggest alternatives",
                        confidence=0.92,
                    )
                if facts.orderable:
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={
                            "product":        _matched_product,
                            "forced_product": _matched_product,
                            "source":         "name_match",
                            "candidate_source": "last_search_candidates",
                        },
                        reason=f"customer message matches candidate '{_matched_product.get('title')}' — start order (forced_product set)",
                        confidence=0.92,
                    )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": _matched_product.get("title", ""), "selected_product": _matched_product},
                    reason="customer picked named product — store not orderable, show details",
                    confidence=0.88,
                )

        # ── 3.5 Pick from numbered list ───────────────────────────────────────
        if intent.name == INTENT_PICK_LIST_ITEM:
            # CRITICAL: only fall back to last_recommended_products when
            # last_search_candidates is empty AND we have NO active list
            # context. Mixing the two lists causes the customer to see
            # "1. بنطلون" (from search_candidates) but get "بلوزة" (from
            # last_recommended_products) at index 0.
            _search_cands = list(state.last_search_candidates or [])
            _rec_cands = list(state.last_recommended_products or [])
            candidates = _search_cands or _rec_cands
            _candidate_source = (
                "last_search_candidates" if _search_cands
                else ("last_recommended_products" if _rec_cands else "none")
            )

            # ── Diagnostic: log numeric pick state before resolution ──────────
            _pick_msg = (ctx.message or "").strip()
            logger.info(
                "[ORDER FLOW] numeric pick debug | text=%r "
                "last_candidates_count=%d first_candidate=%r "
                "current_product_focus=%r intent=%s source=%s",
                _pick_msg,
                len(candidates),
                (candidates[0] or {}).get("title") if candidates else None,
                (state.current_product_focus or {}).get("title"),
                intent.name, _candidate_source,
            )

            if candidates:
                idx = int(intent.slots.get("list_index", 1))
                idx = max(1, min(idx, len(candidates)))
                product = candidates[idx - 1]
                if product:
                    # Use can_checkout as the single source of truth.
                    # If the product was shown in the numbered list, it
                    # MUST have can_checkout=True — any mismatch here
                    # means the catalog or state has a bug.
                    _prod_orderable = product.get(
                        "can_checkout", product.get("orderable", True)
                    )

                    # Log the FULL candidate so we can see exactly which
                    # field is missing/false when section 3.5 rejects it.
                    logger.info(
                        "[ORDER FLOW] product selection validation | "
                        "display_index=%d source=%s name=%r external_id=%s "
                        "stock_qty=%s in_stock=%s status=%s "
                        "can_checkout=%s orderable=%s tenant=%s "
                        "candidates_count=%d full_candidate=%s",
                        idx, _candidate_source,
                        product.get("title"), product.get("external_id"),
                        product.get("stock_qty"), product.get("in_stock"),
                        product.get("status"),
                        product.get("can_checkout"), product.get("orderable"),
                        ctx.tenant_id, len(candidates),
                        {k: product.get(k) for k in (
                            "id", "title", "external_id", "can_checkout",
                            "orderable", "stock_qty", "in_stock", "status",
                            "variants_in_stock",
                        )},
                    )

                    # ── Numeric pick source confirmation log (the line the
                    #    user explicitly asked to see) ────────────────────
                    logger.info(
                        "[ORDER FLOW] numeric pick source | "
                        "source=%s index=%d selected=%r external_id=%s "
                        "can_checkout=%s",
                        _candidate_source, idx,
                        product.get("title"), product.get("external_id"),
                        _prod_orderable,
                    )

                    # GUARD: when source is last_recommended_products, a
                    # missing field is far more likely (those records can
                    # come from sales-context pipelines that don't compute
                    # can_checkout). Don't reject — let DraftOrderHandler
                    # try and surface a coherent error if the product is
                    # genuinely broken.
                    _strict_reject = (
                        _candidate_source == "last_search_candidates"
                        and (not _prod_orderable or not product.get("external_id"))
                    )

                    if _strict_reject:
                        # A product that was shown in the numbered list is
                        # now failing validation — this is a catalog/state bug.
                        _alts = [
                            c for c in candidates
                            if c.get("can_checkout", c.get("orderable", True))
                            and c.get("external_id")
                            and c.get("id") != product.get("id")
                        ][:3]
                        logger.error(
                            "[ORDER FLOW] selected product mismatch | "
                            "expected=%r (index=%d) can_checkout=%s external_id=%s "
                            "stock_qty=%s in_stock=%s status=%s "
                            "bug=True — rebuilding list with %d alternatives",
                            product.get("title"), idx,
                            product.get("can_checkout"),
                            product.get("external_id"),
                            product.get("stock_qty"), product.get("in_stock"),
                            product.get("status"), len(_alts),
                        )
                        return Decision(
                            action=ACTION_SEARCH_PRODUCTS,
                            args={
                                "query": product.get("category") or product.get("title", ""),
                                "rejected_product": product,
                                "alternatives": _alts,
                            },
                            reason=f"picked product #{idx} not orderable — suggest alternatives",
                            confidence=0.95,
                        )
                    if facts.orderable:
                        # CRITICAL: pass the FULL chosen product as
                        # `forced_product` (not just `product`).  The
                        # executor MUST honour `forced_product` over
                        # `state.current_product_focus` so a stale focus
                        # (e.g. previous بلوزة) cannot win the race.
                        return Decision(
                            action=ACTION_PROPOSE_DRAFT_ORDER,
                            args={
                                "product":        product,
                                "forced_product": product,
                                "source":         "list_pick",
                                "list_index":     idx,
                                "candidate_source": _candidate_source,
                            },
                            reason=f"customer picked option {idx} from list — start order (forced_product set)",
                            confidence=0.95,
                        )
                    return Decision(
                        action=ACTION_SEARCH_PRODUCTS,
                        args={"query": product.get("title", ""),
                              "selected_product": product},
                        reason=f"customer picked option {idx} — not orderable, confirm product",
                        confidence=0.90,
                    )
            # We saw a numeric pick but have no list to map it onto.
            # GUARD: if the last bot action was a product list display
            # (search_products / narrow_choices), the candidate state
            # was lost (race condition or DB save failure). In that case
            # we MUST NOT route to current_product_focus — that would
            # show "بلوزة غير متوفر" for a customer who picked "بنطلون".
            # Instead, ask the customer to pick by name or re-run the list.
            _last_action = str(getattr(state, "last_action", "") or "")
            _list_was_last = _last_action in (
                "search_products", "narrow_choices",
                "ACTION_SEARCH_PRODUCTS", "ACTION_NARROW",
            )
            if _list_was_last:
                logger.warning(
                    "[ORDER FLOW] numeric pick with NO candidates — "
                    "last_action was a list display, candidates were lost | "
                    "last_action=%r current_product_focus=%r "
                    "— asking clarification to avoid wrong product",
                    _last_action,
                    (state.current_product_focus or {}).get("title"),
                )
                return Decision(
                    action=ACTION_CLARIFY,
                    args={
                        "question": (
                            "أرسل اسم المنتج الذي تريده، "
                            "أو اكتب \"أكثر مبيعاً\" لأعرض لك القائمة مجدداً."
                        ),
                    },
                    reason="numeric pick after list — candidates lost, re-ask",
                    confidence=0.75,
                )

            # ── If we already have a product focus + order_prep, the
            # number is likely a quantity ("1") rather than a product
            # pick — keep the order flow alive instead of breaking it.
            if state.current_product_focus and facts.orderable:
                logger.info(
                    "[ORDER FLOW] number interpreted as quantity-or-option | "
                    "product=%r — continuing order (no active candidate list)",
                    (state.current_product_focus or {}).get("title"),
                )
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": state.current_product_focus},
                    reason="numeric pick + existing product focus — continue order flow",
                    confidence=0.85,
                )
            # Otherwise: ask for clarification so the customer can
            # name the product (or repeat the search).
            return Decision(
                action=ACTION_CLARIFY,
                args={"question": "أي منتج تقصد؟ اكتب اسمه أو اطلب مني عرض المنتجات مرة ثانية."},
                reason="pick_list_item with no remembered candidates — ask for clarification",
                confidence=0.7,
            )

        # ── 3.6 Address signals BEFORE a product is picked ───────────────────
        # The customer dropped a national short code / Maps link / city
        # while we don't have a product in focus yet (e.g. they typed
        # "TAPA7401" before tapping a product). DON'T:
        #   • try to create an order (no product → 422),
        #   • ask for the address again later (we already have it),
        #   • silently lose the signal in an LLM reply.
        # DO: stash it in `state.pending_*` and tell the customer to
        # pick a product. The DraftOrderHandler consumes the pending
        # values as soon as a product is selected on the next turn.
        _has_address_signal = any(
            (intent.slots.get(k) or "").strip()
            for k in ("short_address_code", "google_maps_url", "location_url")
        )
        if (
            _has_address_signal
            and not state.current_product_focus
            and intent.name not in (INTENT_TALK_HUMAN,)
        ):
            _sc = (intent.slots.get("short_address_code") or "").strip()
            _gm = (
                intent.slots.get("google_maps_url")
                or intent.slots.get("location_url")
                or ""
            ).strip()
            _ci = (intent.slots.get("city") or "").strip()
            logger.info(
                "[ORDER FLOW] address signal received before product pick — "
                "stashing pending values | short_code=%r maps=%r city=%r tenant=%s",
                _sc, _gm[:60], _ci, ctx.tenant_id,
            )
            return Decision(
                action=ACTION_STASH_ADDRESS_PRE_PRODUCT,
                args={
                    "short_address_code": _sc,
                    "google_maps_url": _gm,
                    "city": _ci,
                },
                reason="address signal received before any product was picked",
                confidence=0.95,
            )

        # ── 3.6 Numeric message with active candidate list ────────────────────
        # Safety net: if the customer sent a bare number AND we have an
        # active candidate list, treat it as a list pick even when the
        # intent classifier returned INTENT_GENERAL / INTENT_HESITATION
        # instead of INTENT_PICK_LIST_ITEM.  Without this guard, section
        # 3.7 would grab the message and route to the OLD current_product_focus,
        # producing the "listed بنطلون, customer sent 1, bot says بلوزة غير متوفر"
        # bug.
        #
        # Candidate priority: last_search_candidates (exact displayed list)
        # then last_recommended_products (previous recommendation list).
        _msg_text = (ctx.message or "").strip()
        _active_candidates = (
            list(state.last_search_candidates or [])
            or list(state.last_recommended_products or [])
        )

        # Log numeric pick state for ALL digit messages (even INTENT_PICK_LIST_ITEM
        # cases already handled above) so we can diagnose state at entry.
        if _msg_text.isdigit() and intent.name != INTENT_PICK_LIST_ITEM:
            logger.info(
                "[ORDER FLOW] numeric pick debug | text=%r "
                "last_candidates_count=%d first_candidate=%r "
                "current_product_focus=%r intent=%s",
                _msg_text,
                len(_active_candidates),
                (_active_candidates[0] or {}).get("title") if _active_candidates else None,
                (state.current_product_focus or {}).get("title"),
                intent.name,
            )

        if (
            _msg_text.isdigit()
            and _active_candidates
            and intent.name != INTENT_PICK_LIST_ITEM
        ):
            _forced_idx = int(_msg_text)
            if 1 <= _forced_idx <= len(_active_candidates):
                _forced_product = _active_candidates[_forced_idx - 1]
                _forced_orderable = _forced_product.get(
                    "can_checkout", _forced_product.get("orderable", True)
                )

                # Guard: if stale current_product_focus differs from the
                # candidate the customer is picking, clear it first so
                # section 3.7 can never steal the message.
                _stale_focus_title = (state.current_product_focus or {}).get("title")
                _picked_title = _forced_product.get("title")
                if state.current_product_focus and _stale_focus_title != _picked_title:
                    logger.info(
                        "[ORDER FLOW] clearing stale focus before numeric pick | "
                        "stale_focus=%r picked_from_list=%r",
                        _stale_focus_title, _picked_title,
                    )

                logger.info(
                    "[ORDER FLOW] numeric pick source | source=last_search_candidates "
                    "index=%d selected=%r external_id=%s can_checkout=%s "
                    "(intent was %s — overriding to list-pick)",
                    _forced_idx, _forced_product.get("title"),
                    _forced_product.get("external_id"), _forced_orderable,
                    intent.name,
                )

                # ALWAYS route to draft-order from the candidate list —
                # even if external_id is missing.  DraftOrderHandler will
                # surface the correct "غير متوفر" message with the right
                # product name.  NOT doing this causes fall-through to
                # section 3.7 which uses the stale current_product_focus
                # (بلوزة) and produces the wrong unavailable message.
                if facts.orderable:
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={
                            "product":        _forced_product,
                            "forced_product": _forced_product,
                            "source":         "list_pick",
                            "list_index":     _forced_idx,
                            "candidate_source": "last_search_candidates",
                        },
                        reason=f"numeric pick #{_forced_idx} from active candidate list "
                               f"(intent={intent.name} overridden to list-pick, forced_product set)",
                        confidence=0.95,
                    )
                # Store not orderable — still acknowledge the pick, don't
                # silently fall through to an irrelevant template.
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={
                        "query": _forced_product.get("title", ""),
                        "selected_product": _forced_product,
                    },
                    reason=f"numeric pick #{_forced_idx} from list — store not orderable, show product",
                    confidence=0.90,
                )

        # ── 3.7 Continue order preparation while collecting checkout details ──
        # While ordering we treat slot-bearing messages and a small set of
        # "neutral" intents as continuation so the funnel doesn't reset.
        #
        # GUARD: Never fire this block when there is an active candidate list
        # (last_search_candidates non-empty).  A pending list means the
        # customer is browsing — the continuation intent should not hijack
        # their next message and route it to a stale current_product_focus.
        #
        # Two more rules to keep this from over-firing:
        #   a) ASK_PRODUCT / ASK_PRICE are NOT continuation intents on their
        #      own. A real product/price question mid-order is a request to
        #      browse, not a slot fill.
        #   b) Greeting / general / hesitation stay in the list so a polite
        #      "هلا" or "تمام" doesn't bounce the customer to the greeting
        #      template.
        _CONTINUATION_INTENTS = (
            INTENT_START_ORDER,
            INTENT_GENERAL,
            INTENT_GREETING,
            INTENT_HESITATION,
        )
        if (
            state.stage in (STAGE_ORDERING, STAGE_DECIDING)
            and state.current_product_focus
            and not state.checkout_url
            and not _active_candidates          # GUARD: no pending list
            and (
                intent.name in _CONTINUATION_INTENTS
                or any(slot in intent.slots for slot in checkout_slots)
            )
        ):
            logger.info(
                "[ORDER FLOW] numeric pick source | source=current_product_focus "
                "selected=%r (no active candidate list)",
                (state.current_product_focus or {}).get("title"),
            )
            return Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={"product": state.current_product_focus},
                reason="continue collecting checkout details for current product",
                confidence=0.88,
            )

        # ── 4. Simple FAQ / identity / shipping / contact ──────────────────
        if intent.name == INTENT_WHO_ARE_YOU:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "identity"},
                reason="customer asked who the assistant is",
            )

        if intent.name == INTENT_ASK_SHIPPING:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "shipping"},
                reason="customer asked about shipping / delivery",
            )

        if intent.name == INTENT_ASK_STORE_INFO:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "store_info"},
                reason="customer asked for store info / link / location",
            )

        if intent.name == INTENT_ASK_OWNER_CONTACT:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "owner_contact"},
                reason="customer asked for contact details",
            )

        # ── 5. Greeting (explicit greeting or first-turn generic help) ─────
        # Two hard rules to prevent the "bot keeps re-greeting mid-order" bug:
        #   a) NEVER greet if the customer is in a committed sales stage
        #      (deciding/ordering/checkout). The continuation block above
        #      already routes those messages back into the order flow.
        #   b) NEVER greet twice in the same conversation: once `greeted`
        #      is true, only an explicit INTENT_GREETING re-triggers the
        #      template, and even then we only do it when the funnel is
        #      back at discovery (e.g. after an order completed).
        _greet_locked = state.stage in (
            STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT,
        )
        if not _greet_locked:
            if intent.name == INTENT_GREETING and not state.greeted:
                return Decision(
                    action=ACTION_GREET,
                    reason="explicit greeting on first turn",
                )
            if not state.greeted and intent.name == INTENT_GENERAL:
                return Decision(
                    action=ACTION_GREET,
                    reason="first-turn general help",
                )

        # ── 6. Start order — product in focus ──────────────────────────────
        if intent.name == INTENT_START_ORDER:
            if state.current_product_focus and facts.has_products:
                # Only propose order if store can actually fulfil it
                if facts.orderable:
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={"product": state.current_product_focus},
                        reason="customer wants to buy the product currently in focus",
                        confidence=0.90,
                    )
                else:
                    # Integration missing or all out-of-stock
                    return Decision(
                        action=ACTION_LLM_REPLY,
                        reason="store not orderable (no integration or all out-of-stock)",
                    )
            elif facts.has_products:
                query = intent.slots.get("product_query", "").strip()
                if not query:
                    # Customer said "أبغى أطلب" with no product mentioned
                    return Decision(
                        action=ACTION_CLARIFY,
                        args={"question": "ما المنتج الذي تودّ طلبه؟ يمكنك ذكر الاسم أو الوصف."},
                        reason="start_order with no product query — ask for clarification",
                        confidence=0.85,
                    )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query, "after_search": "propose_order"},
                    reason="customer wants to buy but no product focus — search first",
                    confidence=0.80,
                )

        # ── 7. Ask about product or price ─────────────────────────────────
        if intent.name in (INTENT_ASK_PRODUCT, INTENT_ASK_PRICE):
            if facts.has_products:
                query = (
                    intent.slots.get("product_query")
                    or intent.slots.get("product_name")
                    or ctx.message
                )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query},
                    reason=f"customer {intent.name} — search catalog",
                )
            else:
                # No products in DB — go to LLM to apologise gracefully
                return Decision(
                    action=ACTION_LLM_REPLY,
                    reason="no products in catalog — LLM apologises",
                )

        # ── 8. Hesitation with product focus & coupons ───────────────────
        if intent.name == INTENT_HESITATION:
            if state.current_product_focus and facts.has_coupons and facts.has_products:
                return Decision(
                    action=ACTION_SUGGEST_COUPON,
                    args={"product": state.current_product_focus},
                    reason="customer hesitating — nudge with a coupon",
                    confidence=0.75,
                )

        # ── 8.5 Upsell / addon recommendation ────────────────────────────
        if (
            state.current_product_focus
            and ctx.sales_context
            and ctx.sales_context.recommendations
            and intent.name in (INTENT_START_ORDER, INTENT_PAY_NOW, INTENT_ASK_PRODUCT)
        ):
            return Decision(
                action=ACTION_RECOMMEND_ADDON,
                args={"query": state.current_product_focus.get("category", "")},
                reason="customer close to purchase with recommendations available",
                confidence=0.68,
            )

        # ── 8.6 Web research when store knowledge likely insufficient ─────
        if (
            intent.name == INTENT_GENERAL
            and ctx.sales_context
            and not ctx.facts.has_products
            and len(ctx.message.split()) >= 4
        ):
            return Decision(
                action=ACTION_WEB_SEARCH,
                args={"query": ctx.message},
                reason="general knowledge question with weak store context",
                confidence=0.55,
            )

        # ── 9.5 Ordering-stage safety net ────────────────────────────────
        # NEVER let a message reach the LLM when the customer is actively
        # placing an order.  If all the specific rules above failed to match,
        # we have a product in focus → continue collecting checkout slots.
        # This is the last line of defence before LLM fallback.
        if state.stage in (STAGE_ORDERING, STAGE_DECIDING):
            if state.current_product_focus and facts.orderable and not state.checkout_url:
                logger.info(
                    "[ORDER FLOW] FORCED action=propose_draft_order "
                    "reason=ordering_stage_safety_net | tenant=%s product=%r intent=%s "
                    "— preventing llm_reply during active checkout",
                    ctx.tenant_id,
                    (state.current_product_focus or {}).get("title"),
                    intent.name,
                )
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": state.current_product_focus},
                    reason=f"ordering_stage_safety_net: intent={intent.name} fell through all rules — force checkout continuation",
                    confidence=0.80,
                )
            # Product focus was lost (unsyncable product cleared it) but
            # customer is still in ordering stage → search for a replacement.
            if not state.current_product_focus and facts.has_products:
                _query = (
                    intent.slots.get("product_query")
                    or intent.slots.get("product_name")
                    or ""
                )
                logger.info(
                    "[ORDER FLOW] ordering stage with no product focus — "
                    "directing to search | tenant=%s intent=%s query=%r",
                    ctx.tenant_id, intent.name, _query,
                )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS if _query else ACTION_CLARIFY,
                    args=(
                        {"query": _query}
                        if _query
                        else {"question": "ما المنتج الذي تودّ طلبه؟ يمكنك ذكر الاسم أو قول «أكثر مبيعاً»."}
                    ),
                    reason="ordering_stage_safety_net: no product focus — ask customer to pick",
                    confidence=0.75,
                )

        # ── 9. Fallback: LLM ─────────────────────────────────────────────
        return Decision(
            action=ACTION_LLM_REPLY,
            reason=f"no rule matched for intent={intent.name} — LLM fallback",
            confidence=0.50,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_ar(text: str) -> str:
    """Lightweight Arabic normalization for product title matching."""
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)  # diacritics + tatweel
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\u0621-\u064Aa-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _match_product_from_message(
    message: str,
    candidates: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the candidate product whose title best matches the message, or None.

    Matching strategy (in order of priority):
      1. Exact normalized title match
      2. Title is a contiguous substring of the message (or vice-versa)
      3. All title words appear in the message

    Minimum title length: 2 characters (avoids false positives on single-char
    titles). The message must contain at least the title to avoid matching
    on irrelevant keywords.
    """
    msg_norm = _normalize_ar(message)
    if not msg_norm:
        return None

    best: Optional[Dict[str, Any]] = None
    best_score = 0

    for prod in candidates:
        title = str(prod.get("title") or "").strip()
        if len(title) < 2:
            continue
        title_norm = _normalize_ar(title)
        if not title_norm:
            continue

        score = 0
        # Exact match
        if title_norm == msg_norm:
            score = 100
        # Title is a substring of message (e.g. "بلورة" inside "بلورة 179.0 ر")
        elif title_norm in msg_norm:
            score = 80
        # Message is a substring of title (customer typed abbreviation)
        elif msg_norm in title_norm and len(msg_norm) >= 3:
            score = 60
        # All title words appear somewhere in the message
        else:
            title_words = [w for w in title_norm.split() if len(w) >= 2]
            if title_words and all(w in msg_norm for w in title_words):
                score = 40 + len(title_words) * 5

        if score > best_score:
            best_score = score
            best = prod

    # Require at least a substring match to avoid false positives
    return best if best_score >= 40 else None
