"""
commerce_followup_policy.py
───────────────────────────
Separate availability / options-list / quantity follow-up contexts.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

FollowupKind = Literal[
    "availability",
    "options_list",
    "quantity",
    "price",
    "general",
]

_NORM_RE = re.compile(r"[\u064B-\u065F\u0670]")

_OPTIONS_REQUEST_RE = re.compile(
    r"(?:"
    r"\b(?:الخيارات|خيارات|options|choices|variants|الأنواع|انواع|types|"
    r"المتوفر|what(?:'s)?\s+available)\b"
    r"|"
    r"(?:أ?رسل|ارسل|send|show|list|اعرض|عرض)\s*(?:لي\s+)?(?:ال)?(?:خيارات|options|choices|أنواع|types|"
    r"المتوفر|available|catalog|details|التفاصيل)"
    r"|"
    r"^(?:وش|what)\s+(?:ال)?(?:خيارات|options|choices|أنواع|types|المتوفر|available)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_QUANTITY_REQUEST_RE = re.compile(
    r"(?:"
    r"\b(?:كم\s+(?:عدد|quantity|qty|حبة|علبة|كرتون|piece|pieces)|"
    r"how many|quantity)\b"
    r"|"
    r"^(?:كم\s+)?(?:عدد|quantity)\s+(?:تحتاج|تبغى|want|need)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_REQUEST_RE = re.compile(
    r"(?:\b(?:بكم|كم\s+سعر|price|pricing|cost)\b|^(?:how much))",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_REQUEST_RE = re.compile(
    r"(?:\b(?:متوفر|متاح|available|in stock|عندكم|عندك|لديكم|do you have)\b|"
    r"^(?:هل\s+)?(?:عندكم|عندك|do you have))",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    raw = unicodedata.normalize("NFKC", (text or "").strip())
    raw = _NORM_RE.sub("", raw)
    return re.sub(r"\s+", " ", raw).strip()


def classify_commerce_request_kind(inbound_text: str) -> FollowupKind:
    text = _norm(inbound_text)
    if not text:
        return "general"
    if _OPTIONS_REQUEST_RE.search(text):
        return "options_list"
    if _QUANTITY_REQUEST_RE.search(text):
        return "quantity"
    if _PRICE_REQUEST_RE.search(text):
        return "price"
    if _AVAILABILITY_REQUEST_RE.search(text):
        return "availability"
    return "general"


def followup_style_for_request(
    *,
    inbound_text: str,
    category: str,
    seeded_style: str,
) -> str:
    """Map request kind to compositional follow-up style (not a sentence template)."""
    kind = classify_commerce_request_kind(inbound_text)
    if kind == "options_list":
        return "options_list"
    if kind == "quantity":
        return "quantity"
    if kind == "availability":
        return seeded_style if seeded_style != "quantity" else "options"
    return seeded_style


__all__ = [
    "FollowupKind",
    "classify_commerce_request_kind",
    "followup_style_for_request",
]
