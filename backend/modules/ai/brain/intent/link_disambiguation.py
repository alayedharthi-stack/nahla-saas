"""
brain/intent/link_disambiguation.py
───────────────────────────────────
Order-state-aware disambiguation between store / payment / tracking
link requests.

The store-link safety net and commerce decision paths treat bare
"الرابط" / "ارسل الرابط" as checkout/store intent. After an order
is already confirmed, the same phrasing usually means tracking /
shipment follow-up — not a new funnel start.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# High-level order states where the customer is past checkout creation.
POST_ORDER_STATUSES = frozenset({
    "awaiting_receipt",
    "under_review",
    "in_review",
    "pending_review",
    "awaiting_review",
    "processing",
    "preparing",
    "ready",
    "shipped",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "payment_pending",
    "complete",
    "confirmed",
})

# Outbound copy proving an order was already confirmed / is in review.
_POST_ORDER_OUTBOUND_MARKERS: tuple = (
    "تم تأكيد",
    "تم تأكيده",
    "تم تأكيد طلبك",
    "بانتظار المراجعة",
    "بإنتظار المراجعة",
    "في انتظار المراجعة",
    "طلبك رقم",
    "رقم طلبك",
    "order confirmed",
    "under review",
    "pending review",
)

# Prior turn discussed tracking being unavailable / forthcoming.
_TRACKING_TOPIC_HISTORY_MARKERS: tuple = (
    "رابط التتبع",
    "رقم التتبع",
    "ما يتوفر عندي رابط تتبع",
    "ما صدر رابط التتبع",
    "link التتبع",
    "tracking link",
    "tracking number",
)

_TRACKING_EXPLICIT_PHRASES: tuple = (
    "رابط التتبع",
    "رابط تتبع",
    "رقم التتبع",
    "رقم تتبع",
    "رابط الشحن",
    "رابط شحن",
    "ارسلوا التتبع",
    "أرسلوا التتبع",
    "ارسل التتبع",
    "أرسل التتبع",
    "ابعث التتبع",
    "أبعث التتبع",
    "ارسلوا رقم التتبع",
    "أرسلوا رقم التتبع",
    "ارسل رقم التتبع",
    "أرسل رقم التتبع",
    "متى يوصلني رابط",
    "متى يوصل رابط",
    "tracking link",
    "tracking number",
    "track my order",
    "order tracking",
)

_SHIPPING_CONTEXT_MARKERS: tuple = (
    "تشحن",
    "تشحنو",
    "تشحنه",
    "تشحنها",
    "انشحن",
    "إذا شحن",
    "اذا شحن",
    "لما يشحن",
    "لما تشحن",
    "بمجرد ما يتم شحن",
    "اول ما يتم شحن",
    "أول ما يتم شحن",
    "بعد الشحن",
    "عند الشحن",
    "يوصل",
    "التوصيل",
    "الشحنة",
    "شحنتي",
    "shipped",
    "shipping",
    "delivery",
    "tracking",
)

_LINK_NOUN_MARKERS: tuple = (
    "الرابط",
    "رابط",
    "اللينك",
    "لينك",
    "link",
)

_STORE_LINK_MARKERS: tuple = (
    "رابط المتجر",
    "رابط متجر",
    "المتجر الالكتروني",
    "المتجر الإلكتروني",
    "store link",
    "store url",
    "website link",
    "shop link",
    "online store",
)

_PAYMENT_LINK_MARKERS: tuple = (
    "رابط الدفع",
    "رابط دفع",
    "رابط الطلب",
    "checkout link",
    "payment link",
    "ادفع",
    "أدفع",
    "دفع",
    "سدد",
    "أسدد",
    "إتمام الدفع",
    "اكمل الدفع",
    "أكمل الدفع",
)

_SHIPPING_LINK_COMBO_RE = re.compile(
    r"(?:تشحن|تشحنو|تشحنه|تشحنها|انشحن|إذا\s+شحن|اذا\s+شحن|لما\s+(?:ي)?شحن|"
    r"بمجرد\s+ما\s+يتم\s+شحن|اول\s+ما\s+يتم\s+شحن|أول\s+ما\s+يتم\s+شحن|"
    r"بعد\s+الشحن|عند\s+الشحن|shipped|shipping|delivery|tracking)"
    r".{0,40}"
    r"(?:الرابط|رابط|التتبع|تتبع|اللينك|لينك|link|tracking)",
    re.IGNORECASE | re.UNICODE,
)

_SEND_LINK_COMBO_RE = re.compile(
    r"(?:ارسل|أرسل|ارسلو|أرسلوا|ارسلي|أرسلي|ابعث|أبعث|ابعثو|أبعثوا|"
    r"اعط|أعط|وين|send)\s*(?:لي\s+|لنا\s+|لي\s+)?"
    r"(?:ال)?(?:رابط|تتبع|لينك|link|tracking)",
    re.IGNORECASE | re.UNICODE,
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_DIA_RE = re.compile(f"[{_DIA}]+")


def _normalise(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = _DIA_RE.sub("", t)
    t = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _contains_any(text: str, needles: tuple) -> bool:
    return any(n in text for n in needles)


def history_indicates_post_order(
    history: Optional[List[Dict[str, Any]]],
    *,
    lookback_outbound: int = 6,
) -> bool:
    """True when recent outbound turns prove an order already exists."""
    if not history:
        return False
    outbound_seen = 0
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            outbound_seen += 1
            body = _normalise(str((turn or {}).get("body") or ""))
            if body and _contains_any(body, _POST_ORDER_OUTBOUND_MARKERS):
                return True
            if outbound_seen >= lookback_outbound:
                break
    except Exception:  # noqa: BLE001
        return False
    return False


def history_recent_tracking_topic(
    history: Optional[List[Dict[str, Any]]],
    *,
    lookback_outbound: int = 4,
) -> bool:
    """True when a recent bot turn already discussed tracking links."""
    if not history:
        return False
    outbound_seen = 0
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            outbound_seen += 1
            body = _normalise(str((turn or {}).get("body") or ""))
            if body and _contains_any(body, _TRACKING_TOPIC_HISTORY_MARKERS):
                return True
            if outbound_seen >= lookback_outbound:
                break
    except Exception:  # noqa: BLE001
        return False
    return False


def order_prep_indicates_post_order(order_prep: Any) -> bool:
    if order_prep is None:
        return False
    try:
        status = str(getattr(order_prep, "order_status", "") or "").strip().lower()
        if status in POST_ORDER_STATUSES:
            return True
        if bool(getattr(order_prep, "payment_receipt_received", False)):
            return True
        if str(getattr(order_prep, "draft_order_id", "") or "").strip() and status:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def brain_state_indicates_post_order(state: Any) -> bool:
    if state is None:
        return False
    try:
        if order_prep_indicates_post_order(getattr(state, "order_prep", None)):
            return True
        if str(getattr(state, "draft_order_id", "") or "").strip():
            status = str(
                getattr(getattr(state, "order_prep", None), "order_status", "") or ""
            ).strip().lower()
            if status in POST_ORDER_STATUSES:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def has_active_post_order_context(
    *,
    state: Any = None,
    order_prep: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Unified post-order detector for brain + safety-net layers."""
    if brain_state_indicates_post_order(state):
        return True
    if order_prep_indicates_post_order(order_prep):
        return True
    if history_indicates_post_order(history):
        return True
    return False


