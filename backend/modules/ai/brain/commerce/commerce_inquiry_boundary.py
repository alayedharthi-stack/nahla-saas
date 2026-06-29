"""
Platform-wide browse/availability vs checkout boundary detection.

Deterministic guard — separates inquiry turns from order/select turns without
merchant-specific product hardcoding.
"""
from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import List, Optional

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

_PRICE_INQUIRY_RE = re.compile(
    r"(?:"
    r"كم\s*(?:ال)?(?:سعر|ثمن)\b"
    r"|(?:^|\s)(?:ال)?(?:سعر|ثمن)(?:ه|ها|هم|كم)?\b"
    r"|(?:^|\s)بكم\b"
    r"|قد\s*ايش"
    r"|how\s*much"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PACKAGED_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"\d+\s*(?:عبو(?:ات|ه|ة)|حبات?|قطع(?:ة|ات)?|pieces?)\s*(?:\d+\s*)?(?:g|gr|جرام|gram|ج)\b"
    r"|\d+\s*(?:g|gr|جرام|gram|ج)\s*(?:عبو(?:ات|ه|ة)|حبات?)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MULTI_TYPE_INQUIRY_RE = re.compile(
    r"(?:"
    r"^اب(?:ي|غ(?:ى|a)?)\s+(?:\d+\s*)?(?:انواع|الانواع|الأنواع|نوع|types)\b"
    r"|^(?:\d+\s*)?(?:انواع|الانواع|الأنواع)\s+(?:ه(?:ذه|ذي|ذا)|دي|ذي)\b"
    r"|^اب(?:ي|غ(?:ى|a)?)\s+(?:\d+\s*)?(?:ه(?:ذه|ذي|ذا)|دي|ذي)\s*(?:انواع|الانواع|الأنواع|نوع)?\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SOFT_WANT_INQUIRY_RE = re.compile(
    r"(?:"
    r"^اب(?:ي|غ(?:ى|a)?)\s+(?:اعرف|استفسر|استفسار|تفاصيل|معلومات|اسعار|عرض|اشوف|أشوف)\b"
    r"|(?:استفسار|استفسر)\s+(?:عن|حول)\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_PURCHASE_RE = re.compile(
    r"(?:"
    r"(?:أ?ضف|اضف|حط|زود|جهز|اعتمد|خذ(?:\s+لي)?|"
    r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ار(?:يد|سل)|ودي|بدي)\s*(?:اطلب|أطلب|اشتري|أشتري))"
    r"|(?:اطلب|أطلب|طلب|اشتري|أشتري|شراء|buy|order|purchase)\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:"
    r"هل\s+"
    r"|(?:في(?:ه|ا)?|عند(?:كم|ك)?|لد(?:يكم|يك)?|"
    r"متوفر(?:ة)?|موجود(?:ة)?|available|في)\s+"
    r")+",
    re.UNICODE | re.IGNORECASE,
)

_INQUIRY_SUBJECT_RE = re.compile(
    r"^(?:هل\s+)?(?:في(?:ه|ا)?|عند(?:كم|ك)?|لد(?:يكم|يك)?|"
    r"متوفر(?:ة)?|موجود(?:ة)?|available|في)\s+(.+?)\s*[؟?]?\s*$",
    re.UNICODE | re.IGNORECASE,
)

_MEDIA_FRAMING_MARKERS: tuple[str, ...] = (
    "[وصف الصورة",
    "[وصف الفيديو",
    "[وصف الستيكر",
    "[تصنيف",
)


class CommerceTurnKind(str, Enum):
    UNKNOWN = "unknown"
    BROWSE = "browse"
    AVAILABILITY = "availability"
    VISUAL_BROWSE = "visual_browse"
    PRICE_INQUIRY = "price_inquiry"
    ORDER = "order"


def _is_media_framed_message(message: str) -> bool:
    raw = (message or "").strip()
    return any(marker in raw for marker in _MEDIA_FRAMING_MARKERS)


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


