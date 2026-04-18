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
    Decision,
    Intent,
    MerchantConversationState,
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
    Tries normalized_phone first (E.164), then raw phone column.
    """
    from database.models import Customer

    customer = (
        db.query(Customer)
        .filter(
            Customer.tenant_id == tenant_id,
            Customer.normalized_phone == phone,
        )
        .first()
    )
    if customer:
        return customer

    # Fallback to raw phone — may not be normalised
    return (
        db.query(Customer)
        .filter(
            Customer.tenant_id == tenant_id,
            Customer.phone == phone,
        )
        .first()
    )


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
        try:
            customer = _find_customer(db, tenant_id, customer_phone)
            if not customer:
                logger.debug(
                    "[StateStore] no customer for tenant=%s phone=%s — fresh state",
                    tenant_id, customer_phone,
                )
                return MerchantConversationState()

            conv = _find_conversation(db, tenant_id, customer.id)
            if not conv:
                logger.debug(
                    "[StateStore] no conversation for customer=%s — fresh state",
                    customer.id,
                )
                return MerchantConversationState()

            meta = conv.extra_metadata or {}
            raw  = meta.get(_STATE_KEY)
            if not raw:
                return MerchantConversationState()

            state = MerchantConversationState.from_dict(raw)
            logger.debug(
                "[StateStore] loaded state for tenant=%s customer=%s stage=%s turn=%s",
                tenant_id, customer.id, state.stage, state.turn,
            )
            return state

        except Exception as exc:
            logger.warning("[StateStore] load error: %s — returning fresh state", exc)
            return MerchantConversationState()

    # ── Save ─────────────────────────────────────────────────────────────────

    def save(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
        state: MerchantConversationState,
    ) -> None:
        try:
            customer = _find_customer(db, tenant_id, customer_phone)
            if not customer:
                logger.debug("[StateStore] save: no customer found — skip")
                return

            conv = _find_conversation(db, tenant_id, customer.id)
            if not conv:
                logger.debug("[StateStore] save: no conversation found — skip")
                return

            meta = dict(conv.extra_metadata or {})
            meta[_STATE_KEY] = state.to_dict()
            # Reassign whole dict so SQLAlchemy detects the JSONB mutation
            conv.extra_metadata = meta
            db.commit()

            logger.debug(
                "[StateStore] saved state for tenant=%s customer=%s stage=%s",
                tenant_id, customer.id, state.stage,
            )

        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.error("[StateStore] save error: %s", exc)

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
            ACTION_SEARCH_PRODUCTS,
            ACTION_PROPOSE_DRAFT_ORDER,
            ACTION_SEND_PAYMENT_LINK,
            ACTION_HANDOFF,
            ACTION_TRACK_ORDER,
            ACTION_CLARIFY,
            ACTION_NARROW,
        )

        s = MerchantConversationState(
            stage=state.stage,
            greeted=state.greeted,
            last_intent=intent.name,
            current_product_focus=state.current_product_focus,
            draft_order_id=state.draft_order_id,
            checkout_url=state.checkout_url,
            turn=state.turn + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        action = decision.action

        if action == ACTION_GREET:
            s.greeted = True
            s.stage   = STAGE_DISCOVERY

        elif action == ACTION_SEARCH_PRODUCTS:
            if intent.name == INTENT_ASK_PRODUCT:
                s.current_product_focus = None
            s.stage = STAGE_EXPLORING

        elif action in (ACTION_CLARIFY, ACTION_NARROW):
            s.stage = STAGE_EXPLORING

        elif action == ACTION_PROPOSE_DRAFT_ORDER:
            s.stage = STAGE_ORDERING
            if decision.args.get("product"):
                s.current_product_focus = decision.args["product"]

        elif action == ACTION_SEND_PAYMENT_LINK:
            s.stage = STAGE_CHECKOUT
            if decision.args.get("checkout_url"):
                s.checkout_url = decision.args["checkout_url"]
            if decision.args.get("draft_order_id"):
                s.draft_order_id = str(decision.args["draft_order_id"])

        elif action == ACTION_HANDOFF:
            s.stage = STAGE_SUPPORT

        return s