def looks_like_payment_link_request(message: str) -> bool:
    norm = _normalise(message)
    if not norm:
        return False
    return _contains_any(norm, _PAYMENT_LINK_MARKERS)


def looks_like_store_link_request(message: str) -> bool:
    norm = _normalise(message)
    if not norm:
        return False
    return _contains_any(norm, _STORE_LINK_MARKERS)


def looks_like_tracking_link_request(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
    order_prep: Any = None,
) -> bool:
    """True when the customer is asking about shipment / tracking links."""
    norm = _normalise(message)
    if not norm:
        return False

    if looks_like_payment_link_request(norm):
        return False

    if looks_like_store_link_request(norm):
        return False

    if _contains_any(norm, _TRACKING_EXPLICIT_PHRASES):
        return True

    if _SHIPPING_LINK_COMBO_RE.search(norm):
        return True

    if _SEND_LINK_COMBO_RE.search(norm) and _contains_any(norm, _SHIPPING_CONTEXT_MARKERS):
        return True

    post_order = has_active_post_order_context(
        state=state,
        order_prep=order_prep,
        history=history,
    )
    if not post_order:
        return False

    if _contains_any(norm, _LINK_NOUN_MARKERS) and _contains_any(norm, _SHIPPING_CONTEXT_MARKERS):
        return True

    if _SEND_LINK_COMBO_RE.search(norm):
        return True

    if _contains_any(norm, _LINK_NOUN_MARKERS) and history_recent_tracking_topic(history):
        return True

    return False


def should_suppress_store_link_intent(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
    order_prep: Any = None,
) -> bool:
    """Gate store-link safety nets / artifact injection for tracking asks."""
    return looks_like_tracking_link_request(
        message,
        history=history,
        state=state,
        order_prep=order_prep,
    )
