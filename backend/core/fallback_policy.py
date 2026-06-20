"""
core/fallback_policy.py
─────────────────────────
Platform-wide fallback policy (P1-D-1 / P1-D-2).

Operational replies may be deterministic; personality / CS closers must not
come from static template pools.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Tuple

# Honest system fallback when compose returns empty — triggers recovery, not ACK stub.
EMPTY_REPLY_OPERATIONAL_AR = (
    "تعذّرت صياغة الرد الآن — أعد رسالتك لو يناسبك."
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
    "إذا تحتاج أي مساعدة",
    "اذا تحتاج مساعده",
    "إذا تحتاج مساعدة",
    "لو تحتاج أي مساعدة",
    "عندك استفسار",
    "عندك اي استفسار",
    "عندك أي استفسار",
    "انا هنا",
    "أنا هنا",
    "انا موجود",
    "أنا موجود",
    "كيف حالك اليوم",
    "كيف حالك",
    "كيف امورك اليوم",
    "كيف أمورك اليوم",
    "كيف امورك",
    "كيف أمورك",
    "وش الخدمه",
    "وش الخدمة",
    "وش اللي تحتاجه",
    "وش تحتاج",
    "انا معك خطوه بخطوه",
    "أنا معك خطوة بخطوة",
    "بالخدمه",
    "بالخدمة",
    "خبرني كيف اساعدك",
    "تحت امرك",
    "تحت أمرك",
    "أقدر أساعدك أكثر",
    "اقدر اساعدك اكثر",
    "لأساعدك أكثر",
    "اكتب استفسارك",
    "اكتب استفسارك هنا",
    "تقدر تكتب استفسارك",
    "تقدر تكتب استفسارك هنا",
    "نخدمك باذن الله",
    "نخدمك بإذن الله",
    "اذا احتجت اي حاجه من المتجر",
    "إذا احتجت أي حاجة من المتجر",
    "تواصل معنا",
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

# Inline CS tail — match from opener to end-of-line (keep phatic prefix).
_INLINE_TAIL_START_RE = re.compile(
    r"(?:[،,.!\s…]|^)"
    r"(?:"
    r"(?:إذا|لو)\s+(?:تحتاج|احتج(?:ت)?)(?:\s+أي|\s+مس)?(?:\s+حاج(?:ة|ه))?"
    r"|(?:إذا|لو)\s+احتج(?:ت)?\s+أي\s+حاج(?:ة|ه)\s+من\s+المتجر"
    r"|عندك(?:\s+أي)?\s+استفسار"
    r"|(?:أنا\s+هنا|أنا\s+موجود)"
    r"|كيف\s+حالك(?:\s+اليوم)?"
    r"|كيف\s+أ?مورك(?:\s+اليوم)?"
    r"|وش\s+(?:الخدمة|الخدمه|لي\s+تحتاج(?:ه)?|تحتاج)"
    r"|أنا\s+معك\s+خطوة\s+بخطوة"
    r"|(?:^|[،.\s])بالخدمة"
    r"|(?:^|[،.\s])بالخدمه"
    r"|تحت\s+أمر(?:ك|كم|كن)"
    r"|(?:^|[،.\s])(?:كيف\s+أ(?:قدر|خدم)|خبرني\s+كيف)"
    r"|نخدمك\s+ب(?:اذ|إذ)ن\s+الله"
    r"|(?:اكت|تكتب)\s+استفسار(?:ك|ات)?(?:\s+هنا)?"
    r")",
    re.UNICODE,
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


# Rebuild normalized marker tuples after _normalize_ar is defined.
_NORM_SERVICE_MARKERS = tuple(_normalize_ar(m) for m in _SERVICE_CLOSER_MARKERS)
_NORM_SALES_MARKERS = tuple(_normalize_ar(m) for m in _SALES_CLOSER_MARKERS)


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
    if any(m in norm for m in _NORM_SERVICE_MARKERS):
        return True
    for line in (text or "").splitlines():
        if _strip_inline_service_tail(line.strip())[1]:
            return True
    return False


def contains_sales_closer(text: str) -> bool:
    norm = _normalize_ar(text)
    if not norm:
        return False
    return any(m in norm for m in _NORM_SALES_MARKERS)


def _segment_matches_markers(segment: str, *, include_sales: bool) -> bool:
    norm = _normalize_ar(segment)
    if not norm:
        return False
    if any(m in norm for m in _NORM_SERVICE_MARKERS):
        return True
    if include_sales and any(m in norm for m in _NORM_SALES_MARKERS):
        return True
    if is_personality_fallback_text(segment):
        return True
    return False


def _strip_inline_service_tail(line: str) -> Tuple[str, bool]:
    """Remove CS-closer tail from a single line, keeping phatic prefix."""
    raw = (line or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    current = raw
    while current:
        match = _INLINE_TAIL_START_RE.search(current)
        if not match:
            norm = _normalize_ar(current)
            if norm in _NORM_SERVICE_MARKERS:
                return "", True
            break

        kept = current[: match.start()].rstrip(" ،,.!…")
        if not kept.strip():
            return "", True
        stripped_any = True
        current = kept.strip()

    return current, stripped_any


def strip_closer_segments(text: str, *, non_commerce: bool = False) -> Tuple[str, bool]:
    """Remove CS closers (inline tails or whole segments/lines)."""
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
            # Try inline tail strip before dropping the whole paragraph.
            inline_lines: list[str] = []
            paragraph_stripped = False
            for ln in p.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                cleaned, did = _strip_inline_service_tail(ln)
                if did:
                    paragraph_stripped = True
                if cleaned and not _segment_matches_markers(
                    cleaned, include_sales=include_sales
                ):
                    inline_lines.append(cleaned)
            if inline_lines:
                if paragraph_stripped:
                    stripped_any = True
                kept_paragraphs.append("\n".join(inline_lines))
                continue
            stripped_any = True
            continue

        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        kept_lines: list[str] = []
        for ln in lines:
            cleaned, did_inline = _strip_inline_service_tail(ln)
            if did_inline:
                stripped_any = True
            candidate = cleaned if did_inline else ln
            if not candidate:
                continue
            if _segment_matches_markers(candidate, include_sales=include_sales):
                stripped_any = True
                continue
            kept_lines.append(candidate)

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
