"""OrderFlowV2 triggers — when checkout starts vs stays in inquiry."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_GREETING_RE = re.compile(
    r"^(?:"
    r"السلام\s*عليكم|سلام\s*عليكم|مرحبا|مرحب(?:اً|ا)|"
    r"هلا|أهلا|اهلا|صباح\s*الخير|مساء\s*الخير|"
    r"السلام\s*عليكم\s*ورحمة\s*الله|"
    r"hi\b|hello\b|hey\b|good\s*(?:morning|evening)"
    r")\s*[!.؟?]*\s*$",
    re.I | re.UNICODE,
)

_INQUIRY_RE = re.compile(
    r"(?:"
    r"استفسر|استفسار|س(?:ؤ|و)ال|"
    r"كم\s*(?:سعر|ب(?:ك|ق)م)|ب(?:ك|ق)م|"
    r"متوفر|موجود|متاح|"
    r"وش\s*(?:ال)?(?:أ?نواع|انواع|فرق|الفرق)|"
    r"وش\s*(?:ال)?(?:أ?حجام|احجام|مقاسات)|"
    r"ما\s*(?:هو|هي)\s*(?:ال)?(?:فرق|أ?نواع)|"
    r"(?:وصف|تفاصيل|مكونات|فوائد)\s*(?:ال)?(?:منتج|عسل|منتجات)?|"
    r"price|available|availability|compare|difference|description|details"
    r")",
    re.I | re.UNICODE,
)
_BROWSE_ESCAPE_RE = re.compile(
    r"(?:"
    r"وش\s*(?:عندكم|المتوفر|الموجود)\s*(?:من\s*)?(?:منتجات|عسل|أ?نواع|احجام|أ?حجام)?|"
    r"(?:وش|ايش|ما)\s*(?:ال)?(?:منتجات|كتالوج|أ?نواع|انواع|أ?حجام|احجام|مقاسات)\s*(?:المتوفر(?:ة)?|عندكم)?|"
    r"(?:اعرض|ورني|ارسل|أرسل)\s*(?:لي\s*)?(?:ال)?(?:منتجات|كتالوج|أ?نواع|احجام|أ?حجام)|"
    r"(?:تصفح|استعراض|اشوف|أشوف)\s*(?:ال)?(?:منتجات|كتالوج|أ?نواع)|"
    r"products|catalog|browse|sizes|variants"
    r")",
    re.I | re.UNICODE,
)

_EXPLICIT_PURCHASE_RE = re.compile(
    r"(?:"
    r"أ?بي\s*أ?طلب|اب(?:ي|غ(?:ى|a)?)\s*أ?طلب|"
    r"أ?ض(?:ف|يف)|اض(?:ف|يف)|"
    r"خ(?:ذ|ذ)\s*لي|خذ\s*لي|"
    r"اعتمد|جهز\s*لي|"
    r"أ?بي\s*واحد\s*من|اب(?:ي|غ(?:ى|a)?)\s*واحد\s*من|"
    r"أ?بي\s*أ?طلب\s*(?:ال)?منتج|"
    r"^\s*(?:اطلب|أطلب|طلب|اشتري|أشتري)\s*$|"
    r"order\s+now|buy\s+now|add\s+to\s+cart|checkout"
    r")",
    re.I | re.UNICODE,
)

_RESUME_RE = re.compile(
    r"(?:"
    r"ك(?:م|مل)\s*(?:ال)?طلب|أ?ك(?:م|مل)\s*(?:ال)?طلب|"
    r"ن(?:ك|كمل)\s*(?:ال)?طلب|"
    r"resume\s*order|continue\s*order|complete\s*order"
    r")",
    re.I | re.UNICODE,
)

_ORDER_NUMBER_QUESTION_RE = re.compile(
    r"(?:"
    r"كم\s*رقم\s*(?:ال)?طلب"
    r"|(?:وين|اين)\s*رقم\s*(?:ال)?طلب"
    r"|رقم\s*(?:ال)?طلب\s*(?:كم|؟|\?)"
    r")",
    re.I | re.UNICODE,
)

_PRELIMINARY_ORDER_REF_RE = re.compile(
    r"(?:"
    r"الطلب\s*(?:ال)?(?:لي\s*)?(?:"
    r"انش(?:أ|ا|ئ)ت(?:ه|مو)|سجل(?:ت|مو)|جهز(?:ت|مو)|"
    r"مبد(?:أ|ئ)(?:ي(?:ً|ا)?)?"
    r")"
    r")",
    re.I | re.UNICODE,
)

_WHATSAPP_CHECKOUT_CHANNELS = frozenset({
    "whatsapp_fast",
    "whatsapp_quick_order",
    "whatsapp_catalog",
})

_CATALOG_ORDER_SOURCE = frozenset({"catalog_order", "native_catalog_order"})


def _norm(text: str) -> str:
    t = _NORM_RE.sub("", str(text or ""))
    return _WS_RE.sub(" ", t).strip()


def is_greeting_message(message: str) -> bool:
    return bool(_GREETING_RE.match(_norm(message)))


def is_inquiry_message(message: str) -> bool:
    text = _norm(message)
    if not text:
        return False
    if is_explicit_purchase_intent(text):
        return False
    return bool(_INQUIRY_RE.search(text))


def is_explicit_purchase_intent(message: str) -> bool:
    return bool(_EXPLICIT_PURCHASE_RE.search(_norm(message)))


def is_resume_order_command(message: str) -> bool:
    return bool(_RESUME_RE.search(_norm(message)))


def is_catalog_order_inbound(
    inbound_metadata: Optional[Dict[str, Any]],
    message: str = "",
) -> bool:
    meta = dict(inbound_metadata or {})
    source = str(
        meta.get("source_type")
        or meta.get("inbound_source")
        or meta.get("normalized_type")
        or ""
    ).strip().lower()
    if source in _CATALOG_ORDER_SOURCE:
        return bool(meta.get("product_items") or meta.get("order") or meta.get("catalog_order"))
    if source == "order":
        order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
        return bool(meta.get("product_items") or order.get("product_items"))
    if meta.get("catalog_order_submitted"):
        return True
    if "[طلب كتالوج من العميل]" in str(message or ""):
        return bool(
            meta.get("product_items")
            or meta.get("order")
            or "رمز المنتج (SKU):" in str(message or "")
        )
    return False


def is_catalog_sent_only(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(inbound_metadata or {})
    if is_catalog_order_inbound(meta):
        return False
    return bool(meta.get("native_catalog_sent") or meta.get("catalog_sent"))


def should_not_start_checkout(message: str, inbound_metadata: Optional[Dict[str, Any]] = None) -> bool:
    if is_greeting_message(message):
        return True
    if is_checkout_escape_inquiry(message, inbound_metadata):
        return True
    if is_catalog_sent_only(inbound_metadata):
        return True
    return False


def is_checkout_escape_inquiry(
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Current-turn inquiry/browse evidence that must not inherit checkout."""
    meta = dict(inbound_metadata or {})
    text = _norm(message)
    if not text:
        return False
    if is_catalog_order_inbound(meta, text):
        return False
    if is_explicit_purchase_intent(text) or is_resume_order_command(text):
        return False
    return bool(is_inquiry_message(text) or _BROWSE_ESCAPE_RE.search(text))


