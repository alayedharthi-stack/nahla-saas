"""
order_tracking_intent_guard.py
──────────────────────────────
Platform-wide guard: existing-order tracking follow-ups must not drift
into product browse, availability rewrite, or generic escalation stubs.

Layer 1 — intent boost (classifier + decision engine)
Layer 2 — availability rewrite exempt (via availability_guard_policy)
Layer 3 — staff escalation stub replacement (via staff_escalation_truth_guard)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional

from ..types import INTENT_TRACK_ORDER, Intent

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s.lower()).strip()


# Hypothetical / pre-order shipping — NOT an existing-order follow-up.
_PRE_ORDER_MARKERS_RE = re.compile(
    r"(?:"
    r"(?:اذا|إذا|لو|لما|قبل\s*(?:ما\s*)?(?:اطلب|اطلبي|اشتري|الطلب))"
    r"|"
    r"(?:if\s+i\s+order|before\s+i\s+order|when\s+i\s+order)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Pure browse — no existing-order tracking context.
_PURE_BROWSE_RE = re.compile(
    r"^(?:"
    r"(?:ا|أ)?(?:بي|بغى|بغي|ريد|ودي|بدي)\s+(?:عسل|طلح|سمر|سدر|شمع|حلاو|منتج|\S+)"
    r"|"
    r"(?:وش|ايش|ايه|what)\s*(?:ال)?(?:خيارات|خيار|انواع|أنواع|options|choices)"
    r"|"
    r"(?:وش|ايش|ايه)\s+(?:المتوفر|الموجود|عندكم)"
    r")(?:\s*[\?؟!.]*)?$",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_TRACKING_PHRASES_RAW = (
    "متى يوصل الطلب",
    "متى توصل الطلب",
    "متى يجي الطلب",
    "متى توصل الطلبية",
    "وين طلبي",
    "وين الطلب",
    "فين طلبي",
    "حالة الطلب",
    "رقم التتبع",
    "رابط التتبع",
    "الشحنة وينها",
    "وين الشحنة",
    "فين الشحنة",
    "تتبع الطلب",
    "order status",
    "tracking number",
    "track my order",
    "where is my order",
)
_EXPLICIT_TRACKING_PHRASES = tuple(_norm_ar(p) for p in _EXPLICIT_TRACKING_PHRASES_RAW)

_ORDER_ANCHOR_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|الشحنه|الشحنة|الطلب(?:يه)?|رقم\s*الطلب|"
    r"رقم\s*التتبع|رابط\s*التتبع|التتبع|tracking"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_STATUS_QUESTION_RE = re.compile(
    r"(?:"
    r"متي|وين|فين|اين|أين|حالة|status|"
    r"(?:هل\s+)?(?:يوصل|وصل|وصلت|توصل|تشحن|شحن)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PAST_ORDER_TRACKING_RE = re.compile(
    r"(?:"
    r"طلبت(?:\s+طلب)?|سويت\s*طلب|عملت\s*طلب|قدمت\s*طلب"
    r").{0,50}(?:"
    r"متي\s*(?:يوصل|توصل|يجي|يصل)|"
    r"(?:وين|فين|اين)\s*(?:طلب|الشحن|الشحنه)|"
    r"حالة\s*(?:ال)?طلب|"
    r"(?:و)?اب(?:ي|غى|غي)\s*اعرف\s*متي"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PLACED_ORDER_DELIVERY_RE = re.compile(
    r"طلبت.{0,20}(?:"
    r"متي\s*(?:يوصل|توصل|يجي|يصل)|"
    r"(?:و)?اب(?:ي|غى|غي)\s*اعرف\s*متي\s*(?:يوصل|توصل|يجي|يصل)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_SHIPPING_TIMING_RE = re.compile(
    r"متي\s*(?:يوصل|توصل|يجي|يصل|تاخذ|تاخذون|يستغرق)",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_PRODUCT_HINT_RE = re.compile(
    r"(?:"
    r"عسل|طلح|سمر|سدر|شمع|حلاو|كيلو|جرام|منتج|صنف|نوع"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_pre_order_shipping_inquiry(message: str) -> bool:
    """Hypothetical shipping timing — e.g. «متى يوصل عسل الطلح إذا طلبته؟»."""
    norm = _norm_ar(message)
    if not norm:
        return False
    if _PRE_ORDER_MARKERS_RE.search(norm):
        return True
    if (
        _PRODUCT_SHIPPING_TIMING_RE.search(norm)
        and _CATALOG_PRODUCT_HINT_RE.search(norm)
        and not _ORDER_ANCHOR_RE.search(norm)
        and not _PAST_ORDER_TRACKING_RE.search(norm)
    ):
        return True
    return False


def is_order_tracking_follow_up(message: str) -> bool:
    """
    True when the customer is asking about an existing order/shipment,
    not browsing catalog or asking pre-order shipping policy.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if is_pre_order_shipping_inquiry(raw):
        return False
    norm = _norm_ar(raw)
    if _PURE_BROWSE_RE.search(norm):
        return False
    if any(phrase in norm for phrase in _EXPLICIT_TRACKING_PHRASES):
        return True
    if _PAST_ORDER_TRACKING_RE.search(norm):
        return True
    if _PLACED_ORDER_DELIVERY_RE.search(norm):
        return True
    if _ORDER_ANCHOR_RE.search(norm) and _STATUS_QUESTION_RE.search(norm):
        return True
    return False


def boost_track_order_intent(
    message: str,
    rule_intent: Optional[Intent] = None,
) -> Optional[Intent]:
    """Return a high-confidence track_order intent when guard fires."""
    if not is_order_tracking_follow_up(message):
        return None
    if rule_intent and rule_intent.name == INTENT_TRACK_ORDER:
        return rule_intent
    slots = dict(getattr(rule_intent, "slots", None) or {})
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.97,
        slots=slots,
        raw_message=message,
        extraction_method="order_tracking_guard",
    )


def resolve_order_tracking_guard_reply(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
) -> str:
    """
    Layer 3: honest tracking reply or ask for order number / phone.
    """
    from core.order_creation_evidence import resolve_track_order_fallback  # noqa: PLC0415
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    fallback = resolve_track_order_fallback(state=state, history=history)
    if fallback:
        return fallback
    return T.track_order_need_identifiers()


__all__ = [
    "boost_track_order_intent",
    "is_order_tracking_follow_up",
    "is_pre_order_shipping_inquiry",
    "resolve_order_tracking_guard_reply",
]
