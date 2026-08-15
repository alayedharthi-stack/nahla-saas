"""
brain/state/store.py
─────────────────────
DefaultStateStore: loads and persists MerchantConversationState.

Storage strategy:
  brain_state is stored in Conversation.extra_metadata['brain_state']
  under the key "brain_state".

  Lookup path (robust — doesn't rely on JSONB phone field):
    1. Find Customer row by (tenant_id, normalized_phone OR phone)
    2. Find latest Conversation by (tenant_id, customer_id)
    3. Deserialise extra_metadata['brain_state']

  This is more reliable than querying extra_metadata['phone'] because:
  - Customer lookup uses a proper indexed column
  - Conversation → Customer join is a FK-based query
  - Works even when extra_metadata lacks the 'phone' key

  If no Conversation exists yet (first message), returns a fresh
  MerchantConversationState. The save() call will write it once the
  Conversation row exists (created by _get_or_create_conversation in
  the webhook handler before brain.process() is called).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..types import (
    INTENT_GREETING,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_GENERAL,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from .stages import (
    STAGE_CHECKOUT,
    STAGE_DECIDING,
    STAGE_DISCOVERY,
    STAGE_EXPLORING,
    STAGE_ORDERING,
    STAGE_SUPPORT,
)

logger = logging.getLogger("nahla.brain.state_store")

_STATE_KEY = "brain_state"


def _find_customer(db: Any, tenant_id: int, phone: str):
    """
    Locate a Customer row for this (tenant, phone).

    The webhook hands us the raw ``to`` field straight from Meta (e.g.
    ``966555555555`` or ``+966555555555``), but :class:`Customer` rows are
    written by ``_get_or_create_customer`` with both ``phone`` and
    ``normalized_phone = E.164`` (see customer_intelligence.normalize_phone).
    Mismatching the format here means we fail the lookup, return a fresh
    state, and the bot loses turn-to-turn memory — the root cause of the
    "greeting repeats every message" symptom.

    Strategy: try every realistic shape of the phone, in order of how the
    DB actually stores it.

    Returns the Customer row plus a small diagnostics tuple consumed by
    ``DefaultStateStore.load`` for INFO-level audit logging — that lets
    operators confirm in production whether the second-turn lookup hit
    the same row as the first turn.
    """
    from database.models import Customer

    candidates: list[str] = []
    raw = (phone or "").strip()
    if raw:
        candidates.append(raw)

    try:  # noqa: PLC0415 — local import to avoid cycle on package init
        from services.customer_intelligence import normalize_phone as _normalize
        e164 = _normalize(raw) or ""
        if e164 and e164 not in candidates:
            candidates.append(e164)
    except Exception as _exc:
        logger.debug("[StateStore] phone normalize unavailable: %s", _exc)

    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits and digits not in candidates:
        candidates.append(digits)
    if digits and not digits.startswith("+"):
        plus = f"+{digits}"
        if plus not in candidates:
            candidates.append(plus)

    for candidate in candidates:
        customer = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.normalized_phone == candidate,
            )
            .first()
        )
        if customer:
            return customer, candidate, "normalized_phone", candidates

        customer = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.phone == candidate,
            )
            .first()
        )
        if customer:
            return customer, candidate, "phone", candidates

    return None, "", "", candidates


def _mask_phone(phone: str) -> str:
    """
    Mask a phone number for logging — keep country prefix and last 4 digits
    only, replace the middle with X. ``"+966555123456"`` → ``"+966XXXXXX3456"``.
    Never log full PII.
    """
    raw = (phone or "").strip()
    if not raw:
        return "<empty>"
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) <= 6:
        return raw[0] + "X" * (len(raw) - 1)
    prefix = "+" + digits[:3] if raw.startswith("+") or len(digits) >= 11 else digits[:2]
    suffix = digits[-4:]
    return f"{prefix}{'X' * (len(digits) - len(prefix.lstrip('+')) - 4)}{suffix}"


def _find_conversation(db: Any, tenant_id: int, customer_id: int):
    """Return the latest Conversation row for this customer."""
    from database.models import Conversation

    return (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.customer_id == customer_id,
        )
        .order_by(Conversation.id.desc())
        .first()
    )


class DefaultStateStore:
    """Implements StateStore protocol."""

    # ── Load ─────────────────────────────────────────────────────────────────

    def load(self, db: Any, tenant_id: int, customer_phone: str) -> MerchantConversationState:
        masked = _mask_phone(customer_phone)
        try:
            customer, matched_value, matched_column, tried = _find_customer(
                db, tenant_id, customer_phone
            )
            if not customer:
                # Single, parseable INFO line that answers exactly the
                # diagnostic question "did the second-turn lookup hit?"
                logger.info(
                    "[StateStore] turn_load tenant=%s phone=%s "
                    "customer_found=False candidates_tried=%d "
                    "matched_column=- state_source=fresh",
                    tenant_id, masked, len(tried),
                )
                return MerchantConversationState()

            conv = _find_conversation(db, tenant_id, customer.id)
            if not conv:
                logger.info(
                    "[StateStore] turn_load tenant=%s phone=%s "
                    "customer_found=True customer_id=%s matched_column=%s "
                    "conv_found=False state_source=fresh",
                    tenant_id, masked, customer.id, matched_column,
                )
                return MerchantConversationState()

            meta = conv.extra_metadata or {}
            raw  = meta.get(_STATE_KEY)
            if not raw:
                logger.info(
                    "[StateStore] turn_load tenant=%s phone=%s "
                    "customer_found=True customer_id=%s conv_id=%s "
                    "matched_column=%s state_source=fresh "
                    "reason=no_brain_state_in_metadata",
                    tenant_id, masked, customer.id, conv.id, matched_column,
                )
                return MerchantConversationState()

            state = MerchantConversationState.from_dict(raw)
            logger.info(
                "[StateStore] turn_load tenant=%s phone=%s "
                "customer_found=True customer_id=%s conv_id=%s "
                "matched_column=%s state_source=persisted "
                "stage=%s turn=%s greeted=%s focus=%s",
                tenant_id, masked, customer.id, conv.id, matched_column,
                state.stage, state.turn, state.greeted,
                bool(state.current_product_focus),
            )
            return state

        except Exception as exc:
            logger.warning(
                "[StateStore] turn_load tenant=%s phone=%s ERROR=%s "
                "state_source=fresh_fallback",
                tenant_id, masked, exc,
            )
            return MerchantConversationState()

    # ── Save ─────────────────────────────────────────────────────────────────

    def save(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
        state: MerchantConversationState,
    ) -> None:
        masked = _mask_phone(customer_phone)
        try:
            customer, _matched_value, matched_column, tried = _find_customer(
                db, tenant_id, customer_phone
            )
            if not customer:
                # If save can't find the customer, the next turn won't be
                # able to load state either — surface this loudly so we
                # catch it before it manifests as the "bot keeps greeting".
                logger.warning(
                    "[StateStore] turn_save tenant=%s phone=%s "
                    "customer_found=False candidates_tried=%d "
                    "result=skip reason=no_customer_row",
                    tenant_id, masked, len(tried),
                )
                return

            conv = _find_conversation(db, tenant_id, customer.id)
            if not conv:
                logger.warning(
                    "[StateStore] turn_save tenant=%s phone=%s "
                    "customer_found=True customer_id=%s conv_found=False "
                    "result=skip reason=no_conversation_row",
                    tenant_id, masked, customer.id,
                )
                return

            meta = dict(conv.extra_metadata or {})
            meta[_STATE_KEY] = state.to_dict()
            _bs = state.to_dict()
            _op = _bs.get("order_prep") or {}
            try:
                from core.active_order_context import maybe_persist_from_brain_save  # noqa: PLC0415

                maybe_persist_from_brain_save(
                    meta,
                    brain_state=_bs,
                    order_prep=_op,
                )
            except Exception as _aoc_exc:  # noqa: BLE001
                logger.warning(
                    "[ACTIVE_ORDER_CONTEXT] brain_save persist failed tenant=%s: %s",
                    tenant_id, _aoc_exc,
                )
            conv.extra_metadata = meta

            # JSONB without MutableDict.as_mutable() does NOT track in-place
            # dict mutations, and even attribute reassignment can be lost in
            # the presence of autoflush snapshots taken earlier in the same
            # request (a known SQLAlchemy + JSONB gotcha — see how
            # promotion_engine.py and coupon_generator.py handle the same
            # column). flag_modified guarantees the column is actually
            # written on the next flush, regardless of how the attribute
            # got there. Without this, brain_state is silently dropped on
            # the second turn and the bot re-greets — exactly the live
            # symptom we observed in production.
            try:
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                flag_modified(conv, "extra_metadata")
            except Exception as _exc:
                logger.debug("[StateStore] flag_modified unavailable: %s", _exc)

            # Phase 2 — draft/paid order bridge after brain state persist.
            try:
                from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415
                _bs = state.to_dict()
                sync_nahla_wa_order(
                    db,
                    tenant_id=int(tenant_id),
                    conversation=conv,
                    brain_state=_bs,
                    order_prep=_bs.get("order_prep") or {},
                    trigger="brain_save",
                    customer=customer,
                )
            except Exception as _bridge_exc:  # noqa: BLE001
                logger.warning(
                    "[NAHLA_ORDER_BRIDGE] brain_save hook failed tenant=%s conv=%s: %s",
                    tenant_id, getattr(conv, "id", None), _bridge_exc,
                )

            db.commit()

            # Verify the write actually landed. We refresh the row from DB
            # and re-read brain_state. This is cheap (a single SELECT) and
            # turns a silent persistence failure into a loud WARNING.
            persistence_verified = False
            try:
                db.refresh(conv, attribute_names=["extra_metadata"])
                fresh_meta = conv.extra_metadata or {}
                persistence_verified = bool(fresh_meta.get(_STATE_KEY))
            except Exception as _exc:
                logger.debug("[StateStore] post-commit verify failed: %s", _exc)

            if not persistence_verified:
                logger.warning(
                    "[StateStore] turn_save tenant=%s phone=%s "
                    "customer_id=%s conv_id=%s matched_column=%s "
                    "result=committed_but_unverified "
                    "reason=brain_state_not_present_after_commit "
                    "stage=%s turn=%s",
                    tenant_id, masked, customer.id, conv.id, matched_column,
                    state.stage, state.turn,
                )
            else:
                logger.info(
                    "[StateStore] turn_save tenant=%s phone=%s "
                    "customer_found=True customer_id=%s conv_id=%s "
                    "matched_column=%s result=ok verified=True "
                    "stage=%s turn=%s greeted=%s focus=%s",
                    tenant_id, masked, customer.id, conv.id, matched_column,
                    state.stage, state.turn, state.greeted,
                    bool(state.current_product_focus),
                )

        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.error(
                "[StateStore] turn_save tenant=%s phone=%s ERROR=%s",
                tenant_id, masked, exc,
            )

    # ── Mark greeted (used by proactive senders) ─────────────────────────────

    def mark_greeted(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
    ) -> bool:
        """
        Stamp `greeted=True` on the brain_state for this customer, idempotently.

        Used by every code path that sends an outbound message OUTSIDE the
        Brain pipeline (cart recovery taps, automation templates, manual
        agent replies). Without this, the next inbound from that customer
        loads `greeted=False`, the DecisionEngine routes to ACTION_GREET,
        and the bot re-introduces itself mid-conversation — the exact
        "تكرار الترحيب" symptom production was hitting.

        Returns True when the flag was stamped (or was already True),
        False when no Customer/Conversation row exists yet (in which case
        the next Brain turn will create them and persist a fresh state).
        """
        masked = _mask_phone(customer_phone)
        try:
            customer, _matched, matched_column, _tried = _find_customer(
                db, tenant_id, customer_phone
            )
            if not customer:
                logger.info(
                    "[StateStore] mark_greeted tenant=%s phone=%s "
                    "result=skip reason=no_customer_row",
                    tenant_id, masked,
                )
                return False

            conv = _find_conversation(db, tenant_id, customer.id)
            if not conv:
                logger.info(
                    "[StateStore] mark_greeted tenant=%s phone=%s "
                    "customer_id=%s result=skip reason=no_conversation_row",
                    tenant_id, masked, customer.id,
                )
                return False

            meta = dict(conv.extra_metadata or {})
            raw  = meta.get(_STATE_KEY)
            state = (
                MerchantConversationState.from_dict(raw)
                if raw else MerchantConversationState()
            )
            if state.greeted:
                return True

            state.greeted = True
            state.updated_at = datetime.now(timezone.utc).isoformat()
            meta[_STATE_KEY] = state.to_dict()
            conv.extra_metadata = meta
            try:
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                flag_modified(conv, "extra_metadata")
            except Exception:
                pass
            db.commit()
            logger.info(
                "[StateStore] mark_greeted tenant=%s phone=%s "
                "customer_id=%s conv_id=%s matched_column=%s result=ok",
                tenant_id, masked, customer.id, conv.id, matched_column,
            )
            return True
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "[StateStore] mark_greeted tenant=%s phone=%s ERROR=%s",
                tenant_id, masked, exc,
            )
            return False

    # ── Transition ────────────────────────────────────────────────────────────

    def transition(
        self,
        state: MerchantConversationState,
        intent: Intent,
        decision: Decision,
    ) -> MerchantConversationState:
        """Return a NEW state (immutable transition)."""
        from ..decision.actions import (
            ACTION_GREET,
            ACTION_FAQ_REPLY,
            ACTION_LLM_REPLY,
            ACTION_SEARCH_PRODUCTS,
            ACTION_PROPOSE_DRAFT_ORDER,
            ACTION_SEND_PAYMENT_LINK,
            ACTION_HANDOFF,
            ACTION_TRACK_ORDER,
            ACTION_CLARIFY,
            ACTION_NARROW,
        )
        from ..types import INTENT_PICK_LIST_ITEM as _PICK  # noqa: PLC0415

        s = MerchantConversationState(
            stage=state.stage,
            greeted=state.greeted,
            assistant_identity_introduced=getattr(
                state, "assistant_identity_introduced", False
            ),
            last_intent=intent.name,
            current_product_focus=state.current_product_focus,
            previous_product_focus=getattr(state, "previous_product_focus", None),
            suspended_product_focus=getattr(state, "suspended_product_focus", None),
            conversation_focus=str(getattr(state, "conversation_focus", "") or ""),
            draft_order_id=state.draft_order_id,
            checkout_url=state.checkout_url,
            customer_goal=state.customer_goal,
            last_question_asked=state.last_question_asked,
            last_question_answered=state.last_question_answered,
            recommended_next_step=state.recommended_next_step,
            order_prep=OrderPreparationState.from_dict(state.order_prep.to_dict()),
            turn=state.turn + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
            # Carry candidates forward so pipeline can clear them after pick
            last_search_candidates=list(state.last_search_candidates or []),
            recent_messages=list(state.recent_messages or []),
            conversation_summary=state.conversation_summary,
            cart_items=list(state.cart_items or []),
            selected_variant=state.selected_variant,
            payment_method=state.payment_method,
            pending_action=state.pending_action,
            last_recommended_products=list(state.last_recommended_products or []),
            last_presented_products=list(
                getattr(state, "last_presented_products", None) or []
            ),
            last_presented_collections=list(
                getattr(state, "last_presented_collections", None) or []
            ),
            last_presented_group_products=list(
                getattr(state, "last_presented_group_products", None) or []
            ),
            selected_product_id=str(getattr(state, "selected_product_id", "") or ""),
            selected_variant_id=str(getattr(state, "selected_variant_id", "") or ""),
            selected_collection=str(getattr(state, "selected_collection", "") or ""),
            selection_context_turn=int(
                getattr(state, "selection_context_turn", 0) or 0
            ),
            pending_short_address_code=getattr(state, "pending_short_address_code", "") or "",
            pending_google_maps_url=getattr(state, "pending_google_maps_url", "") or "",
            pending_city=getattr(state, "pending_city", "") or "",
            last_action=getattr(state, "last_action", "") or "",
            product_focus_turn=int(getattr(state, "product_focus_turn", 0) or 0),
            visual_focus_turn=int(getattr(state, "visual_focus_turn", 0) or 0),
            last_inbound_canonical=str(getattr(state, "last_inbound_canonical", "") or ""),
            last_inbound_canonical_turn=int(getattr(state, "last_inbound_canonical_turn", 0) or 0),
            recent_topic=str(getattr(state, "recent_topic", "") or ""),
            recent_topic_turn=int(getattr(state, "recent_topic_turn", 0) or 0),
            last_fallback_fingerprint=str(getattr(state, "last_fallback_fingerprint", "") or ""),
            last_fallback_turn=int(getattr(state, "last_fallback_turn", 0) or 0),
            # Increment general_streak when intent is GENERAL, reset otherwise.
            general_streak=(
                (getattr(state, "general_streak", 0) or 0) + 1
                if intent.name == INTENT_GENERAL
                else 0
            ),
            current_selected_options=dict(getattr(state, "current_selected_options", None) or {}),
            pending_option_groups=list(getattr(state, "pending_option_groups", None) or []),
            commerce_session=dict(getattr(state, "commerce_session", None) or {}),
        )

        action = decision.action

        # Stages that represent committed sales progress. Once we are in any
        # of these, only an explicit handoff or completion can move us out —
        # NOT a stray greeting/search detected mid-flow. This is the lock
        # that prevents the "bot resets to greeting after the customer sent
        # their name" symptom.
        _PROGRESS_STAGES = {STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT}

        if action == ACTION_GREET:
            s.greeted = True
            # Identity is introduced ONLY on the first full greeting
            # (variant 0/1/2 of greeting() — each one says "أنا نحلة"
            # or "أنا مساعد {store}"). A re-greeting (``re_greet=True``)
            # is a short "ياهلا 🌷" and does NOT touch identity, so we
            # must not flip the flag for those.
            re_greet = bool((decision.args or {}).get("re_greet"))
            if not re_greet:
                s.assistant_identity_introduced = True
            # Preserve any in-progress sales stage. The greeting itself is
            # also gated upstream in the decision engine, so reaching this
            # branch with a progress stage should be very rare — but if it
            # happens we MUST NOT downgrade the funnel.
            if state.stage not in _PROGRESS_STAGES:
                s.stage = STAGE_DISCOVERY

        elif action == ACTION_FAQ_REPLY:
            # When the customer explicitly asked the identity FAQ
            # (INTENT_WHO_ARE_YOU → topic="identity"), the bot just
            # said "أنا نحلة / أنا مساعد المتجر الذكي" — stamp the
            # flag so subsequent turns can stop re-introducing. We
            # look at the decision.args topic AND fall back to intent
            # so this still fires even if the topic wasn't passed
            # through explicitly (e.g. an LLM-routed identity reply).
            topic = str((decision.args or {}).get("topic") or "")
            if topic == "identity" or intent.name == "who_are_you":
                s.assistant_identity_introduced = True

        elif action == ACTION_LLM_REPLY:
            topic = str((decision.args or {}).get("topic") or "")
            persona_kind = str((decision.args or {}).get("persona_kind") or "")
            if topic == "persona_identity" or intent.name == "who_are_you":
                s.assistant_identity_introduced = True
            if topic == "persona_social" and persona_kind == "greeting":
                s.greeted = True

        elif action == ACTION_SEARCH_PRODUCTS:
            if intent.name == INTENT_ASK_PRODUCT:
                from ..commerce.commerce_focus_owner import archive_current_product_focus  # noqa: PLC0415

                archive_current_product_focus(s, reason="ask_product_search")
            # Browsing again is fine while exploring/discovery, but DON'T
            # demote a customer who is mid-checkout or mid-ordering.
            if state.stage not in {STAGE_ORDERING, STAGE_CHECKOUT}:
                s.stage = STAGE_EXPLORING

        elif action in (ACTION_CLARIFY, ACTION_NARROW):
            if state.stage not in _PROGRESS_STAGES:
                s.stage = STAGE_EXPLORING

        elif action == ACTION_PROPOSE_DRAFT_ORDER:
            # Distinguish "still gathering details" from "fully prepared":
            # if the order action returned needs_collection, we are deciding
            # on what to fill next, not yet finalising. Both mean the funnel
            # is committed; only checkout supersedes ordering downstream.
            s.stage = STAGE_ORDERING
            _product = decision.args.get("product") or {}
            if _product:
                from ..commerce.commerce_focus_owner import set_product_focus  # noqa: PLC0415

                set_product_focus(
                    s,
                    _product,
                    reason="propose_draft_order_transition",
                    turn=int(getattr(s, "turn", 0) or 0),
                )
                _ext_id = str(_product.get("external_id") or "").strip()
                if _ext_id:
                    s.order_prep.product_id = _ext_id

        elif action == ACTION_SEND_PAYMENT_LINK:
            s.stage = STAGE_CHECKOUT
            if decision.args.get("checkout_url"):
                s.checkout_url = decision.args["checkout_url"]
            if decision.args.get("draft_order_id"):
                s.draft_order_id = str(decision.args["draft_order_id"])

        elif action == ACTION_HANDOFF:
            s.stage = STAGE_SUPPORT

        return s
