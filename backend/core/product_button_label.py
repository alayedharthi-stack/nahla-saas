"""
core/product_button_label.py
────────────────────────────
Compact WhatsApp quick-reply button titles for catalog products.

Meta caps reply-button titles at 20 characters. Long catalog titles
truncate mid-word and read poorly — this helper keeps identity tokens
plus weight/year when present, and drops filler. Labels are derived
only from the product/collection title string (no domain prefix).
"""
from __future__ import annotations

import re
from typing import List

WA_REPLY_BUTTON_TITLE_MAX = 20

_YEAR_RE = re.compile(r"\b(14\d{2}|20\d{2})\b")

_WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(?:"
    r"\u0643\u064a\u0644\u0648|\u0643\u064a\u0644\u0648|\u0643\u062c\u0645|\u0643\u062c|\u0643\u064a\u0644|\u0643\u064a\u0644o|"
    r"\u062c\u0631\u0627\u0645|\u062c\u0645|"
    r"kg|g"
    r")\b",
    re.IGNORECASE,
)

_KILO_UNITS = frozenset({
    "كilo", "كيلo", "كيلو", "كيلو", "كجم", "كج", "كيل", "kg",
})

# Catalog filler tokens — safe to drop from button labels (not SKU identity).
_BUTTON_NOISE = frozenset({
    "إنتاج", "انتاج", "منحلنا", "مناحلنا", "مناحل", "منحل",
    "وزن", "سطل", "براري", "بري", "بلدي", "جبلي", "production",
    "the", "and", "for", "with",
})


def _normalize(text: str) -> str:
    from modules.ai.knowledge.product_matcher import normalize_arabic  # noqa: PLC0415

    return normalize_arabic(text or "")


def _tokenize(text: str) -> List[str]:
    from modules.ai.knowledge.product_matcher import tokenize  # noqa: PLC0415

    return tokenize(_normalize(text))


def _extract_weight_label(title: str) -> str:
    m = _WEIGHT_RE.search(_normalize(title))
    if not m:
        return ""
    raw_num = m.group(1).replace(",", ".")
    try:
        num_f = float(raw_num)
        num_s = str(int(num_f)) if num_f.is_integer() else raw_num
    except ValueError:
        num_s = raw_num
    unit = m.group(0).split()[-1].lower() if m.group(0) else ""
    unit_norm = _normalize(unit)
    if unit_norm in _KILO_UNITS or unit_norm.startswith("ك"):
        return f"{num_s} كجم"
    return f"{num_s} جم"


def _extract_year_label(title: str) -> str:
    years = _YEAR_RE.findall(title or "")
    return years[0] if years else ""


def _strip_weight_and_year(text: str) -> str:
    t = _YEAR_RE.sub(" ", text or "")
    t = _WEIGHT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def compact_whatsapp_product_button_title(
    title: str,
    *,
    max_len: int = WA_REPLY_BUTTON_TITLE_MAX,
) -> str:
    """
    Build a short, readable WA reply-button label from a catalog title.

    Price is intentionally omitted — it belongs in the message body/card.
    No category/domain prefix is invented; tokens come from ``title`` only.
    """
    raw = (title or "").strip()
    if not raw:
        return ""
    limit = max(8, min(int(max_len or WA_REPLY_BUTTON_TITLE_MAX), 20))

    weight = _extract_weight_label(raw)
    year = _extract_year_label(raw)

    stripped = _strip_weight_and_year(raw)
    tokens = [
        t for t in _tokenize(stripped)
        if len(t) >= 2 and t not in _BUTTON_NOISE
    ]

    suffix_parts: List[str] = []
    if weight:
        suffix_parts.append(weight)
    elif year:
        suffix_parts.append(year)
    suffix = " ".join(suffix_parts)
    suffix_budget = len(suffix) + (1 if suffix else 0)

    name_parts: List[str] = []
    used = 0
    name_budget = limit - suffix_budget
    for tok in tokens[:5]:
        add = len(tok) + (1 if name_parts else 0)
        if used + add > name_budget:
            break
        name_parts.append(tok)
        used += add

    if not name_parts:
        fallback = re.sub(r"\s+", " ", stripped).strip() or raw
        label = fallback[:limit]
    else:
        label = " ".join(name_parts)

    if suffix:
        combined = f"{label} {suffix}".strip()
        if len(combined) <= limit:
            label = combined
        else:
            trim_budget = limit - len(suffix) - 1
            label = f"{label[:max(trim_budget, 4)].rstrip()} {suffix}".strip()

    return label[:limit]


__all__ = [
    "WA_REPLY_BUTTON_TITLE_MAX",
    "compact_whatsapp_product_button_title",
]
