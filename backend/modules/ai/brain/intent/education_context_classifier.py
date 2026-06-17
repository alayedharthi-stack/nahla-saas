"""
education_context_classifier.py
───────────────────────────────
Detect teacher/student / classroom messages that must not route to
product availability or catalog commerce.

Platform-wide — not merchant-specific. Blocks commerce when education
signals dominate and no explicit buying / product intent is present.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from .non_commerce_classifier import has_product_commerce_signal
from .social_classifier import _norm

NC_EDUCATION = "education_context"

_STRONG_EDUCATION_MARKERS: Tuple[str, ...] = (
    "استاذ",
    "أستاذ",
    "استاد",
    "معلم",
    "معلمه",
    "مدرس",
    "مدرسه",
    "مدرسة",
    "طلاب",
    "طالب",
    "طالبه",
    "طالبة",
    "منهج",
    "اختبار",
    "امتحان",
    "واجب",
    "واجبات",
    "دروس",
    "ماده",
    "مادة",
    "مواد",
    "فصل",
    "جامعه",
    "جامعة",
    "كلية",
    "محاضره",
    "محاضرة",
    "teacher",
    "student",
    "classroom",
    "homework",
    "curriculum",
)

_EXPLICIT_EDUCATION_RE = re.compile(
    r"(?:"
    r"استاذ|استاد|معلم|مدرس|طلاب|طالب|منهج|اختبار|امتحان|واجب|"
    r"محاضره|محاضرة|teacher|student|homework|curriculum"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MEDIA_FRAMING_RE = re.compile(
    r"(?:\[(?:وصف|تصنيف)\s*(?:ال)?(?:صور|فيديو|وسائط|ستيكر))",
    re.UNICODE | re.IGNORECASE,
)

_WEAK_EDUCATION_MARKERS: Tuple[str, ...] = (
    "تحديد",
)

_LOCATION_DISAMBIG_RE = re.compile(
    r"(?:"
    r"تحديد\s*(?:ال)?(?:موقع|عنوان|مكان|فرع|محل)|"
    r"(?:موقع|عنوان|gps|google\s*maps|العنوان\s*الوطني)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_STORE_OFFER_RE = re.compile(
    r"(?:"
    r"فيه\s+عرض|هل\s+فيه\s+عرض|عروض|تخفيض|خصم|"
    r"offer|discount|sale"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COMMERCE_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"(?:هل\s+)?(?:ال)?(?:منتج|منتجات)\s*(?:متوفر|متاح|موجود)|"
    r"(?:عندكم|عندك|لديكم|متوفر|موجود)\s+\S|"
    r"فيه\s+(?:عسل|منتج|بضاع|سلعه|صنف|موديل|نوع)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class EducationContextMatch:
    category: str = NC_EDUCATION
    confidence: float = 0.96
    source: str = "text"
    topic: str = "study_material"

    @property
    def social_category(self) -> str:
        return self.category


def _normalize(text: str) -> str:
    return _norm(text or "")


def _marker_hits(norm: str, markers: Sequence[str]) -> int:
    hits = 0
    for marker in markers:
        m = _normalize(marker)
        if not m:
            continue
        if m == "exam":
            if re.search(r"(?<![a-z])exam(?![a-z])", norm):
                hits += 1
            continue
        if len(m) <= 4 and not m.isascii():
            if re.search(rf"(?<![ء-ي]){re.escape(m)}(?![ء-ي])", norm):
                hits += 1
            continue
        if m in norm:
            hits += 1
    return hits


def _has_location_disambiguation(norm: str) -> bool:
    return bool(_LOCATION_DISAMBIG_RE.search(norm))


def _weak_education_signal(norm: str, strong_hits: int) -> bool:
    if "تحديد" not in norm:
        return False
    if _has_location_disambiguation(norm):
        return False
    if strong_hits >= 1:
        return True
    if "المنهج" in norm or "ماده" in norm or "مادة" in norm:
        return True
    return False


def classify_education_context(message: str) -> Optional[EducationContextMatch]:
    """Return match when inbound is education/study context without commerce."""
    raw = (message or "").strip()
    if not raw:
        return None

    norm = _normalize(raw)
    if not norm:
        return None

    if _COMMERCE_AVAILABILITY_RE.search(norm):
        return None
    if _STORE_OFFER_RE.search(norm) and not _marker_hits(norm, _STRONG_EDUCATION_MARKERS):
        return None
    if has_product_commerce_signal(raw):
        return None

    if _MEDIA_FRAMING_RE.search(raw) and not _EXPLICIT_EDUCATION_RE.search(norm):
        return None

    strong_hits = _marker_hits(norm, _STRONG_EDUCATION_MARKERS)
    weak_hit = _weak_education_signal(norm, strong_hits)

    if strong_hits == 0 and not weak_hit:
        return None

    topic = "study_material"
    if any(t in norm for t in ("اختبار", "امتحان", "exam")):
        topic = "exam"
    elif any(t in norm for t in ("واجب", "homework")):
        topic = "homework"
    elif any(t in norm for t in ("منهج", "curriculum")):
        topic = "curriculum"

    return EducationContextMatch(topic=topic)


def is_education_non_commerce_context(message: str) -> bool:
    return classify_education_context(message) is not None


def education_clarify_reply(message: str) -> str:
    """Safe operational stub — persona may warm later."""
    norm = _normalize(message or "")
    if norm.startswith("السلام") or "سلام عليكم" in norm:
        return (
            "وعليكم السلام، حياك الله. "
            "تقصد تحديد أي مادة أو اختبار؟"
        )
    return "حياك الله. تقصد تحديد أي مادة أو اختبار؟"


__all__ = [
    "EducationContextMatch",
    "NC_EDUCATION",
    "classify_education_context",
    "education_clarify_reply",
    "is_education_non_commerce_context",
]
