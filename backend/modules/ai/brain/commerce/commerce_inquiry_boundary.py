"""
Platform-wide browse/availability vs checkout boundary detection.

Deterministic guard — separates inquiry turns from order/select turns without
merchant-specific product hardcoding.
"""
from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Optional

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_BROWSE_TYPES_RE = re.compile(
    r"(?:"
    r"(?:وش|ايش|ايه|ما|ماذا)\s+(?:ال)?(?:انواع|الأنواع|المتوفر|الخيارات|المنتجات|العطور|الاقسام|الأقسام)"
    r"|(?:وش|ايش|ايه)\s+(?:ال)?[\w\u0600-\u06FF]{2,30}\s*(?:الرجال(?:ية|يه)?|النسائ(?:ية|يه)?|المتوفر(?:ة)?)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"^(?:هل\s+)?(?:"
    r"في(?:ه|ا)?|عند(?:كم|ك)?|لد(?:يكم|يك)?|"
    r"متوفر(?:ة)?|موجود(?:ة)?|available"
    r")\s+\S"
    r"|^في\s+\S"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_VISUAL_BROWSE_RE = re.compile(
    r"(?:"
    r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ودي|ار(?:يد|سل)|"
    r"ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a)|اعرض(?:\s*لي)?)\s*(?:أ?شوف\s*)?(?:صور|صوره|صورة|الصور|الصورة)"
    r"|(?:صور|صوره|صورة)\s*(?:ل|لـ|ال|لل)\s*\S"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_VERB_RE = re.compile(
    r"(?:"
    r"^(?:أ?ضف|اضف|حط|زود|اب[يغ]|أ[بب]غ|اريد|احتاج|want|need|order|buy)\b"
    r"|(?:اخترت|خذ(?:\s+لي)?|هذا|هذي|الاول|الأول|رقم\s+\d)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_QTY_OR_WEIGHT_RE = re.compile(
    r"(?:"
    r"^\d+\s*(?:كilo|كيلo|kg|حبه|حبات|piece|pieces)\b"
    r"|(?:ربع|نصف|half|quarter)\s*كيل?o"
    r"|\d+\s*كيل?o"
    r"|كيل?o\s*(?:واحد|1|٢|2|٣|3)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ADD_VERB_RE = re.compile(
    r"(?:أ?ضف|اضف|حط|زود|add)\b",
    re.UNICODE | re.IGNORECASE,
)

# Trailing question on a lone token — availability ask, not a cart pick.
_BARE_TOKEN_INQUIRY_RE = re.compile(
    r"^[\w\u0600-\u06FF]{2,20}\s*[؟?]\s*$",
    re.UNICODE,
)


class CommerceTurnKind(str, Enum):
    UNKNOWN = "unknown"
    BROWSE = "browse"
    AVAILABILITY = "availability"
    VISUAL_BROWSE = "visual_browse"
    ORDER = "order"


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _NORM_RE.sub("", s)
    s = (
        s.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", s).strip()


def has_explicit_order_select_signal(message: str) -> bool:
    """True when the turn carries clear buy/select/add evidence."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)

    if _ORDER_VERB_RE.search(norm):
        if _VISUAL_BROWSE_RE.search(norm) and not _QTY_OR_WEIGHT_RE.search(norm):
            if not _ADD_VERB_RE.search(norm):
                return False
        return True

    if _QTY_OR_WEIGHT_RE.search(norm):
        return True

    if _ADD_VERB_RE.search(norm):
        return True

    # Legacy: bare subtype token without inquiry phrasing is a direct pick.
    tokens = [t for t in norm.split() if t]
    if len(tokens) == 1 and len(tokens[0]) >= 2:
        if not raw.rstrip().endswith("?") and not raw.rstrip().endswith("؟"):
            return True

    return False


def classify_commerce_turn_kind(message: str) -> CommerceTurnKind:
    raw = (message or "").strip()
    if not raw:
        return CommerceTurnKind.UNKNOWN

    if has_explicit_order_select_signal(raw):
        return CommerceTurnKind.ORDER

    norm = _norm(raw)

    if _VISUAL_BROWSE_RE.search(norm):
        return CommerceTurnKind.VISUAL_BROWSE

    if _BROWSE_TYPES_RE.search(norm):
        return CommerceTurnKind.BROWSE

    if _AVAILABILITY_RE.search(norm):
        return CommerceTurnKind.AVAILABILITY

    if _BARE_TOKEN_INQUIRY_RE.match(raw):
        return CommerceTurnKind.AVAILABILITY

    return CommerceTurnKind.UNKNOWN


def is_browse_availability_inquiry(message: str) -> bool:
    kind = classify_commerce_turn_kind(message)
    return kind in (
        CommerceTurnKind.BROWSE,
        CommerceTurnKind.AVAILABILITY,
        CommerceTurnKind.VISUAL_BROWSE,
    )


def is_commerce_inquiry_turn(message: str) -> bool:
    return is_browse_availability_inquiry(message)


def extract_inquiry_subject(message: str) -> Optional[str]:
    """Best-effort product/group token from an availability question."""
    raw = (message or "").strip()
    if not raw:
        return None
    norm = _norm(raw)

    m = re.search(
        r"^(?:هل\s+)?(?:في(?:ه|ا)?|عند(?:كم|ك)?|لد(?:يكم|يك)?|"
        r"متوفر(?:ة)?|موجود(?:ة)?|available|في)\s+(.+?)\s*[؟?]?\s*$",
        norm,
        re.UNICODE | re.IGNORECASE,
    )
    if m:
        subject = (m.group(1) or "").strip()
        return subject or None

    if _BARE_TOKEN_INQUIRY_RE.match(raw):
        return _norm(raw.rstrip("?؟")).strip() or None

    return None


__all__ = [
    "CommerceTurnKind",
    "classify_commerce_turn_kind",
    "extract_inquiry_subject",
    "has_explicit_order_select_signal",
    "is_browse_availability_inquiry",
    "is_commerce_inquiry_turn",
]
