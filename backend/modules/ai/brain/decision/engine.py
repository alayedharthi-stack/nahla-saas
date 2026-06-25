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
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
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
)
from ..types import (
    INTENT_ASK_COD,
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_GREETING,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_ONLINE_STORE_INQUIRY,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_HESITATION,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_PLATFORM_INQUIRY,
    INTENT_SOCIAL,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_GENERAL,
    INTENT_WHO_ARE_YOU,
    INTENT_COMPLAINT_REFUND,
    INTENT_PICK_LIST_ITEM,
    INTENT_PERSONA_INTERACTION,
)
from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING

logger = logging.getLogger("nahla.brain.decision")


def _is_commerce_blocked(ctx: BrainContext) -> bool:
    """True when non-commerce safety layer forbids catalog escalation."""
    try:
        slots = getattr(ctx.intent, "slots", None) or {}
        if slots.get("block_commerce_escalation"):
            return True
        from ..intent.non_commerce_classifier import resolve_commerce_block  # noqa: PLC0415
        intent = getattr(ctx, "intent", None)
        _profile = getattr(ctx, "profile", None) or {}
        _in_meta = (
            _profile.get("inbound_metadata")
            if isinstance(_profile, dict) else None
        )
        nc = resolve_commerce_block(
            ctx.message or "",
            inbound_metadata=_in_meta if isinstance(_in_meta, dict) else None,
            intent_name=getattr(intent, "name", None),
            intent_confidence=getattr(intent, "confidence", None),
        )
        if nc is not None:
            logger.info(
                "[NON_COMMERCE_BLOCK] tenant=%s category=%s source=%s preview=%r",
                getattr(ctx, "tenant_id", None),
                nc.category,
                nc.source,
                (ctx.message or "")[:60],
            )
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _goal_based_commerce_decision(ctx: BrainContext) -> Optional[Decision]:
    """Structured KB regimen — only when pipeline composed a resolved bundle."""
    _goal_bundle = getattr(ctx, "goal_regimen_bundle", None)
    if _goal_bundle is None or getattr(_goal_bundle, "resolved_count", 0) <= 0:
        return None
    intent = getattr(ctx, "intent", None)
    if intent is None or intent.name not in {
        INTENT_NEED_BASED_PRODUCT_ADVICE,
        "need_based_product_advice",
        "solution_seeking_commerce",
    }:
        return None
    try:
        from ..commerce.goal.telemetry import log_goal_commerce  # noqa: PLC0415

        log_goal_commerce(
            tenant_id=ctx.tenant_id,
            goal=str(getattr(_goal_bundle, "goal", "") or ""),
            kb_hits=1,
            selected_bundle=str(getattr(_goal_bundle, "title", "") or ""),
            resolved_products=int(_goal_bundle.resolved_count),
            unresolved_products=len(getattr(_goal_bundle, "unresolved_refs", []) or []),
            retrieval_source="goal_based_recommendation",
            fallback_used=False,
            final_action="goal_based_commerce",
            preview=ctx.message or "",
        )
    except Exception:  # noqa: BLE001
        pass
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "goal_based_commerce",
            "goal": _goal_bundle.goal,
            "regimen_bundle": _goal_bundle.to_dict(),
            "response_goal": "goal_based_commerce",
        },
        reason="goal-based KB regimen — structured bundle composed",
        confidence=0.94,
    )


# ── Direct Answer First (DAF) — first-turn welcome bypass ────────────────────
#
# Production regression (May 2026 #20 — "voice note with invoice question"):
# A customer's first-turn message can carry a real, actionable question even
# when the rule classifier returns INTENT_GENERAL (or a high-confidence
# INTENT_GREETING that did not get demoted by the rules-layer welcome gate).
# This happens routinely for media-origin inputs that get flattened to plain
# text BEFORE reaching the brain:
#
#   * voice notes        → Whisper transcript ("السلام عليكم وسهل الخير. اقول
#                          لك فيه فاتورة بتاريخ ... يعني انا كده سددت اسدد
#                          فاتورة اثنين؟")
#   * status replies     → reply-to-status preamble + caption
#   * captioned images   → OCR / vision summary + caption
#   * captioned videos   → frame description + caption
#
# The customer clearly asked a question. The rule layer either could not pin
# a specific intent (long, conversational transcript) or routed it to
# INTENT_GENERAL without ``embedded_greeting=True`` because the greeting
# wasn't the *best* candidate to begin with. The engine then short-circuits
# to ACTION_GREET on the "first-turn general help" branch and the customer
# gets a self-introduction card instead of an answer — exactly what the
# merchant reported as feeling like "the bot didn't read my message".
#
# Fix: before either first-turn greet branch fires, check whether the
# customer's message carries substantive actionable content. We reuse the
# greeting-residue stripper from the rules layer so the threshold matches
# the existing "is this a greeting + question hybrid?" detector. Pure tiny
# inputs ("اي" / "ok" / "هلا") fail the residue check and still trigger
# the welcome card; anything with a real question or request falls through
# to the rest of the engine, where the LLM fallback (rule #9) composes a
# proper answer with full KB/catalog/history context.

# Word-character count above which a first-turn message is treated as a
# real ask and the welcome card is bypassed. Three is the same floor the
# rules layer uses for INTENT_GREETING demotion (`_GREETING_RESIDUE_MIN_CHARS`),
# kept in sync on purpose so the two layers agree on what counts as
# "substantive". Tested boundary cases:
#   * "اي"          → 2 chars  → still greets
#   * "نعم"         → 3 chars  → bypasses (a 3-char ack on first turn is
#                                 vanishingly rare; a real ask like "وش"
#                                 also ≥ 3 should bypass)
#   * "كم سعره؟"    → 6 chars  → bypasses
#   * voice transcript with invoice question → 100+ chars → bypasses
_DAF_FIRST_TURN_MIN_RESIDUE = 3


