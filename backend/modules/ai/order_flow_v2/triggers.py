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
    r"ما\s*(?:هو|هي)\s*(?:ال)?(?:فرق|أ?نواع)|"
    r"price|available|availability|compare|difference"
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


def is_catalog_order_inbound(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(inbound_metadata or {})
    source = str(meta.get("source_type") or meta.get("inbound_source") or "").strip().lower()
    if source in _CATALOG_ORDER_SOURCE:
        return bool(meta.get("product_items") or meta.get("order") or meta.get("catalog_order"))
    if meta.get("catalog_order_submitted"):
        return True
    return False


def is_catalog_sent_only(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(inbound_metadata or {})
    if is_catalog_order_inbound(meta):
        return False
    return bool(meta.get("native_catalog_sent") or meta.get("catalog_sent"))


def should_not_start_checkout(message: str, inbound_metadata: Optional[Dict[str, Any]] = None) -> bool:
    if is_greeting_message(message):
        return True
    if is_inquiry_message(message):
        return True
    if is_catalog_sent_only(inbound_metadata):
        return True
    return False
