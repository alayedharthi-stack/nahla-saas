"""
commerce/checkout_slot_fallback.py
──────────────────────────────────
Deterministic checkout slot prompts — used when compose is empty/stub
or loop guard must avoid generic recovery inside active checkout.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.commerce.checkout_slot_fallback")

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

_WHATSAPP_PRODUCT_CHANNELS = frozenset({
    "whatsapp_fast",
    "whatsapp_quick_order",
    "whatsapp_catalog",
})


def _has_authoritative_product(state: Any) -> bool:
    """True when checkout has catalog-backed or explicitly selected products."""
    prep = _order_prep_dict(state)
    if not prep:
        return False
    try:
        from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
            is_catalog_line_items_authoritative_from_prep,
        )

        if is_catalog_line_items_authoritative_from_prep(prep):
            line_items = list(prep.get("line_items") or prep.get("cart_items") or [])
            return bool(line_items)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — authoritative probe must not block fallback
        pass

    bs = _brain_state_dict(state)
    line_items = list(
        prep.get("line_items")
        or prep.get("cart_items")
        or bs.get("cart_items")
        or []
    )
    if line_items:
        for item in line_items:
            if not isinstance(item, dict):
                continue
            if item.get("from_native_catalog_order") or item.get("from_catalog_order"):
                return True
            if item.get("product_retailer_id") and item.get("product_id"):
                return True
        channel = str(prep.get("checkout_channel") or "").strip().lower()
        if channel in _WHATSAPP_PRODUCT_CHANNELS:
            return True
    return False


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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional to_dict must not block slot fallback
        logger.exception("[CHECKOUT_SLOT_FALLBACK] order_prep to_dict failed")
    return {}


def _brain_state_dict(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        return dict(state)
    try:
        if hasattr(state, "to_dict"):
            return dict(state.to_dict() or {})
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional to_dict must not block slot fallback
        logger.exception("[CHECKOUT_SLOT_FALLBACK] brain_state to_dict failed")
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

    try:
        from core.order_missing_fields_engine import (  # noqa: PLC0415
            missing_fields_engine_enabled,
        )

        if missing_fields_engine_enabled():
            from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

            return list(
                compute_wa_missing_fields(
                    prep,
                    brain_state=bs,
                    line_items=line_items,
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CHECKOUT_SLOT_FALLBACK] engine missing-fields gate failed")

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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — missing-field compute is best-effort fallback
        logger.exception("[CHECKOUT_SLOT_FALLBACK] compute_wa_missing_fields failed")
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
        from modules.ai.order_flow_v2.flags import should_skip_legacy_order_flow_reply  # noqa: PLC0415

        if should_skip_legacy_order_flow_reply():
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not block legacy fallback import
        pass

    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — stub guard import optional at fallback boundary
        logger.exception("[CHECKOUT_SLOT_FALLBACK] has_active_commerce import failed")
        return None

    if not has_active_commerce_from_state(state):
        return None

    has_product = _has_authoritative_product(state)
    missing = _resolve_missing_fields(state)
    if not missing:
        return _PROMPT_REVIEW if has_product else None

    for field in missing:
        slot = str(field).strip().lower()
        if slot in _CITY_SLOTS or slot in _ADDRESS_SLOTS or slot == "delivery_address":
            if not has_product:
                continue
        if slot in _NAME_SLOTS or slot == "customer_first_name":
            try:
                from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                    is_catalog_checkout_name_question_forbidden,
                )

                prep = getattr(state, "order_prep", None)
                prep_d: Dict[str, Any] = {}
                if prep is not None and hasattr(prep, "to_dict"):
                    try:
                        prep_d = dict(prep.to_dict())
                    except Exception:  # noqa: BLE001
                        prep_d = {}
                elif isinstance(prep, dict):
                    prep_d = dict(prep)
                first = str(prep_d.get("customer_first_name") or "").strip()
                last = str(prep_d.get("customer_last_name") or "").strip()
                if first and last:
                    continue
                if is_catalog_checkout_name_question_forbidden(
                    known_facts={"customer_name_known": bool(first and last)},
                ):
                    continue
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog name guard must not block fallback
                pass
            return _PROMPT_NAME
        if slot in _CITY_SLOTS:
            return _PROMPT_CITY
        if slot in _ADDRESS_SLOTS or slot == "delivery_address":
            return _PROMPT_ADDRESS
        if slot in _PAYMENT_SLOTS:
            return _PROMPT_PAYMENT
        if slot == "product":
            try:
                from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                    is_catalog_checkout_product_question_forbidden,
                )

                if is_catalog_checkout_product_question_forbidden(state=state):
                    continue
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard must not block fallback
                pass
            return (
                "باقي تحدد المنتج أو الكمية عشان نكمل الطلب."
            )

    if not has_product:
        return None

    if is_checkout_continue_inbound(inbound_text):
        return _PROMPT_REVIEW
    return _PROMPT_REVIEW


__all__ = [
    "_has_authoritative_product",
    "build_checkout_slot_fallback_reply",
    "is_checkout_continue_inbound",
]