def _first_turn_has_actionable_substance(message: Optional[str]) -> bool:
    """Return True when a first-turn message carries enough content to skip
    the welcome card and let the LLM answer directly.

    Reuses the rules-layer greeting/courtesy stripper so leading salaams,
    bot tags ("نحلة") and how-are-you fillers are removed before measuring
    residue. Voice transcripts, captions, OCR text and reply-to-status
    snippets all enter the brain as plain text, so a single substance check
    on the customer-facing message text covers every media origin.

    Defensive: if the rules module cannot be imported for any reason
    (circular import, partial test stub, etc.) we fall back to a raw
    word-character count so the bypass still works.
    """
    if not message:
        return False
    raw = message.strip()
    if not raw:
        return False
    try:
        from ..intent.rules import (  # noqa: PLC0415
            _GREETING_RESIDUE_WORD_CHARS_RE,
            _strip_greeting_residue,
        )
        residue = _strip_greeting_residue(raw)
        if not residue:
            return False
        n_chars = len(_GREETING_RESIDUE_WORD_CHARS_RE.findall(residue))
        return n_chars >= _DAF_FIRST_TURN_MIN_RESIDUE
    except Exception:
        # Last-ditch fallback: any message with ≥ 8 raw word characters
        # is almost certainly substantive. Slightly stricter than the
        # rules-aware path to reduce false bypass when courtesy tokens
        # cannot be stripped.
        return len(re.findall(r"[\w\u0600-\u06FF]", raw)) >= 8


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

        def _product_discovery_blocked(source: str = "") -> bool:
            try:
                from ..order_context_gate import (  # noqa: PLC0415
                    fulfillment_lock_reason,
                    log_fulfillment_lock,
                    log_order_context_block,
                )
                from ..product_discovery_gate import (  # noqa: PLC0415
                    log_product_discovery_blocked,
                    product_discovery_block_reason,
                )

                _reason = product_discovery_block_reason(
                    ctx,
                    source=source or None,
                )
                if _reason:
                    log_product_discovery_blocked(
                        tenant_id=getattr(ctx, "tenant_id", None),
                        reason=_reason,
                        preview=(ctx.message or "")[:80],
                        source=source or "-",
                    )
                    if _reason == "active_fulfillment":
                        _fr = fulfillment_lock_reason(ctx) or "fulfillment_session"
                        log_fulfillment_lock(
                            tenant_id=getattr(ctx, "tenant_id", None),
                            reason=_fr,
                            preview=(ctx.message or "")[:80],
                        )
                        log_order_context_block(
                            tenant_id=getattr(ctx, "tenant_id", None),
                            reason=_fr,
                            preview=(ctx.message or "")[:80],
                        )
                    return True
            except Exception:  # noqa: BLE001
                pass
            return False

        def _fulfillment_locked_fallback() -> Optional[Decision]:
            try:
                from ..state.state_relevance import (  # noqa: PLC0415
                    log_state_resurrection_blocked,
                    should_block_workflow_resume,
                    validate_state_relevance,
                )
                _verdict = getattr(ctx, "state_relevance", None) or validate_state_relevance(ctx)
                if should_block_workflow_resume("active_fulfillment", _verdict):
                    log_state_resurrection_blocked(
                        tenant_id=getattr(ctx, "tenant_id", None),
                        blocked_state="active_fulfillment",
                        reason="semantic_mismatch",
                        preview=(ctx.message or "")[:80],
                        intent_hint=_verdict.current_intent_hint,
                    )
                    return None
                from ..order_context_gate import (  # noqa: PLC0415
                    try_fulfillment_lock_continuation,
                    try_order_context_update_decision,
                )
                _upd = try_order_context_update_decision(ctx)
                if _upd is not None:
                    return _upd
                return try_fulfillment_lock_continuation(ctx)
            except Exception:  # noqa: BLE001
                return None

        def _state_relevance():
            try:
                from ..state.state_relevance import validate_state_relevance  # noqa: PLC0415

                return getattr(ctx, "state_relevance", None) or validate_state_relevance(ctx)
            except Exception:  # noqa: BLE001
                return None

        def _support_listing_blocks_checkout() -> bool:
            try:
                _verdict = _state_relevance()
                return bool(
                    _verdict is not None
                    and getattr(_verdict, "support_listing_topic_shift", False)
                )
            except Exception:  # noqa: BLE001
                return False

        def _product_correction_or_info_blocks_checkout() -> bool:
            try:
                _verdict = _state_relevance()
                if _verdict is not None and (
                    getattr(_verdict, "product_correction_topic_shift", False)
                    or getattr(_verdict, "product_information_topic_shift", False)
                ):
                    return True
                from ..state.product_information_topic import (  # noqa: PLC0415
                    product_information_blocks_checkout,
                )

                return product_information_blocks_checkout(ctx)
            except Exception:  # noqa: BLE001
                return False

        def _checkout_topic_blocks() -> bool:
            return (
                _support_listing_blocks_checkout()
                or _product_correction_or_info_blocks_checkout()
            )

        def _block_stale_resume(workflow: str, *, reason: str = "semantic_mismatch") -> bool:
            try:
                from ..state.state_relevance import (  # noqa: PLC0415
                    log_state_resurrection_blocked,
                    should_block_workflow_resume,
                )
                _verdict = _state_relevance()
                if _verdict is None or not should_block_workflow_resume(workflow, _verdict):
                    return False
                log_state_resurrection_blocked(
                    tenant_id=getattr(ctx, "tenant_id", None),
                    blocked_state=workflow,
                    reason=reason,
                    preview=(ctx.message or "")[:80],
                    intent_hint=_verdict.current_intent_hint,
                )
                return True
            except Exception:  # noqa: BLE001
                return False

        # ── -1. Prediction confirmation (absolute highest priority) ─────────
        # ── Variant choice gate (Phase 3 — migration 0064) ─────────────
        # The customer asked for a parent product that had 2+ in-stock
        # variants; the responder shipped ``ask_product_variants`` and
        # set ``awaiting_variant_choice=True`` + pinned the parent's id
        # on the conversation. THIS turn's job is to map the customer's
        # message (digit or variant label) back to a real variant and
        # then re-route to a normal product-card send with the chosen
        # variant pinned. Fires BEFORE any other rule so the customer
        # never gets dropped into a search loop just because they said
        # "2".
        _order_prep = getattr(state, "order_prep", None)
        _awaiting_variant = bool(getattr(_order_prep, "awaiting_variant_choice", False))
        if _awaiting_variant and _order_prep:
            _msg = (ctx.message or "").strip()
            _picked = None
            # Numeric pick — "1" / "٢" / "3."
            _m = re.match(r"^\s*([1-9]\d?|[١٢٣٤٥٦٧٨٩][٠-٩]?)\s*\.?", _msg)
            if _m:
                _digits = _m.group(1)
                _trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
                try:
                    _idx = int(_digits.translate(_trans))
                except (TypeError, ValueError):
                    _idx = 0
                if _idx > 0:
                    _picked = {"index_one_based": _idx}
            # Fallback: free-text match against option summary
            if _picked is None and len(_msg) >= 1:
                _picked = {"label": _msg}
            if _picked is not None:
                logger.info(
                    "[VARIANT_PICK] tenant=%s parent_product_id=%s pick=%r",
                    ctx.tenant_id, _order_prep.pending_variant_product_id,
                    _picked,
                )
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={
                        "variant_pick": _picked,
                        "pending_variant_product_id":
                            _order_prep.pending_variant_product_id,
                    },
                    reason="awaiting_variant_choice — mapped customer pick",
                    confidence=0.97,
                )

        # When the system proposed predicted options and is waiting for the
        # customer to confirm/reject, ALWAYS route to the draft-order handler
        # so the handler can process the confirm/reject logic.  This MUST
        # fire before any other rule to prevent the prediction confirmation
        # turn from being misrouted to LLM_REPLY or search.
        _awaiting_pred = getattr(state, "awaiting_option_confirmation", False)
        if _awaiting_pred and state.current_product_focus:
            _pred_source = "prediction_confirmation"
            _SAME_BEFORE_KW = {"نفس السابق", "نفس الخيارات", "نفس اللي قبل",
                               "زي المرة اللي فاتت", "نفس الاختيار", "نفس الطلب",
                               "زي قبل", "نفسها", "نفسه"}
            _msg_l = (ctx.message or "").strip().lower()
            _is_same_before = any(kw in _msg_l for kw in _SAME_BEFORE_KW)
            if _is_same_before:
                _pred_source = "prediction_confirmation_same_as_before"

            logger.info(
                "[ORDER OPTIONS PREDICT] routing prediction response | "
                "tenant=%s action=propose_draft_order source=%s message=%r",
                ctx.tenant_id, _pred_source, _msg_l[:40],
            )
            return Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={
                    "product": state.current_product_focus,
                    "prediction_action": _pred_source,
                },
                reason=f"awaiting_option_confirmation — {_pred_source}",
                confidence=0.99,
            )

        # ── -0.46 Post-purchase product feedback (P0 — external outbound context) ──
        try:
            from ..commerce.post_purchase_feedback_guard import (  # noqa: PLC0415
                try_post_purchase_feedback_decision,
            )

            _pp_feedback = try_post_purchase_feedback_decision(ctx)
            if _pp_feedback is not None:
                return _pp_feedback
        except Exception as _pp_feedback_exc:  # noqa: BLE001  # noqa: silent-ok — post-purchase route probe must not block turn
            logger.debug(
                "[POST_PURCHASE_FEEDBACK] routing skipped err=%s",
                _pp_feedback_exc,
            )

        # ── -0.45 Complaint / refund / fraud (P0 — beats order mis-route) ──
        try:
            from ..commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                try_complaint_refund_decision,
            )

            _complaint_dec = try_complaint_refund_decision(ctx)
            if _complaint_dec is not None:
                return _complaint_dec
        except Exception as _complaint_exc:  # noqa: BLE001
            logger.debug(
                "[COMPLAINT_REFUND_GUARD] routing skipped err=%s",
                _complaint_exc,
            )

        # ── -0.5 Pending cart confirmation (P0 gift-order gate) ─────────────
        try:
            from ..commerce.gift_order_gate import try_pending_cart_confirmation_decision  # noqa: PLC0415

            _pcc = try_pending_cart_confirmation_decision(ctx)
            if _pcc is not None:
                return _pcc
        except Exception as _pcc_exc:  # noqa: BLE001
            logger.debug(
                "[GIFT_ORDER_GATE] pending_cart_confirmation skipped err=%s",
                _pcc_exc,
            )

        # ── -0.4 Ready-for-order creation (P0 gift-order gate) ──────────────
        try:
            from ..commerce.gift_order_gate import try_ready_for_order_decision  # noqa: PLC0415

            _rfo = try_ready_for_order_decision(ctx)
            if _rfo is not None:
                logger.info(
                    "[GIFT_ORDER_GATE] ready_for_order_creation tenant=%s reason=%s",
                    ctx.tenant_id,
                    _rfo.reason,
                )
                return _rfo
        except Exception as _rfo_exc:  # noqa: BLE001
            logger.debug(
                "[GIFT_ORDER_GATE] ready_for_order skipped err=%s",
                _rfo_exc,
            )

        # ── 0z. Reference resolution: bare confirmation inherits last topic ──
        # A short "نعم" / "طيب" / "أرسل" / "اي" / "okay" on its own carries
        # no commercial signal by itself, but in conversation it almost
        # always means "yes, do what you just offered". When the previous
        # turn left ``state.last_platform_topic`` set (the customer asked
        # about subscription / API / Meta-link etc.), an isolated "نعم"
        # MUST inherit that topic so we re-emit the platform-aware reply
        # instead of falling into a generic greet / OOS branch.
        #
        # We are deliberately conservative: only fires when the message
        # is ≤ 4 short tokens AND contains nothing but a confirmation
        # word. Anything longer carries its own signal and follows the
        # normal intent ladder.
        _conf_msg = (ctx.message or "").strip().lower()
        _CONFIRM_WORDS = {
            "نعم", "ايوه", "ايوة", "أيوه", "أيوة", "ايو", "اي", "أي",
            "طيب", "تمام", "اوكي", "أوكي", "اوك", "اوكيه", "موافق",
            "ارسل", "أرسل", "ابعث", "ابعثه", "ابعثها", "ابعثلي",
            "ابغى", "أبغى", "ابي", "أبي", "ودي",
            # May 2026 #5 — additional Gulf affirmations the LLM tail kept
            # missing because they weren't in the set: "حسنا/ماشي/تكفى"
            # are everyday "go ahead" tokens, "okey/okie" are common Latin
            # mis-spellings the prior strict matcher rejected.
            "حسنا", "حسناً", "ماشي", "تكفى", "تكفي",
            "yes", "yep", "ok", "okay", "okey", "okie", "sure", "go",
            "send", "send it",
        }
        # Multi-token Gulf affirmations that don't decompose into all-token
        # confirm-words (e.g. "ي ريت" has a bare "ي" that we deliberately
        # keep OUT of the set to avoid false-positives on inflected verbs
        # starting with "ي"). Matched on the full normalised string.
        _BARE_CONFIRM_PHRASES = frozenset({
            "ي ريت", "يا ريت", "ياريت",
            "تمام تمام", "اي اي", "نعم نعم", "تمام يا غالي",
            "اوكي تمام", "تمام اوكي", "اوك تمام", "تمام اوك",
            "go ahead", "do it",
        })
        # Positive-only emoji set — bare "👍" / "🙏" / "✅" after the bot
        # offered something (link / product card / image) is a clear
        # "yes, do it". The regex anchors the whole message to one or
        # more positive emojis so it never matches a message that ALSO
        # carries text.
        _POSITIVE_EMOJIS_ONLY_RE = re.compile(
            r"^[\s\u200B-\u200F]*"
            r"[\U0001F44D\U0001F44C\U0001F64F\u2705\U0001F4AF\U0001F31F\u2728]+"
            r"[\s\u200B-\u200F]*$",
            re.UNICODE,
        )
        _conf_tokens = [t for t in re.split(r"\s+", _conf_msg) if t]
        _is_bare_confirmation = (
            _conf_msg in _BARE_CONFIRM_PHRASES
            or bool(_POSITIVE_EMOJIS_ONLY_RE.match(ctx.message or ""))
            or (
                1 <= len(_conf_tokens) <= 4
                and all(t in _CONFIRM_WORDS for t in _conf_tokens)
            )
        )
        if (
            _is_bare_confirmation
            and not state.checkout_url
            and not state.current_product_focus
            and getattr(state, "last_platform_topic", "")
        ):
            _last_topic = str(getattr(state, "last_platform_topic", "") or "general_platform")
            logger.info(
                "[CTX_INHERIT] bare confirmation inherits platform topic | "
                "tenant=%s topic=%s preview=%r",
                getattr(ctx, "tenant_id", None), _last_topic, _conf_msg[:40],
            )
            return Decision(
                action=ACTION_PLATFORM_REPLY,
                args={
                    "platform_topic": _last_topic,
                    "inherited_from_context": True,
                },
                reason=f"bare-confirmation inherits last_platform_topic={_last_topic}",
                confidence=0.85,
            )

        # ── 0. Semantic turn interpretation (Phase 1) ───────────────────
        # Context-aware repair for short/ambiguous replies. Runs BEFORE
        # social/courtesy routing so "كل الحجام" after a size question
        # does not fall through to generic/social paths.
        try:
            from ..interpret.semantic_routing import (  # noqa: PLC0415
                try_semantic_interpretation_decision,
            )
            _sem_dec = try_semantic_interpretation_decision(ctx)
            if _sem_dec is not None:
                return _sem_dec
        except Exception as _sem_route_exc:  # noqa: BLE001
            logger.debug(
                "[SEMANTIC_TURN_INTERPRETER] routing skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None), _sem_route_exc,
            )

        # ── 0. Social & Human Context Layer (P0) ───────────────────────────
        try:
            from ..social_human_context import try_social_human_context_decision  # noqa: PLC0415

            _shc_dec = try_social_human_context_decision(ctx)
            if _shc_dec is not None:
                return _shc_dec
        except Exception as _shc_route_exc:  # noqa: BLE001
            logger.debug(
                "[SOCIAL_HUMAN_CONTEXT] routing skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None), _shc_route_exc,
            )

        # ── 0a. Social / courtesy / religious (May 2026 #4) ─────────────────
        # The intent classifier set INTENT_SOCIAL when the customer's
        # message is a deterministic social ACK — thanks, blessing,
        # prophet invocation, basmala, compliment, courtesy. These
        # carry zero commercial intent. We MUST NOT route to product
        # / KB / LLM here because the sales-oriented prompt would
        # either stay silent or derail into a sales pitch — that's
        # the exact regression we're fixing.
        #
        # Sitting at priority 0a (right after prediction-confirmation)
        # means we run BEFORE every commerce branch. If the customer
        # is mid-checkout, the ACK is harmless — the order state
        # stays intact and the next turn resumes normally. The only
        # signal in the message was "thanks", and we honour it.
        if intent.name == INTENT_SOCIAL:
            category = str((intent.slots or {}).get("social_category") or "general_courtesy")
            logger.info(
                "[SOCIAL_ROUTE] tenant=%s category=%s preview=%r",
                getattr(ctx, "tenant_id", None), category, (ctx.message or "")[:60],
            )
            # P1-F — personality social (thanks/blessing/courtesy/warmth) →
            # LLM persona compose. Compliment keeps merchant_praise_ack.
            # Occasion/safety categories use build_social_courtesy_decision.
            from ..persona_expression import build_social_courtesy_decision  # noqa: PLC0415

            if category == "compliment":
                from ..cost.intent_cost_policy import (  # noqa: PLC0415
                    should_avoid_llm_for_social_category,
                )

                if not should_avoid_llm_for_social_category(category):
                    return Decision(
                        action=ACTION_LLM_REPLY,
                        args={
                            "topic": "merchant_praise_ack",
                            "social_category": category,
                        },
                        reason=f"merchant praise — generative warmth ack ({category})",
                        confidence=intent.confidence,
                    )

            return build_social_courtesy_decision(
                category,
                confidence=intent.confidence,
                reason=f"social courtesy ack ({category})",
            )

        # ── 0a.42 Persona social / emotional (Phase 2 routing) ─────────────
        if intent.name == INTENT_PERSONA_INTERACTION:
            _p_topic = str(
                (intent.slots or {}).get("persona_topic") or "persona_social"
            ).strip()
            _p_kind = str((intent.slots or {}).get("persona_kind") or "").strip()
            try:
                from ..intent.persona_interaction_classifier import (  # noqa: PLC0415
                    log_persona_route,
                )
                log_persona_route(
                    tenant_id=getattr(ctx, "tenant_id", None),
                    persona_topic=_p_topic,
                    persona_kind=_p_kind,
                    preview=(ctx.message or "")[:64],
                )
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                "[PERSONA_ROUTE] route=persona_social kind=%s tenant=%s preview=%r",
                _p_kind or "-",
                getattr(ctx, "tenant_id", None),
                (ctx.message or "")[:60],
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": _p_topic,
                    "persona_kind": _p_kind,
                    "block_commerce_escalation": True,
                },
                reason=f"persona interaction — {_p_topic}/{_p_kind or 'social'}",
                confidence=intent.confidence,
            )

        # ── 0a.5 Non-commerce media / greeting OCR (May 2026) ───────────────
        # Long Eid dua / greeting images bypass ``classify_social`` length
        # limits but still carry zero buying intent. When the classifier
        # (or intent slots) marks the turn, block ALL commerce branches
        # below — including text-pattern top_products / replay fallbacks
        # and LLM catalog drift — and respond socially instead.
        # P1-D-3: occasion-gated categories require explicit inbound signal;
        # product/honey media without occasion falls through to commerce/LLM.
        if _is_commerce_blocked(ctx) and intent.name not in (
            INTENT_WHO_ARE_YOU,
            INTENT_PERSONA_INTERACTION,
        ):
            nc_category = str(
                (intent.slots or {}).get("social_category") or "religious_media"
            )
            _occasion_social = frozenset({
                "eid_greeting", "dua", "religious_media",
            })
            if nc_category in _occasion_social:
                from ..intent.non_commerce_classifier import (  # noqa: PLC0415
                    inbound_has_occasion_signal,
                )
                if not inbound_has_occasion_signal(ctx.message or ""):
                    if nc_category == "religious_media":
                        from ..persona_expression import (  # noqa: PLC0415
                            build_social_courtesy_decision,
                        )

                        logger.info(
                            "[NON_COMMERCE_ROUTE] tenant=%s category=%s "
                            "route=persona_compose preview=%r",
                            getattr(ctx, "tenant_id", None),
                            nc_category,
                            (ctx.message or "")[:60],
                        )
                        return build_social_courtesy_decision(
                            nc_category,
                            confidence=max(float(intent.confidence or 0.0), 0.94),
                            reason=(
                                "non-commerce religious media — persona compose "
                                "(no occasion template gate)"
                            ),
                            block_commerce=True,
                        )
                    logger.info(
                        "[NON_COMMERCE_ROUTE] tenant=%s category=%s "
                        "skipped=occasion_gate preview=%r",
                        getattr(ctx, "tenant_id", None),
                        nc_category,
                        (ctx.message or "")[:60],
                    )
                else:
                    logger.info(
                        "[NON_COMMERCE_ROUTE] tenant=%s category=%s preview=%r",
                        getattr(ctx, "tenant_id", None),
                        nc_category,
                        (ctx.message or "")[:60],
                    )
                    return Decision(
                        action=ACTION_SOCIAL_REPLY,
                        args={
                            "social_category": nc_category,
                            "block_commerce_escalation": True,
                        },
                        reason=f"non-commerce safety gate ({nc_category})",
                        confidence=max(float(intent.confidence or 0.0), 0.94),
                    )
            else:
                logger.info(
                    "[NON_COMMERCE_ROUTE] tenant=%s category=%s preview=%r",
                    getattr(ctx, "tenant_id", None),
                    nc_category,
                    (ctx.message or "")[:60],
                )
                from ..persona_expression import build_social_courtesy_decision  # noqa: PLC0415

                return build_social_courtesy_decision(
                    nc_category,
                    confidence=max(float(intent.confidence or 0.0), 0.94),
                    reason=f"non-commerce safety gate ({nc_category})",
                    block_commerce=True,
                )

        # ── 0a.52 CatalogNavigator ownership (before discovery/search) ────
        try:
            from ..catalog.navigation import try_catalog_navigation_decision  # noqa: PLC0415

            _catalog_nav_dec = try_catalog_navigation_decision(ctx)
            if _catalog_nav_dec is not None:
                return _catalog_nav_dec
        except Exception as _cat_nav_exc:  # noqa: BLE001  # noqa: silent-ok — catalog navigator hook must not block decide
            logger.debug(
                "[CATALOG_NAVIGATOR] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _cat_nav_exc,
            )

        # ── 0a.515 CatalogNavigator group-product pick (before selection_context) ─
        try:
            from ..catalog.product_pick import try_catalog_navigation_product_pick_decision  # noqa: PLC0415

            _nav_product_pick = try_catalog_navigation_product_pick_decision(ctx)
            if _nav_product_pick is not None:
                return _nav_product_pick
        except Exception as _nav_pick_exc:  # noqa: BLE001  # noqa: silent-ok — navigator pick hook must not block decide
            logger.debug(
                "[CATALOG_NAVIGATOR] product_pick skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _nav_pick_exc,
            )

        # ── 0a.516 Group products numeric hard guard (block legacy collection pick) ─
        try:
            from ..catalog.numeric_ownership import try_group_products_numeric_guard_decision  # noqa: PLC0415

            _gp_numeric_guard = try_group_products_numeric_guard_decision(ctx)
            if _gp_numeric_guard is not None:
                return _gp_numeric_guard
        except Exception as _gp_guard_exc:  # noqa: BLE001  # noqa: silent-ok — numeric guard must not block decide
            logger.debug(
                "[NUMERIC_OWNERSHIP] group_products guard skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _gp_guard_exc,
            )

        # ── 0a.535 Selection context follow-up (Phase 4B) ─────────────────
        try:
            from ..commerce.selection_context import try_selection_context_decision  # noqa: PLC0415

            _selection_dec = try_selection_context_decision(ctx)
            if _selection_dec is not None:
                return _selection_dec
        except Exception as _sel_exc:  # noqa: BLE001  # noqa: silent-ok — selection context hook must not block decide
            logger.debug(
                "[SELECTION_CONTEXT] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _sel_exc,
            )

        # ── 0a.54 Types/options overview (beats stale browse continuation) ──
        try:
            from ..product_discovery_gate import try_types_overview_decision  # noqa: PLC0415

            _types_dec = try_types_overview_decision(ctx)
            if _types_dec is not None:
                return _types_dec
        except Exception as _types_exc:  # noqa: BLE001
            logger.debug(
                "[TYPES_OVERVIEW] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _types_exc,
            )

        # ── 0a.55 Short transactional continuation (product focus) ─────────
        try:
            from ..commerce.conversational_priority import (  # noqa: PLC0415
                try_short_continuation_decision,
            )
            _short_dec = try_short_continuation_decision(
                ctx, route="decision_engine",
            )
            if _short_dec is not None:
                return _short_dec
        except Exception as _short_exc:  # noqa: BLE001  # noqa: silent-ok — short continuation probe must not block decide
            logger.debug(
                "[SHORT_CONTINUATION] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None), _short_exc,
            )

        # ── 0a.6 Active-order fulfillment / location update (May 2026) ─────
        # Maps links + "أبغى الطلبية تجي الموقع ذا" during checkout must
        # attach to order_prep — never fall through to catalog search.
        try:
            from ..order_context_gate import (  # noqa: PLC0415
                log_order_context_block,
                try_order_context_update_decision,
            )
            _order_ctx_decision = try_order_context_update_decision(ctx)
            if _order_ctx_decision is not None:
                log_order_context_block(
                    tenant_id=getattr(ctx, "tenant_id", None),
                    reason=str(_order_ctx_decision.reason or "fulfillment_update"),
                    preview=(ctx.message or "")[:80],
                )
                return _order_ctx_decision
        except Exception as _oc_exc:  # noqa: BLE001  # noqa: silent-ok — order context gate must not block decide
            logger.debug(
                "[ORDER_CONTEXT_GATE] update routing skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _oc_exc,
            )

        # ── 0a.615 Active checkout amount (before DB track_order) ───────
        try:
            from ..commerce.current_order_amount import (  # noqa: PLC0415
                current_order_amount_facts_dict,
                resolve_current_order_amount,
                should_route_current_order_amount_over_tracking,
            )

            _inbound_meta = {}
            try:
                _prof = getattr(ctx, "profile", None) or {}
                if isinstance(_prof, dict):
                    _inbound_meta = dict(_prof.get("inbound_metadata") or {})
            except Exception:  # noqa: BLE001
                _inbound_meta = {}

            if should_route_current_order_amount_over_tracking(
                ctx.message or "",
                state=getattr(ctx, "state", None),
                inbound_metadata=_inbound_meta,
            ):
                _amount_snap = resolve_current_order_amount(
                    state=getattr(ctx, "state", None),
                    inbound_metadata=_inbound_meta,
                )
                logger.info(
                    "[CURRENT_ORDER_AMOUNT] route=llm tenant=%s total=%s source=%s preview=%r",
                    getattr(ctx, "tenant_id", None),
                    _amount_snap.total_amount,
                    _amount_snap.source,
                    (ctx.message or "")[:60],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "current_order_amount",
                        "current_order_amount_facts": current_order_amount_facts_dict(
                            _amount_snap,
                        ),
                        "response_goal": (
                            "Answer the customer's question about the total/value of "
                            "their CURRENT in-progress WhatsApp order using only "
                            "current_order_amount_facts. Do not claim no orders exist."
                        ),
                    },
                    reason="active checkout amount question — skip DB track_order",
                    confidence=0.96,
                )
        except Exception as _coa_exc:  # noqa: BLE001  # noqa: silent-ok — amount guard must not block decide
            logger.debug(
                "[CURRENT_ORDER_AMOUNT] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _coa_exc,
            )

        # ── 0a.62 Existing-order tracking guard (Phase 2) ─────────────────
        try:
            from ..commerce.order_tracking_intent_guard import (  # noqa: PLC0415
                is_explicit_order_tracking_request,
            )

            _track_inbound_meta = {}
            try:
                _tp = getattr(ctx, "profile", None) or {}
                if isinstance(_tp, dict):
                    _track_inbound_meta = dict(_tp.get("inbound_metadata") or {})
            except Exception:  # noqa: BLE001
                _track_inbound_meta = {}

            if (
                is_explicit_order_tracking_request(
                    ctx.message or "",
                    state=getattr(ctx, "state", None),
                    history=getattr(ctx, "history", None),
                    commerce_bundle=getattr(ctx, "commerce_bundle", None),
                    inbound_metadata=_track_inbound_meta,
                )
                and intent.name != INTENT_TRACK_ORDER
            ):
                logger.info(
                    "[ORDER_TRACKING_GUARD] override intent=%s → track_order preview=%r",
                    intent.name,
                    (ctx.message or "")[:60],
                )
                return Decision(
                    action=ACTION_TRACK_ORDER,
                    args={"order_id": (intent.slots or {}).get("order_id", "")},
                    reason="order_tracking_guard — existing-order follow-up",
                    confidence=0.97,
                )
        except Exception as _otg_exc:  # noqa: BLE001  # noqa: silent-ok — guard must not block decide
            logger.debug(
                "[ORDER_TRACKING_GUARD] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _otg_exc,
            )

        # ── 0a.55 Price objection / explicit branch location ───────────────
        try:
            from ..state.price_objection_topic import (  # noqa: PLC0415
                build_price_objection_facts,
                detect_price_objection_topic_shift,
            )

            if detect_price_objection_topic_shift(ctx.message or ""):
                _po_facts = build_price_objection_facts(ctx.message or "")
                logger.info(
                    "[PRICE_OBJECTION] tenant=%s route=llm preview=%r",
                    ctx.tenant_id,
                    (ctx.message or "")[:80],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "price_objection",
                        "price_objection_facts": _po_facts,
                        "response_goal": (
                            "Answer the customer's price or competitor comparison "
                            "objection. Briefly explain quality/value, and offer to "
                            "check wholesale quantity pricing or available offers. "
                            "Do not ask for quantity or push checkout unless the "
                            "customer explicitly requests to buy now."
                        ),
                    },
                    reason="price_objection — commerce answer before generic ack",
                    confidence=0.93,
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — price objection gate must not block decide
            pass

        try:
            from ..commerce.link_intent import LinkIntentType, resolve_link_intent
            from ..execution.faq import TOPIC_LOCATION, TOPIC_STORE_INFO

            _link_intent = resolve_link_intent(ctx.message or "")
            if _link_intent == LinkIntentType.WEBSITE_URL:
                logger.info(
                    "[LINK_INTENT] tenant=%s route=store_info",
                    ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_FAQ_REPLY,
                    args={"topic": TOPIC_STORE_INFO},
                    reason="customer asked for online store / website URL",
                    confidence=0.94,
                )

            _maps_url = str(getattr(facts, "maps_url", "") or "").strip()
            if (
                _link_intent == LinkIntentType.PHYSICAL_LOCATION
                and _maps_url
            ):
                logger.info(
                    "[LINK_INTENT] tenant=%s route=faq_location maps=1",
                    ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_FAQ_REPLY,
                    args={"topic": TOPIC_LOCATION},
                    reason=(
                        "explicit branch location request — configured maps URL"
                    ),
                    confidence=0.94,
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — link intent gate must not block decide
            pass

        # ── 0a.565 Identity / collaboration guard (Jun 2026) ─────────────
        # Self-intro or collaboration inbounds must not assume purchase intent.
        try:
            from ..commerce.identity_collaboration_guard import (  # noqa: PLC0415
                try_identity_collaboration_decision,
            )

            _id_collab_dec = try_identity_collaboration_decision(
                ctx, route="decision_engine",
            )
            if _id_collab_dec is not None:
                return _id_collab_dec
        except Exception as _id_collab_exc:  # noqa: BLE001  # noqa: silent-ok — guard must not block decide
            logger.debug(
                "[IDENTITY_COLLABORATION_GUARD] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _id_collab_exc,
            )

        # ── 0a.57 Absence of positive commerce signal (Jun 2026) ─────────
        # When classifiers miss and no commerce/fulfillment signal is
        # present, route to generative non-sales compose — not the default
        # MerchantBrain sales frame. Runs after short-continuation and
        # order-context gates so checkout acks are never swallowed.
        try:
            from ..commerce.conversational_priority import (  # noqa: PLC0415
                try_absence_non_sales_decision,
            )
            _absence_dec = try_absence_non_sales_decision(
                ctx, route="decision_engine",
            )
            if _absence_dec is not None:
                return _absence_dec
        except Exception as _abs_exc:  # noqa: BLE001  # noqa: silent-ok — absence gate must not block decide
            logger.debug(
                "[ABSENCE_COMMERCE_GATE] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _abs_exc,
            )

        # ── 0b. Platform / SaaS inquiry (May 2026 #4) ───────────────────────
        # Customer is asking about Nahla (the platform) itself —
        # subscription, API, dashboard, Meta linking, campaigns.
        # We MUST NOT search the merchant's catalogue for "اشتراك"
        # / "API" — the legacy ASK_PRODUCT regex used to do exactly
        # that and the brain would improvise wrong answers (the May
        # 2026 voice-note incident). Route to the platform-reply
        # template which scopes the conversation back to the merchant
        # WITHOUT inventing platform facts.
        if intent.name == INTENT_PLATFORM_INQUIRY:
            topic = str((intent.slots or {}).get("platform_topic") or "general_platform")
            logger.info(
                "[PLATFORM_ROUTE] tenant=%s topic=%s preview=%r",
                getattr(ctx, "tenant_id", None), topic, (ctx.message or "")[:60],
            )
            return Decision(
                action=ACTION_PLATFORM_REPLY,
                args={"platform_topic": topic},
                reason=f"platform inquiry ({topic})",
                confidence=intent.confidence,
            )

        # ── 0. Product correction / information (before checkout continuation) ─
        try:
            from ..state.product_correction import parse_product_correction  # noqa: PLC0415

            _pc = parse_product_correction(ctx.message or "")
            if _pc.detected:
                if _pc.replacement_query:
                    logger.info(
                        "[PRODUCT_CORRECTION] tenant=%s re-search replacement=%r",
                        ctx.tenant_id,
                        _pc.replacement_query[:80],
                    )
                    return Decision(
                        action=ACTION_SEARCH_PRODUCTS,
                        args={
                            "query": _pc.replacement_query,
                            "source": "product_correction",
                        },
                        reason="product_correction: replacement catalog search",
                        confidence=0.94,
                    )
                logger.info(
                    "[PRODUCT_CORRECTION] tenant=%s negation without replacement",
                    ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "product_correction",
                        "response_goal": (
                            "The customer rejected the previously assumed product. "
                            "Do not mention the old product. Ask which product they mean."
                        ),
                    },
                    reason="product_correction: stale product rejected",
                    confidence=0.93,
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            from ..state.product_information_topic import (  # noqa: PLC0415
                TOPIC_PRODUCT_ATTRIBUTE_INFORMATION,
                detect_product_information_topic_shift,
                product_information_blocks_checkout,
                resolve_product_information_llm_topic,
            )

            if (
                detect_product_information_topic_shift(ctx.message or "")
                or product_information_blocks_checkout(ctx)
            ):
                _info_topic = resolve_product_information_llm_topic(ctx.message or "")
                _response_goal = (
                    "Answer the customer's product attribute, processing, composition, "
                    "or ingredient question before continuing checkout."
                    if _info_topic == TOPIC_PRODUCT_ATTRIBUTE_INFORMATION
                    else (
                        "Answer the customer's product usage, dosage, ingredients, "
                        "benefits, or suitability question before continuing checkout."
                    )
                )
                logger.info(
                    "[PRODUCT_INFORMATION] tenant=%s topic=%s answer before checkout preview=%r",
                    ctx.tenant_id,
                    _info_topic,
                    (ctx.message or "")[:80],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": _info_topic,
                        "response_goal": _response_goal,
                        "suppress_checkout": True,
                    },
                    reason="product_information_topic_shift — answer before checkout",
                    confidence=0.92,
                )
        except Exception:  # noqa: BLE001
            pass

        # ── 0b. Deterministic checkout continuation (highest priority) ────────
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
            "اكمل", "أكمل", "كمل", "كمّل", "نعم", "موافق", "موافقه", "حسنا", "حسناً",
            "حسن", "صح", "صحيح", "شوف", "ابدأ", "إبدأ", "سر", "سري",
            "قدّم", "قدم", "ارسل", "أرسل", "تقدم", "تقدّم", "أتمم",
            "وافق", "أوافق", "أوافقك", "رائع", "ممتاز", "انشئ", "أنشئ",
            "go", "ok", "okay", "yes", "confirm", "proceed", "sure",
        })
        _msg_lower  = (ctx.message or "").strip().lower()
        _msg_words  = set(_msg_lower.split())
        _is_confirm = bool(_msg_words & _CONFIRM_KEYWORDS)
        _has_prep   = bool(getattr(state, "order_prep", None))
        try:
            from ..order_context_gate import has_explicit_commerce_topic_change  # noqa: PLC0415

            _explicit_commerce_switch = has_explicit_commerce_topic_change(ctx.message or "")
        except Exception:  # noqa: BLE001
            _explicit_commerce_switch = False

        if (
            state.stage in (STAGE_ORDERING, STAGE_DECIDING)
            and state.current_product_focus
            and facts.orderable
            and not state.checkout_url
            and (_is_confirm or _has_prep)
            and intent.name not in (INTENT_TALK_HUMAN, INTENT_TRACK_ORDER)
            and not _explicit_commerce_switch
            and not _checkout_topic_blocks()
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
        # The customer explicitly asked for a human agent. We honour that
        # ALWAYS — even mid-order. Production feedback showed merchants
        # losing trust when the brain kept "helping" a customer who had
        # already typed "حولني لموظف" or "أبي مختص يكلمني". An active
        # order_prep no longer blocks the handoff; the order data stays
        # in state and the merchant can resume the sale manually, or the
        # brain picks it up on the next inbound. We capture the active-
        # order signal in ``args`` so the handoff session creation path
        # in the webhook can log it (useful for audit / "this customer
        # bailed mid-cart" support tickets).
        if intent.name == INTENT_TALK_HUMAN:
            from ..intent.service_availability_gate import (  # noqa: PLC0415
                is_service_availability_inquiry,
            )
            if is_service_availability_inquiry(ctx.message or ""):
                logger.info(
                    "[HANDOFF] service availability inquiry — not handoff | "
                    "tenant=%s preview=%r",
                    getattr(ctx, "tenant_id", "?"),
                    (ctx.message or "")[:80],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"policy_reason": "service_availability_not_handoff"},
                    reason=(
                        "service availability inquiry — route to LLM, "
                        "not ACTION_HANDOFF"
                    ),
                )

            # ``order_prep`` is a default-initialised dataclass on every
            # MerchantConversationState, so it's always truthy. We need
            # to ask whether it carries actual order data (a product
            # has been pinned) before considering this "mid-order".
            _op = getattr(state, "order_prep", None)
            _has_active_order = bool(
                getattr(state, "current_product_focus", None)
                or (_op is not None and (
                    getattr(_op, "product_id", "") or ""
                    or bool(getattr(_op, "missing_fields", None))
                    or getattr(_op, "awaiting_payment_receipt", False)
                ))
            )
            if _has_active_order:
                logger.info(
                    "[HANDOFF] customer requested human DURING active order — "
                    "honouring handoff (no longer blocked by order_prep) | "
                    "intent=%s tenant=%s",
                    intent.name, getattr(ctx, "tenant_id", "?"),
                )
            return Decision(
                action=ACTION_HANDOFF,
                args={"during_active_order": _has_active_order} if _has_active_order else {},
                reason="customer requested human agent",
            )

        # ── 1.5 Future transfer promise (awaiting receipt) ───────────────
        # Customer promised to transfer soon while we already asked for
        # receipt proof. Route deterministically — do NOT fall through to
        # intent=general → ACTION_LLM_REPLY.
        _op_pay = getattr(state, "order_prep", None)
        if _op_pay is not None and getattr(_op_pay, "awaiting_payment_receipt", False):
            try:
                from core.payment_intent import (  # noqa: PLC0415
                    detect_future_transfer_promise_text,
                )
                if detect_future_transfer_promise_text(ctx.message or ""):
                    logger.info(
                        "[PAYMENT_TRANSFER_PROMISE] deterministic ack | "
                        "tenant=%s preview=%r",
                        ctx.tenant_id,
                        (ctx.message or "")[:60],
                    )
                    return Decision(
                        action=ACTION_PAYMENT_TRANSFER_PROMISE,
                        args={},
                        reason=(
                            "awaiting_payment_receipt + future transfer "
                            "promise — deterministic ack"
                        ),
                        confidence=0.96,
                    )
            except Exception as _ftp_exc:  # noqa: BLE001  # noqa: silent-ok — payment promise check best-effort
                logger.debug(
                    "[PAYMENT_TRANSFER_PROMISE] check skipped tenant=%s err=%s",
                    ctx.tenant_id, _ftp_exc,
                )

        # ── 1.8 Post-order tracking-link guard (May 2026) ───────────────
        # After an order is confirmed, bare "الرابط" / "ارسل الرابط"
        # usually means tracking follow-up — not store URL / checkout.
        # When no tracking URL exists yet, route to the LLM with a strict
        # response_goal so the customer always gets a natural reply.
        try:
            from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: PLC0415
            from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                should_use_generative_tracking_follow_up,
            )
            _bundle = getattr(ctx, "commerce_bundle", None) or {}
            if should_use_generative_tracking_follow_up(
                ctx.message or "",
                history=ctx.history,
                state=state,
                commerce_bundle=_bundle,
            ):
                _track_args = prepare_tracking_follow_up_decision(ctx)
                logger.info(
                    "[TRACKING_LINK_GUARD] generative follow-up | tenant=%s "
                    "preview=%r order_status=%r",
                    ctx.tenant_id,
                    (ctx.message or "")[:60],
                    _track_args.get("order_status", ""),
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args=_track_args,
                    reason=(
                        "post-order tracking/shipping link follow-up — "
                        "generative reply (no tracking URL yet); do not "
                        "restart checkout or send store URL"
                    ),
                    confidence=0.93,
                )
        except Exception as _tlg_exc:  # noqa: BLE001  # noqa: silent-ok — tracking follow-up guard best-effort
            logger.debug(
                "[TRACKING_LINK_GUARD] skipped tenant=%s err=%s",
                ctx.tenant_id, _tlg_exc,
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
            try:
                from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: PLC0415
                from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                    should_use_generative_tracking_follow_up,
                )
                _bundle = getattr(ctx, "commerce_bundle", None) or {}
                if should_use_generative_tracking_follow_up(
                    ctx.message or "",
                    history=ctx.history,
                    state=state,
                    commerce_bundle=_bundle,
                ):
                    return Decision(
                        action=ACTION_LLM_REPLY,
                        args=prepare_tracking_follow_up_decision(ctx),
                        reason=(
                            "track_order intent but customer asked for a "
                            "future tracking link — generative follow-up"
                        ),
                        confidence=0.93,
                    )
            except Exception:  # noqa: BLE001
                pass
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
            INTENT_ONLINE_STORE_INQUIRY,
            INTENT_ASK_LOCATION, INTENT_ASK_OWNER_CONTACT, INTENT_ASK_PAYMENT_INFO,
        ):
            if _block_stale_resume("pending_candidates"):
                _candidates = []
            _matched_product = _match_product_from_message(ctx.message, _candidates) if _candidates else None
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
                    from ..catalog.catalog_ranking_runtime import resolve_orderable_alternatives  # noqa: PLC0415

                    _alts = resolve_orderable_alternatives(
                        getattr(ctx, "_db", None),
                        ctx.tenant_id,
                        source_product_id=_matched_product.get("id"),
                        fallback_candidates=_candidates,
                        limit=3,
                    )
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
                if _prod_orderable and _matched_product.get("external_id"):
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
            # GUARD: if product options are pending, a numeric pick is an
            # OPTION selection (e.g. "1" for المقاس, "2" for اللون), NOT a
            # product selection. Route to ACTION_PROPOSE_DRAFT_ORDER so
            # orders.py._merge_message_options handles it correctly.
            _pending_opts = list(getattr(state, "pending_option_groups", None) or [])
            if _pending_opts and state.current_product_focus and facts.orderable:
                try:
                    from ..catalog.numeric_ownership import (  # noqa: PLC0415
                        NUMERIC_OWNER_ORDER_OPTIONS,
                        log_numeric_ownership,
                    )

                    log_numeric_ownership(
                        ctx,
                        numeric_owner=NUMERIC_OWNER_ORDER_OPTIONS,
                        action="order_option_pick",
                        intent_name=intent.name,
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry optional
                    pass
                logger.info(
                    "[ORDER FLOW] numeric pick → option selection (not product pick) | "
                    "pending_options=%s product=%r",
                    _pending_opts, (state.current_product_focus or {}).get("title"),
                )
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": state.current_product_focus},
                    reason=f"numeric pick while options pending {_pending_opts} — treat as option selection",
                    confidence=0.96,
                )

            # CRITICAL: only fall back to last_recommended_products when
            # last_search_candidates is empty AND we have NO active list
            # context. Mixing the two lists causes the customer to see
            # "1. بنطلون" (from search_candidates) but get "بلوزة" (from
            # last_recommended_products) at index 0.
            try:
                from ..catalog.numeric_ownership import (  # noqa: PLC0415
                    group_products_candidate_list,
                    is_group_products_navigation_source,
                    log_numeric_ownership,
                    resolve_numeric_owner,
                )

                if is_group_products_navigation_source(state):
                    candidates = group_products_candidate_list(state)
                    _candidate_source = "catalog_navigation_group_products"
                else:
                    _search_cands = list(state.last_search_candidates or [])
                    _rec_cands = list(state.last_recommended_products or [])
                    candidates = _search_cands or _rec_cands
                    _candidate_source = (
                        "last_search_candidates" if _search_cands
                        else ("last_recommended_products" if _rec_cands else "none")
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — ownership helpers optional
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
                        from ..catalog.catalog_ranking_runtime import resolve_orderable_alternatives  # noqa: PLC0415

                        _alts = resolve_orderable_alternatives(
                            getattr(ctx, "_db", None),
                            ctx.tenant_id,
                            source_product_id=product.get("id"),
                            fallback_candidates=candidates,
                            limit=3,
                        )
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
                    _can_start_order = _prod_orderable and (
                        bool(product.get("external_id"))
                        or _candidate_source == "last_recommended_products"
                    )
                    if _can_start_order:
                        try:
                            from ..catalog.numeric_ownership import log_numeric_ownership, resolve_numeric_owner  # noqa: PLC0415

                            log_numeric_ownership(
                                ctx,
                                numeric_owner=resolve_numeric_owner(ctx, intent_name=intent.name),
                                action="list_pick",
                                intent_name=intent.name,
                                candidate_source=_candidate_source,
                                extra={"list_index": idx},
                            )
                        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry optional
                            pass
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
                from ..commerce.product_ordering_prompt import build_ordering_clarify_args  # noqa: PLC0415

                return Decision(
                    action=ACTION_CLARIFY,
                    args=build_ordering_clarify_args(ctx),
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
            # No candidates remembered → show top products so the customer
            # can pick by number from a fresh list. Never show a bare
            # "ما المنتج؟" clarification when we can show real options.
            if facts.has_products:
                if _product_discovery_blocked("top_products_numeric_fallback"):
                    _fb = _fulfillment_locked_fallback()
                    if _fb is not None:
                        return _fb
                    from ..product_discovery_gate import clarify_instead_of_top_products  # noqa: PLC0415
                    return clarify_instead_of_top_products(
                        ctx, reason="weak_or_unknown_intent",
                    )
                logger.info(
                    "[ORDER FLOW] numeric_pick_no_candidates → showing_top_products "
                    "tenant=%s intent=%s",
                    ctx.tenant_id, intent.name,
                )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": "", "source": "top_products_numeric_fallback"},
                    reason="numeric pick with no candidate list — show top products",
                    confidence=0.80,
                )
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
        #
        # ANTI-REGRESSION — Conversation Commerce State Tracking:
        # If the customer is mid-order (we have a product_id / city /
        # name already on order_prep), an address signal here is the
        # NEXT slot in the funnel — NOT a pre-product stash. We MUST
        # NOT send "قبل ما نكمّل، اختر المنتج اللي تبغاه" in that case;
        # the merchant flagged that exact regression. Fall through and
        # let the continuation block (section 3.7 / safety net) handle
        # it as ACTION_PROPOSE_DRAFT_ORDER.
        _has_address_signal = any(
            (intent.slots.get(k) or "").strip()
            for k in ("short_address_code", "google_maps_url", "location_url")
        )
        _op_active = getattr(state, "order_prep", None)
        _mid_order_funnel = bool(
            _op_active
            and (
                getattr(_op_active, "product_id", "")
                or getattr(_op_active, "city", "")
                or getattr(_op_active, "customer_first_name", "")
                or getattr(_op_active, "short_address_code", "")
                or getattr(_op_active, "google_maps_url", "")
            )
        )
        if (
            _has_address_signal
            and not state.current_product_focus
            and not _mid_order_funnel
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
        try:
            from ..catalog.numeric_ownership import (  # noqa: PLC0415
                group_products_candidate_list,
                is_group_products_navigation_source,
            )

            if is_group_products_navigation_source(state):
                _active_candidates = group_products_candidate_list(state)
            else:
                _active_candidates = (
                    list(state.last_search_candidates or [])
                    or list(state.last_recommended_products or [])
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — ownership helpers optional
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

        try:
            from ..commerce.product_breadth_policy import (  # noqa: PLC0415
                global_availability_browse_requested,
            )

            _is_global_browse = global_availability_browse_requested(
                ctx.message or "",
            )
        except Exception:  # noqa: BLE001
            _is_global_browse = False

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
            and not _explicit_commerce_switch
            and not _is_global_browse
            and not _checkout_topic_blocks()
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

        # ── 3.7y Conversational priority before text-pattern fallbacks ─────
        try:
            from ..commerce.conversational_priority import (  # noqa: PLC0415
                try_priority_before_suppression,
            )
            _prio_dec = try_priority_before_suppression(
                ctx, history=list(ctx.history or []), route="pre_text_patterns",
            )
            if _prio_dec is not None:
                return _prio_dec
        except Exception:  # noqa: BLE001
            pass

        # ── 3.8 Text-pattern rules (message-level, intent-agnostic) ─────────
        # Discovery browse / start-order / show-more → unified entry (3.8u).
        # Replay list patterns remain here until Phase 2.
        _msg_norm = _normalize_ar(ctx.message or "")

        _REPEAT_PATTERNS = [
            "مره ثانيه", "مره اخرى", "مرة اخرى", "مرة ثانية",
            "كرر", "اعد", "اعيد", "وريني الخيارات",
            "وريني تاني", "وريني ثاني", "ارسل مره", "ارسل تاني",
            "repeat", "show again", "list again",
        ]
        _is_repeat_req = any(p in _msg_norm for p in _REPEAT_PATTERNS)

        # ── 3.8c Extract product name from order phrases ──────────────────
        from ..discovery.entry import extract_order_product_query  # noqa: PLC0415

        _extracted_product_query = extract_order_product_query(ctx)

        # ── 3.8d Product visual / image request (before order-query hijack) ─
        if intent.name == INTENT_PRODUCT_VISUAL_REQUEST:
            from ..commerce.product_visual import (  # noqa: PLC0415
                extract_visual_product_query,
                is_deictic_visual_request,
            )

            if _is_commerce_blocked(ctx):
                return Decision(
                    action=ACTION_SOCIAL_REPLY,
                    args={
                        "social_category": "religious_media",
                        "block_commerce_escalation": True,
                    },
                    reason="non-commerce block overrides product_visual on social OCR",
                    confidence=0.94,
                )
            focus = state.current_product_focus or {}
            focus_title = str(focus.get("title") or "").strip()
            query = (
                extract_visual_product_query(ctx.message or "")
                or str(intent.slots.get("product_query") or "").strip()
                or str(intent.slots.get("product_name") or "").strip()
            )
            if is_deictic_visual_request(ctx.message or ""):
                from ..commerce.product_visual import (  # noqa: PLC0415
                    resolve_trusted_focus_for_deictic,
                )
                trusted = resolve_trusted_focus_for_deictic(state, ctx.message or "")
                if trusted.title:
                    focus_title = trusted.title
            if focus_title:
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "product_visual",
                        "focus_product": focus_title,
                        "product_query": focus_title,
                        "response_goal": "send_product_visual",
                    },
                    reason="customer wants product image — focused SKU",
                    confidence=0.92,
                )
            if query:
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query, "after_search": "product_visual"},
                    reason=f"customer wants image of {query!r}",
                    confidence=0.90,
                )
            if is_deictic_visual_request(ctx.message or ""):
                from ..commerce.product_visual import (  # noqa: PLC0415
                    resolve_trusted_focus_for_deictic,
                )
                trusted = resolve_trusted_focus_for_deictic(state, ctx.message or "")
                if not trusted.title:
                    return Decision(
                        action=ACTION_CLARIFY,
                        args={
                            "topic": "product_visual",
                            "question": "أي منتج تقصد صورته؟",
                        },
                        reason=trusted.reason or "deictic visual ask without trusted focus",
                        confidence=0.88,
                    )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "product_visual",
                        "focus_product": trusted.title,
                        "product_query": trusted.title,
                        "response_goal": "send_product_visual",
                    },
                    reason=f"deictic visual → trusted focus ({trusted.reason})",
                    confidence=0.90,
                )
            return Decision(
                action=ACTION_CLARIFY,
                args={
                    "topic": "product_visual",
                    "question": "أي منتج تبغى صورته؟",
                },
                reason="product visual ask without resolved SKU",
                confidence=0.85,
            )

        # ── 3.8u Unified discovery entry (Phase 1) ────────────────────────
        from ..discovery.entry import (  # noqa: PLC0415
            resolve_discovery_entry,
            route_discovery_entry,
        )

        _discovery_entry = resolve_discovery_entry(ctx)
        if _discovery_entry.matched:
            _discovery_decision = route_discovery_entry(
                ctx,
                _discovery_entry,
                facts=facts,
                product_discovery_blocked=_product_discovery_blocked,
                fulfillment_locked_fallback=_fulfillment_locked_fallback,
                block_stale_resume=_block_stale_resume,
                is_commerce_blocked=_is_commerce_blocked,
            )
            if _discovery_decision is not None:
                return _discovery_decision

        # ── 3.8c handler — verbatim repeat of last list ───────────────────
        if (
            _is_repeat_req
            and facts.has_products
            and not _is_commerce_blocked(ctx)
            and not _product_discovery_blocked("replay")
            and not _block_stale_resume("product_replay")
        ):
            _last_cands = list(state.last_search_candidates or [])
            if _last_cands:
                logger.info(
                    "[ORDER FLOW] replaying_last_candidates | count=%d tenant=%s",
                    len(_last_cands), ctx.tenant_id,
                )
                # Re-emit the last search result so the pipeline re-saves it
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={
                        "query": "",
                        "source": "replay",
                        "replay_candidates": _last_cands,
                    },
                    reason="text-pattern: replay last product list",
                    confidence=0.90,
                )
            logger.info(
                "[ORDER FLOW] replaying_last_candidates | count=0 → clarify "
                "tenant=%s",
                ctx.tenant_id,
            )
            from ..product_discovery_gate import clarify_instead_of_top_products  # noqa: PLC0415
            return clarify_instead_of_top_products(
                ctx, reason="weak_or_unknown_intent",
            )

        # ── 3.8b½ Goal-based commerce (before order-query text hijack) ───
        _goal_dec = _goal_based_commerce_decision(ctx)
        if _goal_dec is not None:
            return _goal_dec

        # ── 3.9: Refine existing search by price ──────────────────────────
        # When the customer has already seen a product list (last_search_candidates
        # is not empty) and their new message is a price-based refinement
        # ("أرخص", "أقل من 100 ريال", "أغلى"), filter in-memory and emit
        # ACTION_NARROW directly — no round-trip to the catalog needed.
        _PRICE_REFINE_RE = re.compile(
            r"أرخص|رخيص[ةه]?\b|أقل\s*(من|سعر)|بأقل|سعر?\s*أقل"
            r"|أغلى|أعلى\s*(سعر|ثمن)|أجود|أفضل\s*(سعر|قيمة)",
            re.UNICODE,
        )
        _last_cands: list = list(getattr(state, "last_search_candidates", None) or [])
        if (
            _last_cands
            and facts.has_products
            and intent.name in (INTENT_GENERAL, INTENT_ASK_PRODUCT, INTENT_ASK_PRICE)
            and _PRICE_REFINE_RE.search(ctx.message or "")
            and not _product_discovery_blocked("price_refine")
        ):
            _price_max: float | None = None
            _num_match = re.search(r"(\d[\d,\.]{0,7})", ctx.message or "")
            if _num_match:
                try:
                    _price_max = float(_num_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            _want_cheaper = bool(re.search(r"أرخص|رخيص|أقل|بأقل", ctx.message or "", re.UNICODE))
            if _price_max:
                _refined = [p for p in _last_cands if float(p.get("price") or 0) <= _price_max]
            elif _want_cheaper:
                _refined = sorted(_last_cands, key=lambda p: float(p.get("price") or 0))
            else:
                _refined = sorted(_last_cands, key=lambda p: float(p.get("price") or 0), reverse=True)

            if _refined:
                logger.info(
                    "[ORDER FLOW] intent_rule_matched | rule=narrow_by_price "
                    "original=%d refined=%d price_max=%s want_cheaper=%s tenant=%s",
                    len(_last_cands), len(_refined), _price_max, _want_cheaper, ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_NARROW,
                    args={"products": _refined},
                    reason="customer filtered existing product list by price — narrowing in-memory",
                    confidence=0.87,
                )

        # ── 4. Simple FAQ / identity / shipping / contact ──────────────────
        if intent.name == INTENT_WHO_ARE_YOU:
            logger.info(
                "[PERSONA_IDENTITY] route=persona_identity intent=%s tenant=%s preview=%r",
                intent.name,
                getattr(ctx, "tenant_id", None),
                (ctx.message or "")[:60],
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "persona_identity",
                    "block_commerce_escalation": True,
                },
                reason="identity probe — thin persona compose",
                confidence=intent.confidence,
            )

        if intent.name == INTENT_COMPLAINT_REFUND:
            from ..commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                try_complaint_refund_decision,
            )

            _complaint_route = try_complaint_refund_decision(ctx)
            if _complaint_route is not None:
                return _complaint_route

        if intent.name == INTENT_ASK_SHIPPING:
            # ── Shipping intent → ALWAYS the brain (June 2026) ───────────
            # The static ``faq_shipping()`` template ("بالنسبة للشحن: …
            # أقدر أتحقق لك من خيارات الشحن المتاحة بعد اختيار المنتج
            # المناسب") was producing unnatural answers even on the
            # simplest questions ("تتوصلون للقصيم؟", "كم مدة الشحن؟",
            # "بكم الشحن؟"). The merchant explicitly asked us to remove
            # the canned template entirely and route every shipping
            # question to the brain so the AI writes the reply itself
            # using the full conversation + store-knowledge context.
            #
            # ``faq_shipping`` is now a ROUTING HINT, not an outbound
            # template: we set ``topic_hint="shipping"`` on the LLM
            # decision so observers (logging, telemetry, future
            # orchestration) still see what the rule classifier matched,
            # but the customer-facing copy is composed by the brain.
            #
            # Post-order context (paid/processing/shipped order, or
            # product-in-focus + known city) gets a sharper hint so the
            # brain frames the reply as order tracking rather than a
            # shipping-policy answer — without injecting any new copy.
            _op = getattr(state, "order_prep", None)
            _post_order = bool(
                getattr(_op, "payment_receipt_received", False)
                or str(getattr(_op, "order_status", "") or "").lower()
                in (
                    "under_review", "processing", "preparing",
                    "ready", "shipped", "in_transit", "out_for_delivery",
                    "delivered", "payment_pending",
                )
                or bool(getattr(state, "current_product_focus", None))
                and bool(getattr(_op, "city", None))
            )
            if _post_order:
                logger.info(
                    "[SHIPPING_INTENT] post-order context — defer to brain "
                    "| tenant=%s payment_receipt=%s order_status=%r",
                    ctx.tenant_id,
                    bool(getattr(_op, "payment_receipt_received", False)),
                    getattr(_op, "order_status", ""),
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={
                        "topic": "shipping_post_order",
                        "topic_hint": "shipping",
                        "intent_hint": "order_tracking",
                    },
                    reason=(
                        "ASK_SHIPPING matched, paid/processing/shipped "
                        "order present — defer to brain with order context"
                    ),
                )
            logger.info(
                "[SHIPPING_INTENT] pre-order — defer to brain "
                "(faq_shipping template disabled) | tenant=%s",
                ctx.tenant_id,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={"topic_hint": "shipping"},
                reason=(
                    "customer asked about shipping / delivery — let the "
                    "brain compose the reply (faq_shipping template "
                    "disabled June 2026)"
                ),
            )

        if intent.name in {INTENT_ASK_STORE_INFO, INTENT_ONLINE_STORE_INQUIRY}:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "store_info"},
                reason="customer asked for the e-commerce store link",
            )

        if intent.name == INTENT_ASK_LOCATION:
            try:
                from ..order_context_gate import (  # noqa: PLC0415
                    has_active_order_context,
                    try_order_context_update_decision,
                )
                if has_active_order_context(ctx):
                    _loc_update = try_order_context_update_decision(ctx)
                    if _loc_update is not None:
                        return _loc_update
            except Exception:  # noqa: BLE001
                pass
            _maps_url = str(getattr(facts, "maps_url", "") or "").strip()
            if _maps_url:
                from ..execution.faq import TOPIC_LOCATION  # noqa: PLC0415

                logger.info(
                    "[LOCATION_INTENT] faq_location with configured maps | tenant=%s",
                    ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_FAQ_REPLY,
                    args={"topic": TOPIC_LOCATION},
                    reason=(
                        "customer asked for physical shop / Google Maps location "
                        "— send configured branch maps URL"
                    ),
                    confidence=0.94,
                )
            # Physical-shop / branch questions defer to the brain for
            # natural prose. The post-compose ``apply_location_safety_net``
            # still injects the maps URL + CTA button — same contract as
            # shipping (template disabled, wire layer owns the asset).
            logger.info(
                "[LOCATION_INTENT] defer to brain (location_delivery) | tenant=%s",
                ctx.tenant_id,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "location_delivery",
                    "topic_hint": "location",
                },
                reason=(
                    "customer asked for the physical shop / Google Maps "
                    "location — defer to brain (location_delivery)"
                ),
            )

        if intent.name == INTENT_ASK_COD:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "cash_on_delivery"},
                reason=(
                    "customer asked about cash on delivery — "
                    "answer from tenant payment policy evidence"
                ),
            )

        if intent.name == INTENT_ASK_PAYMENT_INFO:
            from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
                PAYMENT_BARCODE_IMAGE_REQUEST,
                is_payment_barcode_image_request,
            )
            _barcode_image = is_payment_barcode_image_request(ctx.message)
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": (
                        "payment_barcode_image"
                        if _barcode_image
                        else "payment_info"
                    ),
                    "payment_request_kind": (
                        PAYMENT_BARCODE_IMAGE_REQUEST
                        if _barcode_image
                        else "ask_payment_info"
                    ),
                },
                reason=(
                    "customer asked for payment barcode/QR image — "
                    "outbound media first"
                    if _barcode_image else
                    "customer asked for bank/IBAN/barcode — let GPT attach "
                    "matching media library item"
                ),
            )

        if intent.name == INTENT_ASK_OWNER_CONTACT:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "owner_contact"},
                reason="customer asked for contact details",
            )

        # ── 5. Greeting (explicit greeting or first-turn generic help) ─────
        # Three hard rules to prevent the "bot keeps re-greeting mid-order"
        # bug while still acknowledging a returning customer politely:
        #   a) NEVER greet if the customer is in a committed sales stage
        #      (deciding/ordering/checkout). The continuation block above
        #      already routes those messages back into the order flow.
        #   b) NEVER send the FULL onboarding greeting twice in the same
        #      conversation. Once `greeted` is true, the long welcome
        #      template is locked.
        #   c) DO acknowledge an EXPLICIT salaam/hello/marhaba even when
        #      `greeted=True` — but with a short, warm re-greeting (no
        #      bullet list, no re-onboarding). This is the case the user
        #      reported: the bot received "السلام عليكم" after sending
        #      automation messages and silently fell through to the LLM
        #      fallback, which made it feel like the bot ignored them.
        _greet_locked = state.stage in (
            STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT,
        )
        if not _greet_locked:
            # ── Embedded-greeting escape hatch (May 2026 #19) ───────────
            # The rules layer marks ``slots["embedded_greeting"]=True``
            # when the salaam was just a wrapper around a real question
            # (handled in rules.py via the welcome gate + residue test).
            # In that case the ACTION_GREET short-circuit below would
            # render a canned welcome card and silently drop the
            # customer's actual ask — exactly the regression the merchant
            # reported on "مساء الخير نحلة وش نشاطهم". Skip the greet
            # branch so the rest of the engine routes the actionable
            # half to ACTION_LLM_REPLY (or a more specific action when
            # one matches). The pipeline's welcome-gate step then
            # prepends a brief warm acknowledgement to the LLM reply
            # so the salaam is still honoured.
            _intent_slots = getattr(intent, "slots", None) or {}
            _embedded_greeting = bool(_intent_slots.get("embedded_greeting"))
            if not _embedded_greeting:
                # ── Direct Answer First (DAF) bypass ─────────────────────
                # Both first-turn greet branches below short-circuit the
                # rest of the engine and emit a canned welcome card. That
                # is the right call ONLY when the customer's message is
                # genuinely thin (pure salaam, "هلا", "اي", "ok"). When
                # the message carries a real question — most often a
                # voice transcript, OCR caption, or reply-to-status
                # preamble — we MUST let it flow through to the LLM
                # fallback so the brain answers the actual ask. Without
                # this guard a customer who opens with a 30-second voice
                # note about an invoice gets met with "أنا نحلة مستشارة
                # المبيعات…" and feels ignored. See module-level comment
                # for the full background.
                _daf_bypass = _first_turn_has_actionable_substance(ctx.message)
                if intent.name == INTENT_GREETING and not state.greeted:
                    if _daf_bypass:
                        logger.info(
                            "[DAF.BYPASS] first_turn_greeting_with_substance "
                            "tenant=%s extraction=%s preview=%r",
                            getattr(ctx, "tenant_id", None),
                            getattr(intent, "extraction_method", "?"),
                            (ctx.message or "")[:80],
                        )
                    else:
                        from ..cost.intent_cost_policy import (  # noqa: PLC0415
                            should_use_template_for_pure_greeting,
                        )
                        from ..persona_expression import (  # noqa: PLC0415
                            PERSONA_KIND_GREETING,
                            PERSONA_TOPIC_SOCIAL,
                            is_established_greet_persona_compose_enabled,
                        )

                        if should_use_template_for_pure_greeting(
                            intent_name=intent.name,
                            embedded_greeting=False,
                            has_actionable_substance=False,
                        ):
                            logger.info(
                                "[INTENT_COST] kind=greeting route=template "
                                "first_turn tenant=%s preview=%r",
                                getattr(ctx, "tenant_id", None),
                                (ctx.message or "")[:60],
                            )
                            return Decision(
                                action=ACTION_GREET,
                                reason=(
                                    "first-turn pure greeting — template "
                                    "(intent cost policy, no LLM)"
                                ),
                                confidence=0.85,
                            )
                        if is_established_greet_persona_compose_enabled():
                            logger.info(
                                "[PERSONA_SOCIAL] kind=greeting route=first_turn_greeting "
                                "tenant=%s preview=%r",
                                getattr(ctx, "tenant_id", None),
                                (ctx.message or "")[:60],
                            )
                            return Decision(
                                action=ACTION_LLM_REPLY,
                                args={
                                    "topic": PERSONA_TOPIC_SOCIAL,
                                    "persona_kind": PERSONA_KIND_GREETING,
                                    "block_commerce_escalation": True,
                                },
                                reason=(
                                    "first-turn pure greeting — persona_social "
                                    "compose (persona_kind=greeting)"
                                ),
                                confidence=0.85,
                            )
                        return Decision(
                            action=ACTION_GREET,
                            reason="explicit greeting on first turn",
                        )
                if not state.greeted and intent.name == INTENT_GENERAL:
                    if _daf_bypass:
                        logger.info(
                            "[DAF.BYPASS] first_turn_general_actionable "
                            "tenant=%s extraction=%s preview=%r",
                            getattr(ctx, "tenant_id", None),
                            getattr(intent, "extraction_method", "?"),
                            (ctx.message or "")[:80],
                        )
                    else:
                        return Decision(
                            action=ACTION_GREET,
                            reason="first-turn general help",
                        )
            if (
                intent.name == INTENT_GREETING
                and state.greeted
                and not bool(_intent_slots.get("embedded_greeting"))
            ):
                # Established pure greeting → template re-greet when intent
                # cost policy avoids LLM; persona_social compose only when
                # ``NAHLA_ROUTINE_LLM_AVOID_ENABLED=false`` and persona flag on.
                # Mixed turns (``embedded_greeting=True``) fall through so the
                # actionable half reaches a specific or general LLM route.
                from ..cost.intent_cost_policy import (  # noqa: PLC0415
                    should_use_template_for_pure_greeting,
                )
                from ..persona_expression import (  # noqa: PLC0415
                    PERSONA_KIND_GREETING,
                    PERSONA_TOPIC_SOCIAL,
                    is_established_greet_persona_compose_enabled,
                )

                if should_use_template_for_pure_greeting(
                    intent_name=intent.name,
                    embedded_greeting=False,
                    has_actionable_substance=False,
                ):
                    logger.info(
                        "[INTENT_COST] kind=greeting route=template "
                        "re_greet tenant=%s preview=%r",
                        getattr(ctx, "tenant_id", None),
                        (ctx.message or "")[:60],
                    )
                    return Decision(
                        action=ACTION_GREET,
                        args={"re_greet": True},
                        reason=(
                            "established pure greeting — template re-greet "
                            "(intent cost policy, no LLM)"
                        ),
                        confidence=0.85,
                    )
                if is_established_greet_persona_compose_enabled():
                    logger.info(
                        "[PERSONA_SOCIAL] kind=greeting route=established_greeting "
                        "tenant=%s preview=%r",
                        getattr(ctx, "tenant_id", None),
                        (ctx.message or "")[:60],
                    )
                    return Decision(
                        action=ACTION_LLM_REPLY,
                        args={
                            "topic": PERSONA_TOPIC_SOCIAL,
                            "persona_kind": PERSONA_KIND_GREETING,
                            "block_commerce_escalation": True,
                        },
                        reason=(
                            "established pure greeting — persona_social "
                            "compose (persona_kind=greeting)"
                        ),
                        confidence=0.85,
                    )
                logger.info(
                    "[PERSONA_SOCIAL] kind=greeting route=legacy_re_greet_template "
                    "tenant=%s flag_off=1",
                    getattr(ctx, "tenant_id", None),
                )
                return Decision(
                    action=ACTION_GREET,
                    args={"re_greet": True},
                    reason=(
                        "explicit greeting after greeted=True — legacy "
                        "re-greeting template (persona compose disabled)"
                    ),
                    confidence=0.85,
                )

        # ── 6.5 Need-based advisory product guidance ───────────────────
        if intent.name in {
            INTENT_NEED_BASED_PRODUCT_ADVICE,
            "need_based_product_advice",
        }:
            _goal_dec = _goal_based_commerce_decision(ctx)
            if _goal_dec is not None:
                return _goal_dec

            _need_cat = str(
                (intent.slots or {}).get("solution_axis")
                or (intent.slots or {}).get("need_category")
                or ""
            )
            if not _need_cat:
                try:
                    from ..commerce.solution_seeking import (  # noqa: PLC0415
                        classify_solution_seeking_commerce,
                    )
                    _nb = classify_solution_seeking_commerce(ctx.message or "")
                    if _nb is not None:
                        _need_cat = _nb.axis
                except Exception:  # noqa: BLE001  # noqa: silent-ok — optional solution-seeking classifier fallback
                    pass
            if not _need_cat:
                _need_cat = "general_attribute"
            _src = "intent_slot" if (intent.slots or {}).get("solution_axis") else "classifier"
            try:
                from ..commerce.solution_seeking import log_solution_seeking_commerce  # noqa: PLC0415
                log_solution_seeking_commerce(
                    tenant_id=ctx.tenant_id,
                    axis=_need_cat,
                    source=_src,
                    route="decision_engine",
                    preview=ctx.message or "",
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — solution-seeking telemetry must not block reply
                pass
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "solution_seeking_commerce",
                    "need_category": _need_cat,
                    "solution_axis": _need_cat,
                },
                reason="solution-seeking commerce — advisory LLM, no SKU clarify",
                confidence=0.93,
            )

        # ── 6. Start order — product in focus ──────────────────────────────
        if intent.name == INTENT_START_ORDER:
            from ..commerce.start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415

            _bare_start_order = is_bare_start_order_phrase(ctx.message or "")
            if _bare_start_order:
                try:
                    from ..order_context_gate import is_fulfillment_session_locked  # noqa: PLC0415

                    if is_fulfillment_session_locked(ctx):
                        from ..commerce.conversation_context_reset import (  # noqa: PLC0415
                            clear_active_order_context,
                        )

                        clear_active_order_context(
                            state,
                            reason="fresh_start_order_opener",
                        )
                        logger.info(
                            "[ORDER FLOW] fresh start-order cleared stale focus tenant=%s preview=%r",
                            ctx.tenant_id,
                            (ctx.message or "")[:80],
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[ORDER FLOW] fresh start-order clear failed tenant=%s",
                        ctx.tenant_id,
                    )
                try:
                    from ..commerce.checkout_route_owner import (  # noqa: PLC0415
                        should_route_bare_start_to_channel_selection,
                    )

                    if should_route_bare_start_to_channel_selection(
                        order_prep=getattr(state, "order_prep", None),
                        store_url=str(getattr(facts, "store_url", "") or ""),
                        maps_url=str(getattr(facts, "maps_url", "") or ""),
                    ):
                        return Decision(
                            action=ACTION_LLM_REPLY,
                            args={
                                "topic": "purchase_channel_selection",
                                "response_goal": "help_customer_choose_purchase_channel",
                            },
                            reason=(
                                "bare start-order — purchase channel not chosen yet"
                            ),
                            confidence=0.92,
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[ORDER FLOW] purchase channel selection gate skipped tenant=%s",
                        ctx.tenant_id,
                    )

            if (
                not _bare_start_order
                and state.current_product_focus
                and facts.has_products
            ):
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
                # Prefer the slot-extracted query; fall back to text extraction
                query = (
                    intent.slots.get("product_query", "").strip()
                    or intent.slots.get("product_name", "").strip()
                    or _extracted_product_query
                )
                if not query:
                    from ..discovery.entry import (  # noqa: PLC0415
                        START_ORDER_BARE,
                        resolve_discovery_entry,
                        route_discovery_entry,
                    )

                    _so_entry = resolve_discovery_entry(ctx)
                    if _so_entry.matched and _so_entry.entry_type == START_ORDER_BARE:
                        _so_dec = route_discovery_entry(
                            ctx,
                            _so_entry,
                            facts=facts,
                            product_discovery_blocked=_product_discovery_blocked,
                            fulfillment_locked_fallback=_fulfillment_locked_fallback,
                            block_stale_resume=_block_stale_resume,
                            is_commerce_blocked=_is_commerce_blocked,
                        )
                        if _so_dec is not None:
                            return _so_dec
                    if _product_discovery_blocked("top_products_start_order"):
                        _fb = _fulfillment_locked_fallback()
                        if _fb is not None:
                            return _fb
                        from ..product_discovery_gate import clarify_instead_of_top_products  # noqa: PLC0415
                        return clarify_instead_of_top_products(
                            ctx, reason="weak_or_unknown_intent",
                        )
                    logger.info(
                        "[ORDER FLOW] intent_rule_matched | rule=top_products "
                        "reason=start_order_no_query tenant=%s",
                        ctx.tenant_id,
                    )
                    return Decision(
                        action=ACTION_SEARCH_PRODUCTS,
                        args={"query": "", "source": "top_products_start_order"},
                        reason="start_order with no product query — show top products",
                        confidence=0.85,
                    )
                if _product_discovery_blocked():
                    _fb = _fulfillment_locked_fallback()
                    if _fb is not None:
                        return _fb
                logger.info(
                    "[ORDER FLOW] intent_rule_matched | rule=order_product_query "
                    "query=%r tenant=%s",
                    query, ctx.tenant_id,
                )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query, "after_search": "propose_order"},
                    reason="customer wants to buy but no product focus — search first",
                    confidence=0.80,
                )

        # ── 7. Ask about product or price ─────────────────────────────────
        if intent.name in (INTENT_ASK_PRODUCT, INTENT_ASK_PRICE):
            from ..product_discovery_gate import (  # noqa: PLC0415
                clarify_instead_of_top_products,
                try_price_query_decision,
                _resolved_product_query,
            )
            if _is_commerce_blocked(ctx):
                return Decision(
                    action=ACTION_SOCIAL_REPLY,
                    args={
                        "social_category": "religious_media",
                        "block_commerce_escalation": True,
                    },
                    reason="non-commerce block overrides ask_product/price on social OCR",
                    confidence=0.94,
                )
            if _product_discovery_blocked("ask_product"):
                _fb = _fulfillment_locked_fallback()
                if _fb is not None:
                    return _fb
            from ..product_discovery_gate import try_price_query_decision  # noqa: PLC0415
            _price_dec = try_price_query_decision(
                ctx, extracted_product_query=_extracted_product_query,
            )
            if _price_dec is not None:
                return _price_dec
            if facts.has_products:
                query = _resolved_product_query(
                    ctx, _extracted_product_query,
                )
                if not query:
                    from ..product_discovery_gate import extract_inquiry_product_query  # noqa: PLC0415
                    query = extract_inquiry_product_query(ctx.message or "")
                if not query:
                    return clarify_instead_of_top_products(
                        ctx, reason="weak_or_unknown_intent",
                    )
                from ..product_discovery_gate import (  # noqa: PLC0415
                    classify_product_inquiry_route,
                    log_inquiry_class,
                    try_broad_category_inquiry_decision,
                )
                _inquiry_class, _inquiry_route = classify_product_inquiry_route(
                    ctx, query=query,
                )
                log_inquiry_class(
                    tenant_id=ctx.tenant_id,
                    inquiry_class=_inquiry_class,
                    route=_inquiry_route,
                    query=query,
                    preview=(ctx.message or "")[:80],
                )
                _broad_dec = try_broad_category_inquiry_decision(
                    ctx,
                    query=query,
                    inquiry_class=_inquiry_class,
                    route=_inquiry_route,
                )
                if _broad_dec is not None:
                    return _broad_dec
                if _product_discovery_blocked("ask_product"):
                    _fb = _fulfillment_locked_fallback()
                    if _fb is not None:
                        return _fb
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
            and not _product_discovery_blocked("order_product_query")
            and not _block_stale_resume("addon_recommendation")
        ):
            return Decision(
                action=ACTION_RECOMMEND_ADDON,
                args={"query": state.current_product_focus.get("category", "")},
                reason="customer close to purchase with recommendations available",
                confidence=0.68,
            )

        # ── 8.6 Hard-only out-of-scope guard (May 2026 #3 — KB-first) ─────
        #
        # History of this rule (three revisions, each fixing the
        # previous one):
        #
        #   #1  Routed long INTENT_GENERAL to ACTION_WEB_SEARCH when
        #       the catalogue was empty. Leaked DuckDuckGo result
        #       dumps into customer threads after "ايهما حساب كهرباء
        #       الشقة". Catastrophic.
        #
        #   #2  Replaced web_search with a 3-tier classifier
        #       (chitchat / safe_fact / hard) that ALWAYS intercepted
        #       INTENT_GENERAL and emitted a canned playful template.
        #       Over-corrected — short-circuited the merchant brain
        #       for legitimate honey-adjacent questions ("حبة البركة"،
        #       "السعال"، "كيف أسوّق للعسل")، ignored the KB and the
        #       catalogue, and made Nahla feel like a clownish
        #       guardrail bot instead of a smart sales assistant.
        #
        #   #3  (this revision) — Only TIER_HARD intercepts. Everything
        #       else falls through to ACTION_LLM_REPLY (the default tail
        #       at the bottom of decide()), where the merchant brain
        #       composes a natural reply WITH access to:
        #         * the merchant's product catalogue
        #         * the merchant's KB / knowledge base
        #         * the sales_context (recommendations, policies,
        #           pricing rules, FAQ topics)
        #         * the customer history
        #       The web_search tool is still gated off at the tool
        #       level (MERCHANT_EXTERNAL_RESEARCH_ENABLED=false by
        #       default) and the outbound sanitizer still scrubs any
        #       URL leak, so this is safe.
        #
        # The classifier ``classify_out_of_scope_tier`` is now hard-
        # biased: TIER_HARD only fires for unambiguous off-domain
        # keywords (electricity bills, real estate, programming,
        # legal cases, financial investing, drug dosages, war). For
        # ALL other INTENT_GENERAL inputs it returns TIER_PASSTHROUGH
        # and we do NOT short-circuit here — we let the rule chain
        # continue and the LLM fallback at the bottom of decide()
        # handle the reply.
        if intent.name == INTENT_GENERAL:
            from modules.ai.tools.web_search import external_research_enabled  # noqa: PLC0415
            from .scope_tiers import (  # noqa: PLC0415
                TIER_HARD,
                classify_out_of_scope_tier,
            )

            # Legacy opt-in research path — preserved behind the env
            # switch for the one beta tenant that needed it. Default
            # tenants never see this branch because
            # external_research_enabled() returns False unless ops
            # explicitly set MERCHANT_EXTERNAL_RESEARCH_ENABLED=true.
            if (
                external_research_enabled()
                and ctx.sales_context
                and not ctx.facts.has_products
                and len(ctx.message.split()) >= 4
            ):
                logger.info(
                    "[GENERAL_WEB_ALLOWED] tenant=%s reason=research_enabled_no_catalogue",
                    getattr(ctx, "tenant_id", None),
                )
                return Decision(
                    action=ACTION_WEB_SEARCH,
                    args={"query": ctx.message},
                    reason="general knowledge question with weak store context",
                    confidence=0.55,
                )

            tier = classify_out_of_scope_tier(ctx.message or "")
            if tier == TIER_HARD:
                logger.info(
                    "[OUT_OF_SCOPE_BLOCK] tenant=%s intent=%s tier=hard preview=%r",
                    getattr(ctx, "tenant_id", None),
                    (ctx.message or "")[:60],
                )
                return Decision(
                    action=ACTION_OUT_OF_SCOPE,
                    args={"tier": "hard", "message": ctx.message or ""},
                    reason="hard out_of_scope keyword match",
                    confidence=0.95,
                )
            # PASSTHROUGH — let the merchant brain handle it with full
            # KB + catalogue + sales_context. We deliberately do NOT
            # return here.
            logger.info(
                "[OUT_OF_SCOPE_PASSTHROUGH] tenant=%s intent=%s preview=%r — "
                "deferring to LLM brain with full KB context",
                getattr(ctx, "tenant_id", None),
                intent.name,
                (ctx.message or "")[:60],
            )

        # ── 9.5 Ordering-stage safety net ────────────────────────────────
        # NEVER let a message reach the LLM when the customer is actively
        # placing an order.  If all the specific rules above failed to match,
        # we have a product in focus → continue collecting checkout slots.
        # This is the last line of defence before LLM fallback.
        try:
            from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: PLC0415
            from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                should_use_generative_tracking_follow_up,
            )
            _bundle = getattr(ctx, "commerce_bundle", None) or {}
            if should_use_generative_tracking_follow_up(
                ctx.message or "",
                history=ctx.history,
                state=state,
                commerce_bundle=_bundle,
            ):
                logger.info(
                    "[ORDER FLOW] suppress ordering safety net for post-order "
                    "tracking follow-up | tenant=%s preview=%r",
                    ctx.tenant_id,
                    (ctx.message or "")[:60],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args=prepare_tracking_follow_up_decision(ctx),
                    reason=(
                        "ordering_stage_safety_net bypassed — active order "
                        "exists and customer asked about tracking/shipping link"
                    ),
                    confidence=0.88,
                )
        except Exception:  # noqa: BLE001
            pass

        if state.stage in (STAGE_ORDERING, STAGE_DECIDING):
            if (
                state.current_product_focus
                and facts.orderable
                and not state.checkout_url
                and not _is_global_browse
                and not _checkout_topic_blocks()
            ):
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
            # Product focus was lost but order_prep still remembers which
            # product the customer was buying — recover it from cached
            # candidates / recommendations so the funnel never resets.
            # This closes the merchant-reported regression where a customer
            # who already sent name/city/address heard "اختر المنتج اللي
            # تبغاه" because focus had been wiped by a side-effect.
            _op = getattr(state, "order_prep", None)
            _prep_product_id = str(getattr(_op, "product_id", "") or "").strip()
            _prep_has_progress = bool(
                _op
                and (
                    getattr(_op, "city", "")
                    or getattr(_op, "customer_first_name", "")
                    or getattr(_op, "short_address_code", "")
                    or getattr(_op, "google_maps_url", "")
                    or _prep_product_id
                )
            )
            if (
                not state.current_product_focus
                and _prep_has_progress
                and facts.orderable
            ):
                _recovered = _find_product_by_external_id(
                    _prep_product_id,
                    state.last_search_candidates or [],
                    state.last_recommended_products or [],
                )
                logger.warning(
                    "[ORDER FLOW] recovering lost product focus from order_prep | "
                    "tenant=%s prep_product_id=%r recovered=%r intent=%s",
                    ctx.tenant_id, _prep_product_id,
                    (_recovered or {}).get("title") if _recovered else None,
                    intent.name,
                )
                if _recovered:
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={
                            "product": _recovered,
                            "forced_product": _recovered,
                            "source": "order_prep_recovery",
                        },
                        reason=(
                            "ordering_stage_safety_net: focus was wiped but "
                            f"order_prep.product_id={_prep_product_id!r} — "
                            "recovered focus from candidate cache"
                        ),
                        confidence=0.88,
                    )
                if _prep_product_id:
                    _minimal_product = {"external_id": _prep_product_id}
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={
                            "product": _minimal_product,
                            "forced_product": _minimal_product,
                            "source": "order_prep_product_id_only",
                        },
                        reason=(
                            "ordering_stage_safety_net: focus lost but "
                            f"order_prep.product_id={_prep_product_id!r} preserved"
                        ),
                        confidence=0.85,
                    )
                # Even without a candidate match, the customer is mid-funnel.
                # Let the LLM compose a "we still have your details — which
                # product was it again?" reply WITH order_prep state visible,
                # rather than the cold "ما المنتج؟" template.
                logger.info(
                    "[ORDER FLOW] focus lost + prep present, no cached candidate to "
                    "recover from → routing to LLM with full order_prep context "
                    "tenant=%s prep_city=%r prep_short_code=%r",
                    ctx.tenant_id,
                    getattr(_op, "city", ""),
                    getattr(_op, "short_address_code", ""),
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"topic": "order_recovery"},
                    reason=(
                        "ordering_stage_safety_net: focus lost but order_prep "
                        "has live progress — let LLM keep the funnel alive"
                    ),
                    confidence=0.70,
                )
            # Product focus was lost (unsyncable product cleared it) and
            # NO order progress exists → fall back to searching.
            if (
                not state.current_product_focus
                and not _prep_product_id
                and facts.has_products
            ):
                if _product_discovery_blocked():
                    _fb = _fulfillment_locked_fallback()
                    if _fb is not None:
                        return _fb
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
                from ..commerce.product_ordering_prompt import build_ordering_clarify_args  # noqa: PLC0415

                return Decision(
                    action=ACTION_SEARCH_PRODUCTS if _query else ACTION_CLARIFY,
                    args=(
                        {"query": _query}
                        if _query
                        else build_ordering_clarify_args(ctx)
                    ),
                    reason="ordering_stage_safety_net: no product focus — ask customer to pick",
                    confidence=0.75,
                )

        # ── 9.4 Execute-pending-offer fallback (May 2026 #5) ─────────────
        # Production regression: after the bot offers something explicitly
        # ("تبين أرسل الرابط؟" / "تحب أرشّح لك العسل المناسب؟" / a product
        # card with implicit "أرسل لي الرابط؟"), the customer answers with
        # a bare confirmation ("اي" / "تمام" / "ي ريت" / "اوكي" / "👍").
        # The intent classifier returns INTENT_GENERAL (no pattern hits)
        # and we would otherwise fall through to a context-free
        # ACTION_LLM_REPLY whose response_goal says only "no rule matched
        # — LLM fallback". The LLM then often replies "أبشري" without
        # actually emitting a marker — the customer sees a verbal
        # acknowledgement but no link / image / card.
        #
        # This block intercepts that case: if the message IS a bare
        # confirmation AND the conversation carries a clear pending-offer
        # signal (last_question_asked / pending_action / product focus),
        # route to a TYPED LLM_REPLY whose ``decision.args`` carry the
        # context that the prompt-builder reads to construct a strict
        # execute-now goal. Confidence kept just below the higher-priority
        # rules so a future rule can still preempt this branch.
        _pending_offer_context = bool(
            (state.last_question_asked or "").strip()
            or (state.pending_action or "").strip()
            or state.current_product_focus
        )
        if _is_bare_confirmation and _pending_offer_context:
            _focus_title = (state.current_product_focus or {}).get("title") or ""
            logger.info(
                "[CTX_INHERIT] bare confirmation honours pending offer | "
                "tenant=%s last_q=%r pending=%r focus=%r preview=%r",
                getattr(ctx, "tenant_id", None),
                (state.last_question_asked or "")[:60],
                state.pending_action,
                _focus_title,
                _conf_msg[:40],
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "execute_pending_offer",
                    "last_question_asked": state.last_question_asked or "",
                    "pending_action": state.pending_action or "",
                    "focus_product": _focus_title,
                },
                reason=(
                    "bare-confirmation honours pending offer: "
                    f"last_q={(state.last_question_asked or '')[:40]!r} "
                    f"pending={state.pending_action!r}"
                ),
                confidence=0.78,
            )

        # ── 9. Fallback: LLM ─────────────────────────────────────────────
        if _is_commerce_blocked(ctx):
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "non_commerce_media",
                    "block_commerce_escalation": True,
                },
                reason="non-commerce media — LLM reply without catalog tools",
                confidence=0.88,
            )

        # Product media identity — OCR + vision + synced catalog (ownership ask)
        try:
            from ..commerce.product_media_identity_guard import (  # noqa: PLC0415
                try_product_media_identity_decision,
            )

            _pmi_dec = try_product_media_identity_decision(ctx)
            if _pmi_dec is not None:
                return _pmi_dec
        except Exception as _pmi_exc:  # noqa: BLE001  # noqa: silent-ok — guard must not block decide
            logger.debug(
                "[PRODUCT_MEDIA_IDENTITY] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _pmi_exc,
            )

        # P1-E: typed product-media goal (honey/process video, product info text)
        try:
            from ..commerce.product_media import (  # noqa: PLC0415
                build_product_media_decision_args,
                detect_product_media_turn,
            )
            _profile = getattr(ctx, "profile", None) or {}
            _in_meta = (
                _profile.get("inbound_metadata")
                if isinstance(_profile, dict) else None
            )
            _pm = detect_product_media_turn(
                ctx.message or "",
                inbound_metadata=_in_meta if isinstance(_in_meta, dict) else None,
                intent_name=intent.name,
                commerce_blocked=False,
            )
            if _pm.matched:
                _bundle = getattr(ctx, "commerce_bundle", None) or {}
                logger.info(
                    "[PRODUCT_MEDIA_ROUTE] tenant=%s vision=%s hint_only=%s "
                    "preview=%r",
                    getattr(ctx, "tenant_id", None),
                    _pm.has_vision_evidence,
                    _pm.has_hint_only,
                    (ctx.message or "")[:60],
                )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args=build_product_media_decision_args(
                        _pm,
                        commerce_bundle=_bundle if isinstance(_bundle, dict) else {},
                    ),
                    reason="product/inbound media — typed LLM goal",
                    confidence=0.86,
                )
        except Exception as _pm_exc:  # noqa: BLE001  # noqa: silent-ok — product media route best-effort
            logger.debug(
                "[PRODUCT_MEDIA_ROUTE] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None), _pm_exc,
            )

        return Decision(
            action=ACTION_LLM_REPLY,
            reason=f"no rule matched for intent={intent.name} — LLM fallback",
            confidence=0.50,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_product_by_external_id(
    external_id: str,
    *candidate_lists: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the first product across *candidate_lists* whose external_id
    matches ``external_id`` (case-insensitive), or ``None``.

    Used by the ordering-stage safety net to recover ``current_product_focus``
    from cached search / recommendation candidates when a side-effect in the
    pipeline wiped the focus mid-funnel. Without this recovery the customer
    would hear the cold "ما المنتج الذي تودّ طلبه؟" template even though we
    still hold their city / name / address in ``order_prep``.
    """
    needle = str(external_id or "").strip().lower()
    if not needle:
        return None
    for cands in candidate_lists:
        for prod in (cands or []):
            ext = str((prod or {}).get("external_id") or "").strip().lower()
            if ext and ext == needle:
                return prod
    return None


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
