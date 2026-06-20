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

# E-commerce store URL — only when the customer explicitly asks for the
# online storefront / checkout site, NOT a physical branch on Maps.
_ECOMMERCE_STORE_EXPLICIT_MARKERS: tuple = (
    "رابط المتجر",
    "رابط متجر",
    "رابط متجركم",
    "رابط متجرك",
    "المتجر الالكتروني",
    "المتجر الإلكتروني",
    "موقعكم الالكتروني",
    "موقعكم الإلكتروني",
    "الموقع الالكتروني",
    "الموقع الإلكتروني",
    "رابط الموقع",
    "رابط موقعكم",
    "رابط موقعك",
    "رابط موقعنا",
    "رابط الشراء",
    "رابط الطلب",
    "الويب سايت",
    "ويب سايت",
    "website",
    "رابط الاونلاين",
    "رابط الأونلاين",
    "رابط الاون لاين",
    "اطلب من الموقع",
    "أطلب من الموقع",
    "ابي اطلب من الموقع",
    "أبي أطلب من الموقع",
    "ابغى اطلب من الموقع",
    "أبغى أطلب من الموقع",
    "اونلاين",
    "أونلاين",
    "online store",
    "store link",
    "store url",
    "website link",
    "shop link",
)

# Physical shop / branch / Google Maps — default for bare "موقع …"
# phrasing on WhatsApp unless the customer explicitly said "online".
_PHYSICAL_LOCATION_MARKERS: tuple = (
    "موقع المتجر",
    "موقع المعرض",
    "موقع المحل",
    "وين موقعكم",
    "أين موقعكم",
    "وين موقع",
    "وين الموقع",
    "وين المحل",
    "وين المعرض",
    "وين أنتم",
    "وين انتم",
    "وين فرعكم",
    "وين الفرع",
    "وين مقركم",
    "عنوان المحل",
    "عنوان المعرض",
    "عنوان الفرع",
    "ارسل اللوكيشن",
    "أرسل اللوكيشن",
    "ارسلي اللوكيشن",
    "أرسلي اللوكيشن",
    "ابعث اللوكيشن",
    "أبعث اللوكيشن",
    "ابعثلي اللوكيشن",
    "ابي اللوكيشن",
    "أبي اللوكيشن",
    "ابغى اللوكيشن",
    "أبغى اللوكيشن",
    "اللوكيشن",
    "لوكيشن المحل",
    "لوكيشن المتجر",
    "لوكيشن المعرض",
    "لوكيشن الفرع",
    "google maps",
    "store location",
    "branch location",
    "where are you",
    "where is your shop",
    "where is your branch",
)

# Bare "موقع + noun" without "رابط"/"إلكتروني" → physical branch.
_PHYSICAL_LOCATION_SITE_RE = re.compile(
    r"(?:^|\s)(?:وين|أين|اين)\s+(?:موقع(?:كم|ك|نا)?|انتم|أنتم|انت|فرع|محل|معرض|مقر)\b",
    re.UNICODE | re.IGNORECASE,
)
_PHYSICAL_SITE_NOUN_RE = re.compile(
    r"(?:^|\s)موقع(?:\s+(?:المتجر|المعرض|المحل|الفرع|كم|ك|نا))\b",
    re.UNICODE | re.IGNORECASE,
)
_SEND_LOCATION_REQUEST_RE = re.compile(
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي|ابعث|أبعث|ابعثلي|أبعثلي|ابي|أبي|ابغى|أبغى)"
    r"\s*(?:لي\s+)?(?:ال)?(?:موقع|عنوان|اللوكيشن)(?:ه|ها|كم|ك)?\b",
    re.UNICODE | re.IGNORECASE,
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

# Pre-shipment statuses — tracking URL is usually not issued yet.
PRE_SHIP_STATUSES = frozenset({
    "",
    "awaiting_receipt",
    "under_review",
    "in_review",
    "pending_review",
    "awaiting_review",
    "processing",
    "preparing",
    "ready",
    "payment_pending",
    "pending_payment",
    "pending",
    "confirmed",
    "complete",
})

_SHIPPED_STATUSES = frozenset({
    "shipped",
    "in_transit",
    "out_for_delivery",
    "delivered",
})

_ORDER_REF_RE = re.compile(
    r"(?:طلب(?:ك|كم)?\s*رقم|رقم\s*(?:ال)?طلب(?:ك|كم)?|order\s*(?:#|number)?)\s*[:#]?\s*(\d{4,})",
    re.IGNORECASE | re.UNICODE,
)


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
    lookback_outbound: int = 0,
) -> bool:
    """True when outbound turns prove an order already exists.

    ``lookback_outbound=0`` scans the full history so post-order context
    survives product Q&A turns after confirmation.
    """
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
            if lookback_outbound and outbound_seen >= lookback_outbound:
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
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    """Unified post-order detector for brain + safety-net layers."""
    try:
        from core.active_order_context import structured_indicates_post_order  # noqa: PLC0415

        if structured_indicates_post_order(commerce_bundle):
            return True
    except Exception:  # noqa: BLE001
        pass
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


def looks_like_ecommerce_store_link_request(message: str) -> bool:
    """True when the customer explicitly wants the online store URL."""
    norm = _normalise(message)
    if not norm:
        return False
    return _contains_any(norm, _ECOMMERCE_STORE_EXPLICIT_MARKERS)


