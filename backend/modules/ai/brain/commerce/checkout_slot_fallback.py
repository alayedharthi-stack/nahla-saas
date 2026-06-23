"""
commerce/checkout_slot_fallback.py
──────────────────────────────────
Deterministic checkout slot prompts — used when compose is empty/stub
or loop guard must avoid generic recovery inside active checkout.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_CHECKOUT_CONTINUE_RE = re.compile(
    r"^(?:"
    r"كمل|كملي|كمّل|كمّلي|"
    r"تابع|تابعي|"
    r"استمر|استمري|"
    r"تمام|"
    r"نعم|"
    r"ايه|أيه|اي|أي|"
    r"اوك|ok|okay|"
    r"ماشي|موافق"
    r")\s*$",
    re.IGNORECASE | re.UNICODE,
)

_NAME_SLOTS = frozenset({"customer_first_name", "customer_last_name", "customer_name"})
_CITY_SLOTS = frozenset({"city"})
_ADDRESS_SLOTS = frozenset({
    "delivery_address",
    "short_address_code",
    "address_location",
    "google_maps_url",
    "address",
    "address_line",
    "location",
})
_PAYMENT_SLOTS = frozenset({"payment_method"})

_PROMPT_NAME = "عشان أكمل طلبك، ممكن تكتب اسمك الكامل؟"
_PROMPT_CITY = "تمام، ممكن تكتب المدينة؟"
_PROMPT_ADDRESS = (
    "باقي العنوان للتوصيل، أرسل رابط خرائط أو رمز العنوان الوطني المختصر."
)
_PROMPT_PAYMENT = "باقي تختار طريقة الدفع المناسبة لك."
_PROMPT_REVIEW = "وصلتني بيانات الطلب، أراجعها الآن وأكمل معك."


def is_checkout_continue_inbound(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_CHECKOUT_CONTINUE_RE.match(raw))


def _order_prep_dict(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        prep = state.get("order_prep") or state
        return dict(prep) if isinstance(prep, dict) else {}
    prep = getattr(state, "order_prep", None)
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    try:
        if hasattr(prep, "to_dict"):
            return dict(prep.to_dict() or {})
    except Exception:  # noqa: BLE001
        pass
    return {}


def _brain_state_dict(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        return dict(state)
    try:
        if hasattr(state, "to_dict"):
            return dict(state.to_dict() or {})
    except Exception:  # noqa: BLE001
        pass
    return {}


def _resolve_missing_fields(state: Any) -> List[str]:
    prep = _order_prep_dict(state)
    bs = _brain_state_dict(state)
    line_items = list(
        prep.get("line_items")
        or prep.get("cart_items")
        or bs.get("cart_items")
        or []
    )
    stored = list(prep.get("missing_fields") or [])
    if stored:
        return [str(x).strip() for x in stored if str(x).strip()]

    try:
        from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

        return list(
            compute_wa_missing_fields(
                prep,
                brain_state=bs,
                line_items=line_items,
            )
        )
    except Exception:  # noqa: BLE001
        return stored


def build_checkout_slot_fallback_reply(
    *,
    state: Any = None,
    inbound_text: str = "",
) -> Optional[str]:
    """
    Return the next checkout slot prompt for active commerce state.

    Never asks for phone. Returns None when checkout is not active.
    """
    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )
    except Exception:  # noqa: BLE001
        return None

    if not has_active_commerce_from_state(state):
        return None

    missing = _resolve_missing_fields(state)
    if not missing:
        return _PROMPT_REVIEW

    for field in missing:
        slot = str(field).strip().lower()
        if slot in _NAME_SLOTS or slot == "customer_first_name":
            return _PROMPT_NAME
        if slot in _CITY_SLOTS:
            return _PROMPT_CITY
        if slot in _ADDRESS_SLOTS or slot == "delivery_address":
            return _PROMPT_ADDRESS
        if slot in _PAYMENT_SLOTS:
            return _PROMPT_PAYMENT
        if slot == "product":
            return (
                "باقي تحدد المنتج أو الكمية عشان نكمل الطلب."
            )

    # Unknown missing token — still better than a broken generic stub.
    if is_checkout_continue_inbound(inbound_text):
        return _PROMPT_REVIEW
    return _PROMPT_REVIEW


__all__ = [
    "build_checkout_slot_fallback_reply",
    "is_checkout_continue_inbound",
]
