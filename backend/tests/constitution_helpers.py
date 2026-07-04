"""Shared helpers for merchant assistant constitution regression tests."""
from __future__ import annotations

import re
import unicodedata
from typing import FrozenSet, Sequence

# Policy §6 / §13 — generic ungrounded line-item placeholders.
GENERIC_PLACEHOLDER_PRODUCT_NAMES: FrozenSet[str] = frozenset(
    {
        "منتج",
        "product",
        "item",
        "شيء",
        "شي",
        "غير محدد",
        "المطلوب",
    }
)

# Policy §11 — support-bot / template-engine openers to flag in anti-template tests.
CONSTITUTION_BANNED_CUSTOMER_OPENERS: FrozenSet[str] = frozenset(
    {
        "أكيد 🌷 تفضل",
        "كيف أقدر أساعدك اليوم؟",
        "تم استلام رسالتك",
    }
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")


def _norm_name(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).strip().lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def is_generic_placeholder_product_name(name: str) -> bool:
    """True when line-item name is an ungrounded placeholder per policy §6."""
    return _norm_name(name) in {_norm_name(x) for x in GENERIC_PLACEHOLDER_PRODUCT_NAMES}


def line_items_contain_only_generic_placeholders(
    line_items: Sequence[dict],
) -> bool:
    if not line_items:
        return False
    for item in line_items:
        if not isinstance(item, dict):
            return False
        name = str(
            item.get("product_name")
            or item.get("title")
            or item.get("name")
            or ""
        ).strip()
        pid = item.get("product_id") or item.get("sku") or item.get("external_id")
        if pid and not is_generic_placeholder_product_name(name):
            return False
        if not is_generic_placeholder_product_name(name):
            return False
    return True


def contains_banned_template_opener(text: str) -> bool:
    raw = str(text or "")
    for phrase in CONSTITUTION_BANNED_CUSTOMER_OPENERS:
        if phrase in raw:
            return True
    return False


def looks_like_invented_payment_credential(text: str) -> bool:
    """Heuristic: IBAN or payment URL in outbound (policy F)."""
    if not text:
        return False
    if re.search(r"\bSA\d{22}\b", text, re.I):
        return True
    if re.search(
        r"https?://[^\s]+(?:pay|payment|checkout|moyasar|stripe|tap)[^\s]*",
        text,
        re.I,
    ):
        return True
    return False