def _inquiry_probe_messages(message: str) -> List[str]:
    """Candidate sub-messages for availability/subject probes after greeting strip."""
    raw = (message or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    probes: List[str] = []

    def _add(candidate: str) -> None:
        c = (candidate or "").strip()
        if c and c not in seen:
            seen.add(c)
            probes.append(c)

    _add(raw)
    try:
        from modules.ai.brain.intent.rules import _strip_greeting_residue  # noqa: PLC0415

        _add(_strip_greeting_residue(raw))
        for line in raw.splitlines():
            _add(line)
            _add(_strip_greeting_residue(line))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional rules import
        for line in raw.splitlines():
            _add(line)
    return probes


def _clean_inquiry_subject(subject: str) -> Optional[str]:
    s = _norm(subject)
    if not s:
        return None
    while True:
        nxt = _SUBJECT_PREFIX_RE.sub("", s, count=1).strip()
        if nxt == s:
            break
        s = nxt
    s = s.rstrip("?؟").strip()
    return s or None


def _extract_subject_from_probe(probe: str) -> Optional[str]:
    raw = (probe or "").strip()
    if not raw:
        return None
    norm = _norm(raw)
    m = _INQUIRY_SUBJECT_RE.search(norm)
    if m:
        subject = (m.group(1) or "").strip()
        return _clean_inquiry_subject(subject) if subject else None
    if _BARE_TOKEN_INQUIRY_RE.match(raw):
        return _clean_inquiry_subject(_norm(raw.rstrip("?؟")).strip())
    return None


def has_price_inquiry_signal(message: str) -> bool:
    """True when the customer asks for price/cost — not checkout."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not _PRICE_INQUIRY_RE.search(norm):
        return False
    if _EXPLICIT_PURCHASE_RE.search(norm):
        return False
    return True


def _has_packaged_availability_ask(norm: str) -> bool:
    if not re.search(r"(?:متوفر|موجود|available)", norm, re.UNICODE | re.IGNORECASE):
        return False
    return bool(_PACKAGED_AVAILABILITY_RE.search(norm))


def has_explicit_order_select_signal(message: str) -> bool:
    """True when the turn carries clear buy/select/add evidence."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)

    if has_price_inquiry_signal(raw):
        return False
    if _MULTI_TYPE_INQUIRY_RE.search(norm) or _SOFT_WANT_INQUIRY_RE.search(norm):
        return False
    if _has_packaged_availability_ask(norm):
        return False

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

    if has_price_inquiry_signal(raw):
        return CommerceTurnKind.PRICE_INQUIRY

    if _VISUAL_BROWSE_RE.search(norm):
        return CommerceTurnKind.VISUAL_BROWSE

    if _BROWSE_TYPES_RE.search(norm):
        return CommerceTurnKind.BROWSE

    if _MULTI_TYPE_INQUIRY_RE.search(norm) or _SOFT_WANT_INQUIRY_RE.search(norm):
        return CommerceTurnKind.BROWSE

    for probe in _inquiry_probe_messages(raw):
        probe_norm = _norm(probe)
        if _AVAILABILITY_RE.search(probe_norm) or _has_packaged_availability_ask(probe_norm):
            return CommerceTurnKind.AVAILABILITY
        if _BARE_TOKEN_INQUIRY_RE.match(probe):
            return CommerceTurnKind.AVAILABILITY

    return CommerceTurnKind.UNKNOWN


def is_browse_availability_inquiry(message: str) -> bool:
    kind = classify_commerce_turn_kind(message)
    return kind in (
        CommerceTurnKind.BROWSE,
        CommerceTurnKind.AVAILABILITY,
        CommerceTurnKind.VISUAL_BROWSE,
        CommerceTurnKind.PRICE_INQUIRY,
    )


def is_commerce_inquiry_turn(message: str) -> bool:
    return is_browse_availability_inquiry(message)


def has_embedded_commerce_inquiry_beyond_greeting(message: str) -> bool:
    """True when a greeting/social prefix coexists with a commerce inquiry.

    Used to block greeting-only / ``social_non_commerce`` ownership when the
    customer bundled availability, price, product, or browse asks.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if _is_media_framed_message(raw):
        return False

    try:
        from modules.ai.brain.intent.rules import _strip_greeting_residue  # noqa: PLC0415

        residue = _strip_greeting_residue(raw)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional rules import; treat as no peel
        return False

    # Pure greeting or no greeting prefix was removed from the front.
    if not residue:
        return False
    if len(residue) >= len(raw) - 2:
        return False

    seen: set[str] = set()
    probes: List[str] = []

    def _add(candidate: str) -> None:
        c = (candidate or "").strip()
        if c and c not in seen:
            seen.add(c)
            probes.append(c)

    _add(raw)
    _add(residue)
    for line in raw.splitlines():
        _add(line)
        try:
            _add(_strip_greeting_residue(line))
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional peel per line
            pass

    for probe in probes:
        if is_commerce_inquiry_turn(probe):
            return True
        if has_price_inquiry_signal(probe):
            return True
        try:
            from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
                has_explicit_product_commerce_intent,
            )

            if has_explicit_product_commerce_intent(probe):
                return True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product intent probe
            pass

    return False


def extract_inquiry_subject(message: str) -> Optional[str]:
    """Best-effort product/group token from an availability question."""
    for probe in _inquiry_probe_messages(message):
        subject = _extract_subject_from_probe(probe)
        if subject:
            return subject
    return None


__all__ = [
    "CommerceTurnKind",
    "classify_commerce_turn_kind",
    "extract_inquiry_subject",
    "has_embedded_commerce_inquiry_beyond_greeting",
    "has_explicit_order_select_signal",
    "has_price_inquiry_signal",
    "is_browse_availability_inquiry",
    "is_commerce_inquiry_turn",
]
