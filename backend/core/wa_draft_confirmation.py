"""
core/wa_draft_confirmation.py
─────────────────────────────
P0 — Deterministic outbound replies for Nahla WhatsApp draft orders.

Ensures no silent draft creation: whenever cart/draft state advances,
the customer receives a clear next-step message (location request,
product clarification, variant availability, or draft acknowledgement).

Operational only — no KB, no LLM.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.wa_cart_catalog_resolver import (
    CartCatalogResolution,
    ITEM_STATUS_CONFIRMED,
    ITEM_STATUS_CUSTOM_UNMATCHED,
    ITEM_STATUS_NEEDS_REVIEW,
)
from core.wa_order_lifecycle import has_accepted_delivery_address

_ORDER_FLOW_MARKERS = (
    "طلب", "موقع", "عنوان", "مدينة", "حي", "مسود", "جهز", "سجل",
    "أرسل", "ارسل", "تقصد", "متاح", "السعر", "كيلo", "كيلو",
    "checkout", "ادفع", "رقم الطلب",
)
_NON_ORDER_CONTACT_REPLY_MARKERS = (
    "موقع المعرض",
    "بيانات التواصل",
    "رقم التواصل",
    "زيارة المعرض",
)


def reply_covers_order_flow(reply: str) -> bool:
    """True when the existing reply already guides the customer on order flow."""
    text = (reply or "").strip()
    if len(text) < 12:
        return False
    blob = text.lower()
    if any(marker in blob or marker in text for marker in _NON_ORDER_CONTACT_REPLY_MARKERS):
        return False
    hits = sum(1 for m in _ORDER_FLOW_MARKERS if m in blob or m in text)
    return hits >= 1 and len(text) >= 20


def _draft_would_sync(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> bool:
    from services.nahla_order_bridge import _draft_eligible  # noqa: PLC0415

    prep = order_prep if isinstance(order_prep, dict) else {}
    bs = brain_state if isinstance(brain_state, dict) else {}
    eligible, _ = _draft_eligible(prep, bs)
    return eligible


def _cart_changed_this_turn(order_prep: Dict[str, Any]) -> bool:
    prep = order_prep if isinstance(order_prep, dict) else {}
    if prep.get("cart_deltas"):
        return True
    return bool(getattr(order_prep, "cart_deltas", None))


def _load_catalog_resolution(order_prep: Any) -> CartCatalogResolution:
    raw = {}
    if isinstance(order_prep, dict):
        raw = order_prep.get("wa_cart_catalog_resolution") or {}
    else:
        raw = getattr(order_prep, "wa_cart_catalog_resolution", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    return CartCatalogResolution(
        needs_clarification=bool(raw.get("needs_clarification")),
        clarification_question=str(raw.get("clarification_question") or ""),
        variant_unavailable=list(raw.get("variant_unavailable") or []),
        unmatched_items=list(raw.get("unmatched_items") or []),
        closest_suggestions=list(raw.get("closest_suggestions") or []),
    )


def _line_items_from_state(order_prep: Any, brain_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    for container in (
        brain_state,
        order_prep if isinstance(order_prep, dict) else {},
        {"line_items": getattr(order_prep, "line_items", None)},
        {"cart_items": getattr(order_prep, "cart_items", None)},
    ):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key)
            if isinstance(raw, list) and raw:
                return list(raw)
    li = getattr(order_prep, "line_items", None)
    return list(li or [])


def _compose_variant_unavailable_message(entry: Dict[str, Any]) -> str:
    hint = str(entry.get("variant_hint") or "").strip()
    title = str(entry.get("product_title") or "المنتج").strip()
    available = [str(v).strip() for v in (entry.get("available_variants") or []) if v]
    avail_txt = "، ".join(available[:4]) if available else "بالكيلo"

    if any(k in _norm_hint(hint) for k in ("10", "سطل")):
        return (
            f"السطل 10 كيلo غير ظاهر كخيار جاهز عندي. المتاح {avail_txt}. "
            "أقدر أسجل لك 10 علب كيلo أو أرفع طلب السطل للتأكيد."
        )
    return (
        f"الحجم اللي طلبته ({hint or 'غير محدد'}) مو متاح حاليًا على *{title}*. "
        f"المتوفر: {avail_txt}. أي حجم يناسبك؟"
    )


def _norm_hint(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _has_unknown_price(line_items: List[Dict[str, Any]]) -> bool:
    for item in line_items:
        status = str(item.get("match_status") or "").strip()
        if status in (ITEM_STATUS_NEEDS_REVIEW, ITEM_STATUS_CUSTOM_UNMATCHED):
            continue
        price = item.get("unit_price") or item.get("price")
        if price is None or str(price).strip() in ("", "0", "0.0"):
            if status == ITEM_STATUS_CONFIRMED:
                return True
    return False


def _all_items_unmatched(line_items: List[Dict[str, Any]]) -> bool:
    if not line_items:
        return False
    return all(
        str(i.get("match_status") or "") in (
            ITEM_STATUS_NEEDS_REVIEW,
            ITEM_STATUS_CUSTOM_UNMATCHED,
            "",
        )
        and not i.get("product_id")
        for i in line_items
    )


def compose_wa_order_flow_reply(
    *,
    order_prep: Any,
    brain_state: Dict[str, Any],
    catalog_resolution: Optional[CartCatalogResolution] = None,
    cart_changed: bool = False,
    existing_reply: str = "",
    customer_message: str = "",
    history: Optional[List[Any]] = None,
) -> Optional[str]:
    """
    Build a deterministic order-flow reply when the brain did not.

    Returns ``None`` when ``existing_reply`` already covers the flow or
    no draft/cart activity occurred this turn.
    """
    try:
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
            should_block_order_draft_injection,
        )

        if should_block_order_draft_injection(
            brain_state=brain_state,
            customer_message=customer_message or "",
            history=history,
        ):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — complaint draft block probe must not break order flow
        pass

    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            is_commerce_inquiry_turn,
        )
        from modules.ai.brain.commerce.product_visual import is_product_visual_request  # noqa: PLC0415

        msg = (customer_message or "").strip()
        if msg and (
            is_commerce_inquiry_turn(msg)
            or is_product_visual_request(msg)
        ):
            return None
    except Exception:  # noqa: silent-ok - inquiry draft block is best-effort
        pass

    prep_dict = order_prep if isinstance(order_prep, dict) else (
        order_prep.to_dict() if hasattr(order_prep, "to_dict") else {}
    )
    qty_clarify = ""
    if isinstance(order_prep, dict):
        qty_clarify = str(order_prep.get("active_order_quantity_clarification") or "").strip()
    else:
        qty_clarify = str(getattr(order_prep, "active_order_quantity_clarification", "") or "").strip()
    if qty_clarify:
        return qty_clarify

    if not cart_changed and not _cart_changed_this_turn(prep_dict):
        if not _draft_would_sync(prep_dict, brain_state):
            return None

    if reply_covers_order_flow(existing_reply):
        return None

    resolution = catalog_resolution or _load_catalog_resolution(order_prep)
    line_items = _line_items_from_state(order_prep, brain_state)

    # Rule D — ambiguous product
    if resolution.needs_clarification and resolution.clarification_question:
        return resolution.clarification_question

    # Rule C — variant/size unavailable
    if resolution.variant_unavailable:
        return _compose_variant_unavailable_message(resolution.variant_unavailable[0])

    # Rule B — unmatched free-text items
    if resolution.unmatched_items or _all_items_unmatched(line_items):
        if resolution.closest_suggestions:
            opts = " أو ".join(f"*{t}*" for t in resolution.closest_suggestions[:2])
            return f"ما لقيت المنتج بالضبط في الكتالوج. تقصد {opts}؟"
        return "ما لقيت المنتج في الكتالوج. اكتب اسم المنتج أو الحجم اللي تبغاه بالضبط."

    # Rule F — unknown price on confirmed items
    if _has_unknown_price(line_items):
        return (
            "هذا الخيار يحتاج تأكيد السعر. أرفعه لك للتأكيد "
            "أو تختار الحجم المتوفر بالسعر الظاهر؟"
        )

    missing_address = not has_accepted_delivery_address(prep_dict)

    # Rule E — product clear, location missing
    if missing_address and line_items and any(i.get("product_id") for i in line_items):
        return (
            "اختياراتك محفوظة في هذه المحادثة مبدئيًا، "
            "ونكملها بعد العنوان."
        )

    # Rule A — generic draft acknowledgement when location still missing
    if missing_address and line_items:
        return (
            "اختياراتك محفوظة في هذه المحادثة مبدئيًا، "
            "ونكملها بعد العنوان."
        )

    if line_items and cart_changed:
        return "تمام، حدّثت طلبك. في شي ثاني تبغاه؟"

    return None


def maybe_inject_draft_flow_reply(
    *,
    reply: str,
    order_prep: Any,
    brain_state: Any,
    catalog_resolution: Optional[CartCatalogResolution] = None,
    cart_changed: bool = False,
    customer_message: str = "",
    history: Optional[List[Any]] = None,
) -> str:
    """Return ``reply`` or an injected order-flow fallback — never silent."""
    try:
        from modules.ai.order_flow_v2.flags import should_skip_legacy_order_flow_reply  # noqa: PLC0415

        if should_skip_legacy_order_flow_reply():
            return reply or ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not block legacy inject
        pass

    bs = brain_state.to_dict() if hasattr(brain_state, "to_dict") else dict(brain_state or {})
    prep = order_prep
    injected = compose_wa_order_flow_reply(
        order_prep=prep,
        brain_state=bs,
        catalog_resolution=catalog_resolution,
        cart_changed=cart_changed,
        existing_reply=reply or "",
        customer_message=customer_message or "",
        history=history,
    )
    if injected and not (reply or "").strip():
        return injected
    if injected and not reply_covers_order_flow(reply or ""):
        return injected
    return reply or ""


__all__ = [
    "compose_wa_order_flow_reply",
    "maybe_inject_draft_flow_reply",
    "reply_covers_order_flow",
]
