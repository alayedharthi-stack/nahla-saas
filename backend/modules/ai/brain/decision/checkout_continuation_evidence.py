"""Current-turn checkout continuation evidence.

Persisted ordering state may keep a draft. It is not current-turn evidence.
Checkout continuation requires a signal from this inbound: an existing
confirmation/resume match, a current-turn order/payment intent, or a slot
value that actually appears in the current message.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ..types import INTENT_PAY_NOW, INTENT_START_ORDER

# Same checkout slot names the Decision engine already used, plus quantity
# and payment_method so a genuine current-turn qty/payment answer continues.
CURRENT_TURN_CHECKOUT_SLOT_KEYS = frozenset(
    {
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
        "payment_method",
    }
)

CURRENT_TURN_ORDER_INTENTS = frozenset(
    {
        INTENT_START_ORDER,
        INTENT_PAY_NOW,
    }
)


_NAME_SLOT_KEYS = frozenset(
    {
        "customer_first_name",
        "customer_last_name",
        "customer_name",
        "full_name",
    }
)


def _contains_arabic(value: Any) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in str(value or ""))


def _slot_value_in_current_inbound(value: Any, message: str) -> bool:
    """True when *value* is present in this inbound, not merely in state."""
    raw = str(value or "").strip()
    msg = str(message or "")
    if not raw or not msg:
        return False
    if len(raw) <= 2 and raw.isdigit():
        start = 0
        while True:
            idx = msg.find(raw, start)
            if idx < 0:
                return False
            before = msg[idx - 1] if idx > 0 else ""
            after = msg[idx + len(raw)] if idx + len(raw) < len(msg) else ""
            if not (before.isalnum() or after.isalnum()):
                return True
            start = idx + 1
    if raw in msg:
        return True
    lowered = raw.lower()
    return bool(lowered) and lowered in msg.lower()


def fresh_checkout_slots_from_current_inbound(
    message: str,
    intent_slots: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Slots extracted from this inbound. Ignores persisted prep values."""
    text = str(message or "")
    fresh: Dict[str, Any] = {}
    try:
        from ..intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

        extracted = extract_ordering_slots(text) or {}
    except Exception:  # noqa: BLE001  # noqa: silent-ok — extractor must not block decide
        extracted = {}
    try:
        from services.address_resolution import extract_address_signals  # noqa: PLC0415

        signals = extract_address_signals(text) or {}
    except Exception:  # noqa: BLE001  # noqa: silent-ok — address probe must not block decide
        signals = {}

    combined: Dict[str, Any] = dict(extracted)
    for key in ("google_maps_url", "short_address_code", "latitude", "longitude"):
        val = signals.get(key)
        if val not in (None, "", [], {}):
            combined[key] = val

    for key, val in dict(intent_slots or {}).items():
        if key in CURRENT_TURN_CHECKOUT_SLOT_KEYS and _slot_value_in_current_inbound(
            val, text
        ):
            combined[key] = val

    for key, val in combined.items():
        if key not in CURRENT_TURN_CHECKOUT_SLOT_KEYS:
            continue
        if val in (None, "", [], {}):
            continue
        if key in _NAME_SLOT_KEYS and not _contains_arabic(val):
            continue
        if key in extracted or key in signals:
            if _slot_value_in_current_inbound(val, text) or key in {
                "latitude",
                "longitude",
            }:
                fresh[key] = val
            continue
        if _slot_value_in_current_inbound(val, text):
            fresh[key] = val
    return fresh


def has_current_turn_checkout_continuation_evidence(
    ctx: Any,
    *,
    confirm_keyword_matched: bool = False,
) -> bool:
    """Authorize checkout continuation from this turn only."""
    if confirm_keyword_matched:
        return True
    message = str(getattr(ctx, "message", "") or "")
    try:
        from ..commerce.fresh_commerce_context import (  # noqa: PLC0415
            detect_explicit_order_resume,
        )

        if detect_explicit_order_resume(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — resume probe must not block decide
        pass

    intent = getattr(ctx, "intent", None)
    intent_name = str(getattr(intent, "name", "") or "")
    if intent_name in CURRENT_TURN_ORDER_INTENTS:
        return True

    try:
        from ..commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_address_on_file_claim,
            is_same_order_confirmation,
        )

        if is_same_order_confirmation(message) or is_address_on_file_claim(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — existing resume helpers must not block decide
        pass

    try:
        from ..commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            message_fulfills_awaited_checkout_slot,
        )

        prep = getattr(getattr(ctx, "state", None), "order_prep", None)
        awaiting_option = bool(getattr(prep, "awaiting_option_confirmation", False))
        awaiting_receipt = bool(getattr(prep, "awaiting_payment_receipt", False))
        if (
            prep is not None
            and not awaiting_option
            and not awaiting_receipt
            and message_fulfills_awaited_checkout_slot(
                message,
                order_prep=prep,
                customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
            )
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — awaited-slot probe must not block decide
        pass

    try:
        from ..commerce.catalog_order_checkout import (  # noqa: PLC0415
            current_turn_continues_catalog_checkout,
        )

        if current_turn_continues_catalog_checkout(ctx):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog current-turn probe must not block decide
        pass

    slots = getattr(intent, "slots", None) or {}
    if fresh_checkout_slots_from_current_inbound(message, slots):
        return True
    return False
