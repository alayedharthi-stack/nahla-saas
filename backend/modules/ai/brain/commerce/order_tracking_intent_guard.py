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

# General shipping-duration asks — stay ask_shipping unless order evidence exists.
_GENERAL_SHIPPING_DURATION_RE = re.compile(
    r"(?:"
    r"^متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:\s*[\?؟!.]*)?$|"
    r"^متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:يه)?(?:\s*[\?؟!.]*)?$|"
    r"متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:يه)?\s+(?:"
    r"اذا|إذا|لو|عاده|عادة|غالبا|غالباً|عاده|"
    r"لل(?:رياض|جده|جدة|دمام|طائف|مدين|مكه|احساء)|"
    r"بعد\s+(?:ال)?(?:طلب|طلبيه)|اليوم|الحين|الان|الآن"
    r")"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Layer 2 — phrases that must NEVER become catalog product labels.
# Independent of intent routing (ask_shipping vs track_order).
_SHIPPING_TRACKING_NON_PRODUCT_PHRASES_RAW = (
    "متى يوصل الطلب",
    "متى توصل الطلب",
    "متى يجي الطلب",
    "متى توصل الطلبية",
    "وين طلبي",
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
_SHIPPING_TRACKING_NON_PRODUCT_PHRASES = tuple(
    _norm_ar(p) for p in _SHIPPING_TRACKING_NON_PRODUCT_PHRASES_RAW
)

# Strong existing-order follow-up markers — Layer 1 track_order routing.
_EXPLICIT_TRACKING_PHRASES_RAW = (
    "وين طلبي",
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

_EXISTING_ORDER_MESSAGE_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|"
    r"عندي\s+طلب|"
    r"طلبت\s+(?:قبل|امس|البارح|الاحد|يوم|اسبوع|اسبوعين)|"
    r"سويت\s*طلب|عملت\s*طلب|قدمت\s*طلب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_ANCHOR_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|الشحنه|الشحنة|"
    r"رقم\s*الطلب|رقم\s*التتبع|رابط\s*التتبع|التتبع|tracking"
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


# Post-order shipping policy / carrier questions — defer to brain (ACTION_LLM_REPLY).
_POST_ORDER_SHIPPING_BRAIN_DEFER_RE = re.compile(
    r"(?:"
    r"(?:اي|ايه|أي|which)\s*(?:فرع|branch)|"
    r"(?:سمسا|smsa|aramex|ارامكس|ارامex|\bdhl\b)|"
    r"(?:ارسل|أرسل|رسل|شحن|شحنت).{0,30}(?:فرع|branch|شركة|carrier)|"
    r"(?:فرع|branch).{0,20}(?:سمسا|smsa|aramex|ارامكس)|"
    r"بكم\s*(?:ال)?(?:شحن|توصيل)|"
    r"(?:مدة|كم\s+يوم).{0,15}(?:ال)?(?:شحن|توصيل)|"
    r"(?:وش|كيف|شلون).{0,15}(?:ال)?(?:شحن|توصيل|توصل)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_general_shipping_duration_inquiry(message: str) -> bool:
    """Policy shipping timing — e.g. bare «متى يوصل الطلب» without order context."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return bool(_GENERAL_SHIPPING_DURATION_RE.search(norm))


def is_shipping_tracking_non_product_label(message: str) -> bool:
    """
    Layer 2 core — shipping/tracking inbound must never become a product label.

    Applies regardless of upstream intent (ask_shipping, ask_product, etc.).
    """
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if any(phrase in norm for phrase in _SHIPPING_TRACKING_NON_PRODUCT_PHRASES):
        return True
    if is_general_shipping_duration_inquiry(message):
        return True
    if _ORDER_ANCHOR_RE.search(norm) and _STATUS_QUESTION_RE.search(norm):
        return True
    if _EXISTING_ORDER_MESSAGE_RE.search(norm):
        return True
    if _PAST_ORDER_TRACKING_RE.search(norm):
        return True
    if _PLACED_ORDER_DELIVERY_RE.search(norm):
        return True
    return False


def has_existing_order_evidence(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """True when persisted/session evidence shows the customer has an active order."""
    try:
        from core.order_creation_evidence import (  # noqa: PLC0415
            recent_outbound_claims_order_creating,
            resolve_order_creation_evidence,
        )

        evidence = resolve_order_creation_evidence(state=state)
        if evidence.can_claim_created() or evidence.can_claim_creating():
            return True
        if recent_outbound_claims_order_creating(history):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence scan is best-effort
        pass

    if state is not None:
        if str(getattr(state, "draft_order_id", "") or "").strip():
            return True
        op = getattr(state, "order_prep", None)
        if op is not None:
            if str(getattr(op, "salla_order_id", "") or "").strip():
                return True
            if str(getattr(op, "order_status", "") or "").strip():
                return True
            if getattr(op, "payment_receipt_received", False):
                return True

    bundle = commerce_bundle if isinstance(commerce_bundle, dict) else {}
    ctx_obj = bundle.get("active_order_context") or {}
    if isinstance(ctx_obj, dict) and any(
        str(ctx_obj.get(k) or "").strip()
        for k in ("order_id", "salla_order_id", "reference", "tracking_number")
    ):
        return True
    return False


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


def is_order_tracking_follow_up(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """
    True when the customer is asking about an existing order/shipment,
    not browsing catalog or asking general shipping policy.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if is_pre_order_shipping_inquiry(raw):
        return False
    norm = _norm_ar(raw)
    if _PURE_BROWSE_RE.search(norm):
        return False

    order_evidence = has_existing_order_evidence(
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    )

    if is_general_shipping_duration_inquiry(raw):
        return order_evidence

    if any(phrase in norm for phrase in _EXPLICIT_TRACKING_PHRASES):
        return True
    if _EXISTING_ORDER_MESSAGE_RE.search(norm):
        return True
    if _PAST_ORDER_TRACKING_RE.search(norm):
        return True
    if _PLACED_ORDER_DELIVERY_RE.search(norm):
        return True
    if _ORDER_ANCHOR_RE.search(norm) and _STATUS_QUESTION_RE.search(norm):
        return True
    return False


def is_post_order_shipping_brain_defer(message: str) -> bool:
    """Paid/post-order shipping policy questions — brain path, not ACTION_TRACK_ORDER."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return bool(_POST_ORDER_SHIPPING_BRAIN_DEFER_RE.search(norm))


def is_explicit_order_tracking_request(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """
    Layer 1 routing — only explicit tracking follow-ups become track_order.

    Excludes general shipping duration and post-order carrier/policy asks that
    the decision engine defers to ACTION_LLM_REPLY with order context.
    """
    if not is_order_tracking_follow_up(
        message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return False
    if is_general_shipping_duration_inquiry(message):
        return False
    if is_post_order_shipping_brain_defer(message):
        return False
    return True


def should_exempt_from_availability_rewrite(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """
    Block availability rewrites for shipping/tracking inbound.

    Layer 2 is independent of track_order routing — even ask_shipping turns
    must not produce «متوفر متى يوصل الطلب بعدة خيارات».
    """
    _ = (state, history, commerce_bundle)  # reserved for future contextual exempt
    return is_shipping_tracking_non_product_label(message)


def boost_track_order_intent(
    message: str,
    rule_intent: Optional[Intent] = None,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> Optional[Intent]:
    """Return a high-confidence track_order intent when guard fires."""
    if not is_explicit_order_tracking_request(
        message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
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
    "has_existing_order_evidence",
    "is_explicit_order_tracking_request",
    "is_general_shipping_duration_inquiry",
    "is_order_tracking_follow_up",
    "is_post_order_shipping_brain_defer",
    "is_pre_order_shipping_inquiry",
    "is_shipping_tracking_non_product_label",
    "resolve_order_tracking_guard_reply",
    "should_exempt_from_availability_rewrite",
]