def is_order_number_question(message: str) -> bool:
    return bool(_ORDER_NUMBER_QUESTION_RE.search(_norm(message)))


def is_preliminary_order_reference(message: str) -> bool:
    return bool(_PRELIMINARY_ORDER_REF_RE.search(_norm(message)))


def is_checkout_order_number_intent(message: str) -> bool:
    return is_order_number_question(message) or is_preliminary_order_reference(message)


def is_whatsapp_order_browse_context(
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the customer is inside WhatsApp quick-order / catalog browsing."""
    prep = dict(order_prep or {})
    bs = dict(brain_state or {})
    meta = dict(inbound_metadata or {})
    channel = str(prep.get("checkout_channel") or "").strip().lower()
    if channel in _WHATSAPP_CHECKOUT_CHANNELS:
        return True
    if prep.get("awaiting_checkout_channel"):
        return False
    if meta.get("native_catalog_sent") or prep.get("catalog_sent"):
        return True
    from .state import in_flight_catalog_checkout, pending_order_exists  # noqa: PLC0415

    if in_flight_catalog_checkout(prep, bs):
        return True
    return pending_order_exists(prep, bs)


def is_short_product_keyword_in_order_flow(message: str) -> bool:
    """Short product-like token during order flow — not social/greeting."""
    text = _norm(message)
    if not text or len(text) > 16:
        return False
    if is_greeting_message(message):
        return False
    if is_explicit_purchase_intent(text) or is_resume_order_command(text):
        return False
    if is_checkout_order_number_intent(text):
        return False
    try:
        from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: PLC0415

        if is_address_on_file_claim(message):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — address-on-file probe must not block product routing
        pass
    if is_checkout_escape_inquiry(text):
        return False
    try:
        from core.dedup_order_state_gate import inbound_is_short_product_inquiry  # noqa: PLC0415

        return inbound_is_short_product_inquiry(message)
    except Exception:  # noqa: BLE001
        return bool(re.search(r"[\u0600-\u06FFa-z]", text))