def looks_like_physical_location_request(message: str) -> bool:
    """True when the customer wants a branch / shop on Google Maps.

    Bare phrasings like ``موقع المتجر`` default here — NOT to the
    e-commerce ``store_url`` — unless the customer said ``online`` /
    ``رابط المتجر`` / ``المتجر الإلكتروني`` explicitly.
    """
    norm = _normalise(message)
    if not norm:
        return False
    if looks_like_ecommerce_store_link_request(message):
        return False
    if _SEND_LOCATION_REQUEST_RE.search(norm):
        return True
    if _contains_any(norm, _PHYSICAL_LOCATION_MARKERS):
        return True
    if _PHYSICAL_LOCATION_SITE_RE.search(norm):
        return True
    if _PHYSICAL_SITE_NOUN_RE.search(norm):
        return True
    return False


def looks_like_store_link_request(message: str) -> bool:
    norm = _normalise(message)
    if not norm:
        return False
    if looks_like_physical_location_request(message):
        return False
    if looks_like_ecommerce_store_link_request(message):
        return True
    return _contains_any(norm, _STORE_LINK_MARKERS)


def looks_like_tracking_link_request(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
    order_prep: Any = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
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
        commerce_bundle=commerce_bundle,
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
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    """Gate store-link safety nets / artifact injection for non-store asks."""
    if looks_like_physical_location_request(message):
        return True
    return looks_like_tracking_link_request(
        message,
        history=history,
        state=state,
        order_prep=order_prep,
        commerce_bundle=commerce_bundle,
    )


def resolve_order_status(
    *,
    state: Any = None,
    order_prep: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Best-effort order status from brain state or recent outbound copy."""
    prep = order_prep if order_prep is not None else getattr(state, "order_prep", None)
    try:
        status = str(getattr(prep, "order_status", "") or "").strip().lower()
        if status:
            return status
    except Exception:  # noqa: BLE001
        pass

    if not history:
        return ""

    status_markers = (
        ("بانتظار المراجعة", "under_review"),
        ("بإنتظار المراجعة", "under_review"),
        ("بمرحلة المراجعة", "under_review"),
        ("مرحلة المراجعة", "under_review"),
        ("pending review", "pending_review"),
        ("under review", "under_review"),
        ("تم الشحن", "shipped"),
        ("في طريق", "in_transit"),
        ("خارج للتوصيل", "out_for_delivery"),
        ("تم التسليم", "delivered"),
    )
    _SHIPPED_BODY_RE = re.compile(
        r"(?<![\u064a\u064a])تم\s+شحن(?:ه|ها|هم)?",
        re.UNICODE,
    )
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            body = _normalise(str((turn or {}).get("body") or ""))
            if not body:
                continue
            for marker, slug in status_markers:
                if marker in body:
                    return slug
            if _SHIPPED_BODY_RE.search(body):
                return "shipped"
    except Exception:  # noqa: BLE001
        return ""
    return ""


def extract_order_reference(
    *,
    state: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Pull a confirmed order number from brain state or conversation history."""
    for source in (
        str(getattr(state, "draft_order_id", "") or "").strip(),
        str(getattr(getattr(state, "order_prep", None), "draft_order_id", "") or "").strip(),
    ):
        if source:
            return source

    if not history:
        return ""
    try:
        for turn in reversed(history):
            body = str((turn or {}).get("body") or "")
            if not body:
                continue
            match = _ORDER_REF_RE.search(body)
            if match:
                return match.group(1)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def is_pre_ship_status(status: str) -> bool:
    slug = str(status or "").strip().lower()
    try:
        from core.active_order_context import is_pre_ship_canonical  # noqa: PLC0415

        if slug in {"pending_review", "confirmed", "preparing", "shipped", "delivered"}:
            return is_pre_ship_canonical(slug)
    except Exception:  # noqa: BLE001
        pass
    if slug in _SHIPPED_STATUSES:
        return False
    return slug in PRE_SHIP_STATUSES or not slug


def should_use_generative_tracking_follow_up(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
    order_prep: Any = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the brain (not a template) should answer a tracking-link ask."""
    if looks_like_payment_link_request(message):
        return False
    if looks_like_store_link_request(message):
        return False
    if not has_active_post_order_context(
        state=state,
        order_prep=order_prep,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return False
    if not looks_like_tracking_link_request(
        message,
        history=history,
        state=state,
        order_prep=order_prep,
        commerce_bundle=commerce_bundle,
    ):
        return False
    try:
        from core.active_order_context import resolve_order_status as _resolve_status  # noqa: PLC0415

        status, _mode = _resolve_status(
            commerce_bundle=commerce_bundle,
            state=state,
            order_prep=order_prep,
            history=history,
        )
    except Exception:  # noqa: BLE001
        status = resolve_order_status(
            state=state,
            order_prep=order_prep,
            history=history,
        )
    return is_pre_ship_status(status)


def build_tracking_follow_up_args(
    *,
    state: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
    tracking_available: bool = False,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decision args for ``ACTION_LLM_REPLY`` tracking follow-up turns."""
    try:
        from core.active_order_context import (  # noqa: PLC0415
            resolve_order_reference as _resolve_ref,
            resolve_order_status as _resolve_status,
            tracking_available_from_bundle,
        )

        order_ref, _ref_mode = _resolve_ref(
            commerce_bundle=commerce_bundle,
            state=state,
            history=history,
        )
        status, _status_mode = _resolve_status(
            commerce_bundle=commerce_bundle,
            state=state,
            order_prep=getattr(state, "order_prep", None) if state else None,
            history=history,
        )
        if commerce_bundle is not None:
            tracking_available = tracking_available_from_bundle(commerce_bundle)
    except Exception:  # noqa: BLE001
        order_ref = extract_order_reference(state=state, history=history)
        status = resolve_order_status(state=state, history=history)

    args: Dict[str, Any] = {
        "topic": "tracking_link_follow_up",
        "intent_hint": "order_tracking",
        "tracking_available": bool(tracking_available),
    }
    if order_ref:
        args["order_reference"] = order_ref
    if status:
        args["order_status"] = status
    return args
