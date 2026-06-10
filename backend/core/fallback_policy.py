"""
core/fallback_policy.py
─────────────────────────
Platform-wide fallback policy (P1-D-1).

Operational replies may be deterministic; personality / CS closers must not
come from static template pools.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Tuple

# Honest system fallback when compose returns empty — not a CS opener.
EMPTY_REPLY_OPERATIONAL_AR = (
    "وصلت رسالتك. جاري المعالجة — إذا تأخر الرد أعد المحاولة."
)

# Last-resort compose error — no product/sales CTA.
OPERATIONAL_COMPOSE_ERROR_AR = (
    "تعذّرت معالجة الطلب الآن. أعد رسالتك لو يناسبك."
)

# Banned rotating dedup / personality pool markers (normalized matching).
_PERSONALITY_FALLBACK_MARKERS: Tuple[str, ...] = (
    "اي نقطه تحب توضحها",
    "أي نقطة تحب أوضحها",
    "وش الجزء اللي تبيني أوضحه",
    "هذي نفس الاجابه قبل قليل",
    "ذكرت لك للتو نفس النقطه",
)

_SERVICE_CLOSER_MARKERS: Tuple[str, ...] = (
    "كيف اقدر اساعدك",
    "كيف أقدر أساعدك",
    "كيف اقدر اخدمك",
    "كيف أقدر أخدمك",
    "انا هنا للمساعده",
    "أنا هنا للمساعدة",
    "انا هنا لمساعدتك",
    "أنا هنا لمساعدتك",
    "اذا تحتاج اي تفاصيل",
    "إذا تحتاج أي تفاصيل",
    "اذا احتجت اي مساعده",
    "إذا احتجت أي مساعدة",
    "خبرني كيف اساعدك",
    "تحت امرك",
    "تحت أمرك",
    "أقدر أساعدك أكثر",
    "اقدر اساعدك اكثر",
    "لأساعدك أكثر",
)

_SALES_CLOSER_MARKERS: Tuple[str, ...] = (
    "المنتجات او الاسعار",
    "المنتجات أو الأسعار",
    "يمكنني مساعدتك في البحث",
    "ابحث عن منتج",
    "ابحث عن منتج أو",
    "تبحث عن منتج",
    "مساعدة في طلب",
    "انشاء طلب",
    "إنشاء طلب",
    "عرض لك المنتجات",
    "تحب أعرض لك",
)


def _normalize_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def empty_reply_fallback() -> str:
    return EMPTY_REPLY_OPERATIONAL_AR


def operational_compose_error_fallback(*, variant: int = 0) -> str:
    del variant  # single honest line — no rotation pool
    return OPERATIONAL_COMPOSE_ERROR_AR


def is_personality_fallback_text(text: str) -> bool:
    norm = _normalize_ar(text)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in _PERSONALITY_FALLBACK_MARKERS)


def contains_service_closer(text: str) -> bool:
    norm = _normalize_ar(text)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in _SERVICE_CLOSER_MARKERS)


def contains_sales_closer(text: str) -> bool:
    norm = _normalize_ar(text)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in _SALES_CLOSER_MARKERS)


def _segment_matches_markers(segment: str, *, include_sales: bool) -> bool:
    norm = _normalize_ar(segment)
    if not norm:
        return False
    if any(m in norm for m in _SERVICE_CLOSER_MARKERS):
        return True
    if include_sales and any(m in norm for m in _SALES_CLOSER_MARKERS):
        return True
    if is_personality_fallback_text(segment):
        return True
    return False


def strip_closer_segments(text: str, *, non_commerce: bool = False) -> Tuple[str, bool]:
    """Remove paragraph/line segments that match CS or (on non-commerce) sales closers."""
    raw = (text or "").strip()
    if not raw:
        return "", False

    include_sales = bool(non_commerce)
    stripped_any = False
    kept_paragraphs: list[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        if _segment_matches_markers(p, include_sales=include_sales):
            stripped_any = True
            continue
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        kept_lines = [
            ln for ln in lines
            if not _segment_matches_markers(ln, include_sales=include_sales)
        ]
        if len(kept_lines) < len(lines):
            stripped_any = True
        if kept_lines:
            kept_paragraphs.append("\n".join(kept_lines))

    return "\n\n".join(kept_paragraphs).strip(), stripped_any


__all__ = [
    "EMPTY_REPLY_OPERATIONAL_AR",
    "OPERATIONAL_COMPOSE_ERROR_AR",
    "contains_sales_closer",
    "contains_service_closer",
    "empty_reply_fallback",
    "is_personality_fallback_text",
    "operational_compose_error_fallback",
    "strip_closer_segments",
]
