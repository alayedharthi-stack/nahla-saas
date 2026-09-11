"""Pure final-wire checks for customer-facing outbound messages.

This module does not compose or rewrite customer prose.  It only answers
whether a structured commerce turn still has a substantive payload at the
last boundary before provider delivery.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Mapping, Sequence

from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_NARROW,
    ACTION_PRODUCT_MEDIA_IDENTITY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SELECT_PURCHASE_CHANNEL,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SUGGEST_COUPON,
    ACTION_VARIANT_PRICING,
)


_COMMERCE_DELIVERY_ACTIONS = frozenset(
    {
        ACTION_CATALOG_NAVIGATE,
        ACTION_CLARIFY,
        ACTION_CUSTOMER_COUPON_REQUEST,
        ACTION_NARROW,
        ACTION_PRODUCT_MEDIA_IDENTITY,
        ACTION_PROPOSE_DRAFT_ORDER,
        ACTION_RECOMMEND_ADDON,
        ACTION_SEARCH_PRODUCTS,
        ACTION_SELECT_PURCHASE_CHANNEL,
        ACTION_SEND_PAYMENT_LINK,
        ACTION_STASH_ADDRESS_PRE_PRODUCT,
        ACTION_SUGGEST_COUPON,
        ACTION_VARIANT_PRICING,
    }
)


def outbound_body_kind(text: object) -> str:
    """Classify final text without interpreting customer language or intent."""
    value = str(text or "").strip()
    if not value:
        return "empty"
    if any(unicodedata.category(ch)[0] in {"L", "N"} for ch in value):
        return "substantive"
    return "symbols_only"


def commerce_delivery_expected(
    *,
    decision_action: object = "",
    brain_action: object = "",
    brain_result: Mapping[str, Any] | None = None,
    had_product_delivery_candidate: bool = False,
) -> bool:
    """Use only structured Brain/dispatch facts to identify commerce delivery."""
    actions = {
        str(decision_action or "").strip(),
        str(brain_action or "").strip(),
    }
    if any(action in _COMMERCE_DELIVERY_ACTIONS for action in actions):
        return True
    if had_product_delivery_candidate:
        return True
    data = brain_result if isinstance(brain_result, Mapping) else {}
    presentation_kind = str(data.get("product_presentation_kind") or "").strip()
    return bool(
        (presentation_kind and presentation_kind != "none")
        or data.get("catalog_product_ids")
        or data.get("pending_product_card_count")
    )


def should_suppress_final_outbound(
    text: object,
    *,
    brain_buttons: Sequence[Any] | None = None,
    pending_attachments: Sequence[Any] | None = None,
    require_substantive_commerce_text: bool = False,
) -> bool:
    """Fail closed for empty payloads and symbol-only commerce remnants.

    A structured button or attachment is itself a useful delivery, so its
    technical body may remain minimal.  Outside commerce, this guard keeps the
    existing empty-only behavior and does not judge model style.
    """
    if brain_buttons or pending_attachments:
        return False
    kind = outbound_body_kind(text)
    if kind == "empty":
        return True
    return bool(require_substantive_commerce_text and kind == "symbols_only")


__all__ = [
    "commerce_delivery_expected",
    "outbound_body_kind",
    "should_suppress_final_outbound",
]
