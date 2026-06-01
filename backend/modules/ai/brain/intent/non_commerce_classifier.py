"""
brain/intent/non_commerce_classifier.py
────────────────────────────────────────
Deterministic NON-COMMERCE / social-media safety classifier.

Production regression (May 2026): customers forwarded Eid dua /
greeting images (long OCR, zero buying intent) and the brain escalated
into product search, ``narrow_choices``, catalog cards, and
"وجدت عدة خيارات تناسبك".

``classify_social`` intentionally caps at ~14 tokens — long OCR dumps
from greeting cards never match. This module closes that gap:

  * Strong religious / Eid / dua patterns bypass the length guard.
  * Media-origin tags set by ``media.normalizer`` short-circuit here.
  * Score-based dominance when OCR is long but clearly non-commercial.
  * Weak / unknown intent MUST NOT escalate into commerce.

Public contract
───────────────
``classify_non_commerce(...) -> NonCommerceMatch | None``

When a match is returned, callers MUST set ``block_commerce_escalation``
and suppress recommendation fallback, catalog orchestration, CTA
generation, and product-card sends for the turn.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .social_classifier import classify_social, _has_commercial_signal, _norm


# ── Categories (map to social_reply buckets) ────────────────────────────────
NC_EID_GREETING = "eid_greeting"
NC_DUA = "dua"
NC_MORNING_GREETING = "morning_greeting"
NC_CONDOLENCE = "condolence"
NC_SOCIAL_FORWARD = "social_forward"
NC_RELIGIOUS_MEDIA = "religious_media"
NC_EMOTIONAL = "emotional_personal"
NC_INFORMATIONAL = "informational_only"

# Tags prepended by media/normalizer.py — grep-stable prefixes.
NON_COMMERCE_IMAGE_TAG = (
    "[تصنيف الصورة: تهنئة/دعاء — بدون نية شراء]"
)
NON_COMMERCE_VIDEO_TAG = (
    "[تصنيف الوسائط: محتوى اجتماعي/ديني — بدون نية شراء]"
)

_NON_COMMERCE_TAGS = (
    NON_COMMERCE_IMAGE_TAG,
    NON_COMMERCE_VIDEO_TAG,
    "[تصنيف الصورة: محتوى اجتماعي",
    "[تصنيف الوسائط: محتوى اجتماعي",
)

# Explicit commerce intents that MAY escalate (positive confidence required).
POSITIVE_COMMERCE_INTENTS = frozenset({
    "ask_product",
    "ask_price",
    "product_visual_request",
    "start_order",
    "pick_list_item",
    "pay_now",
})


@dataclass(frozen=True)
class NonCommerceMatch:
    category: str
    confidence: float
    source: str  # ocr | text | media_tag | topic_hint | social_delegate
    block_commerce: bool = True

    @property
    def social_category(self) -> str:
        """Bucket name for ``ACTION_SOCIAL_REPLY`` / template pools."""
        return self.category


# ── Strong patterns (length-independent) ─────────────────────────────────────
_EID_PATTERNS = [
    re.compile(r"كل\s*عام\s*و?انت"),
    re.compile(r"كل\s*عام\s*و?ال"),
    re.compile(r"عيد\s*مبار"),
    re.compile(r"عيدكم\s*مبار"),
    re.compile(r"عيد\s*سع"),
    re.compile(r"تقبل\s*الله\s*ط"),
    re.compile(r"تقبل\s*الله\s*من"),
    re.compile(r"تقبل\s*الله\s*عب"),
    re.compile(r"ذي\s*الحج"),
    re.compile(r"ذو\s*الحج"),
    re.compile(r"الاضحى|الأضحى|عيد\s*الاض"),
    re.compile(r"عشر\s*ذي\s*الحج"),
    re.compile(r"عشر\s*ذو\s*الحج"),
    re.compile(r"eid\s*mubarak", re.I),
]

_DUA_PATTERNS = [
    re.compile(r"اللهم\s"),
    re.compile(r"اللّ?هم\s"),
    re.compile(r"يا\s*رب"),
    re.compile(r"يارب"),
    re.compile(r"ادعيه|أدعية|ادعية|دعاء"),
    re.compile(r"رب\s*اغفر"),
    re.compile(r"رب\s*ارحم"),
    re.compile(r"آ?مين"),
    re.compile(r"تقبل\s*الله"),
]

_MORNING_PATTERNS = [
    re.compile(r"صباح\s*ال"),
    re.compile(r"صبح\s*ال"),
    re.compile(r"مساء\s*ال"),
    re.compile(r"طاب\s*مس"),
    re.compile(r"طاب\s*صب"),
    re.compile(r"good\s*morning", re.I),
]

_CONDOLENCE_PATTERNS = [
    re.compile(r"الله\s*يرحم"),
    re.compile(r"رحم\s*الله"),
    re.compile(r"الف\s*سح"),
    re.compile(r"الف\s*رح"),
    re.compile(r"عظم\s*الله"),
    re.compile(r"البقاء\s*لله"),
    re.compile(r"انا\s*لله"),
    re.compile(r"إ?نا\s*لله"),
]

_FORWARD_MARKERS = (
    "forwarded", "frequently forwarded", "إعادة توجيه", "اعادة توجيه",
    "تمت إعادة", "تمت اعادة", "forwarded many times",
)

# Keyword buckets for score-based long OCR (normalised substring scan).
_NC_KEYWORDS: dict[str, tuple[str, ...]] = {
    NC_EID_GREETING: (
        "كل عام", "عيد مبار", "عيدكم", "عيد سع", "تقبل الله طاعت",
        "تقبل الله من", "ذي الحجه", "ذو الحجه", "الاضحى", "الأضحى",
        "عشر ذي", "عشر ذو", "eid", "mubarak",
    ),
    NC_DUA: (
        "اللهم", "يا رب", "يارب", "دعاء", "ادعيه", "أدعية", "ادعية",
        "رب اغفر", "رب ارحم", "آمين", "امين", "تقبل الله",
    ),
    NC_MORNING_GREETING: (
        "صباح الخير", "صباح النور", "صباح الورد", "مساء الخير",
        "مساء النور", "طاب مساك", "طاب صباح",
    ),
    NC_CONDOLENCE: (
        "الله يرحم", "رحم الله", "الف سح", "الف رح", "عظم الله",
        "البقاء لله", "انا لله", "إنا لله",
    ),
    NC_RELIGIOUS_MEDIA: (
        "قرآن", "quran", "سوره", "سورة", "آية", "ayah", "حديث",
        "صلى الله", "صلي الله", "بسم الله", "رمضان", "ramadan",
        "حج", "الحج", "hajj", "تهنئة", "تهاني", "مبروك", "بارك",
    ),
    NC_SOCIAL_FORWARD: _FORWARD_MARKERS,
    NC_EMOTIONAL: (
        "احبكم", "أحبكم", "اشتقت", "اشتقنا", "في قلبي", "معاكم",
        "بالتوفيق", "بالسعاده", "بالسعادة",
    ),
}

_MEDIA_ORIGIN_MARKERS = (
    "[وصف الصورة", "[وصف الصورة المرسلة]", "[وصف الفيديو",
    "استنتاج خفيف من النص",
)

_STRONG_COMMERCE_MARKERS = (
    "ابغي", "ابغى", "ابي", "اريد", "أريد", "اطلب", "اشتري",
    "سعر", "بكم", "كم سعر", "منتج", "المنتج", "متوفر", "موجود",
    "كيلو", "جرام", "شحن", "توصيل", "رابط الدفع", "sku",
    "buy", "price", "product", "order",
)


def _strip_media_framing(message: str) -> str:
    """Remove normalizer framing so keyword scans hit OCR body."""
    if not message:
        return ""
    lines = []
    for line in message.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(tag) for tag in _NON_COMMERCE_TAGS):
            continue
        if s.startswith("[تصنيف"):
            continue
        if s.startswith("[وصف الصورة"):
            s = s.split("]", 1)[-1].strip() if "]" in s else ""
        elif s.startswith("[وصف الفيديو"):
            s = s.split("]", 1)[-1].strip() if "]" in s else ""
        if s.startswith("استنتاج خفيف"):
            continue
        if s.startswith("اقرأ السياق"):  # video brain instruction block
            continue
        if s:
            lines.append(s)
    return "\n".join(lines)


def _count_keyword_hits(norm: str, keywords: Sequence[str]) -> int:
    return sum(1 for kw in keywords if kw in norm)


def _score_categories(norm: str) -> List[tuple[str, int]]:
    scored: List[tuple[str, int]] = []
    for cat, kws in _NC_KEYWORDS.items():
        hits = _count_keyword_hits(norm, kws)
        if hits:
            scored.append((cat, hits))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _match_strong_patterns(norm: str) -> Optional[str]:
    for pat in _EID_PATTERNS:
        if pat.search(norm):
            return NC_EID_GREETING
    for pat in _DUA_PATTERNS:
        if pat.search(norm):
            return NC_DUA
    for pat in _MORNING_PATTERNS:
        if pat.search(norm):
            return NC_MORNING_GREETING
    for pat in _CONDOLENCE_PATTERNS:
        if pat.search(norm):
            return NC_CONDOLENCE
    return None


def _is_media_origin(message: str) -> bool:
    return any(m in (message or "") for m in _MEDIA_ORIGIN_MARKERS)


def _commerce_keyword_hit(norm: str, kw: str) -> bool:
    """Boundary-aware commerce keyword match.

    Production regression (May 2026): Eid greeting cards signed
    ``محبكم`` falsely matched the price-ask token ``بكم`` and blocked
    the non-commerce classifier on long religious OCR.
    """
    if kw == "بكم":
        return bool(re.search(r"(?<![ء-ي])بكم(?![ء-ي])", norm))
    if kw in ("منتج", "المنتج", "product"):
        return bool(re.search(rf"(?<![\w]){re.escape(kw)}", norm))
    return kw in norm


def _has_commercial_signal_bounded(norm: str) -> bool:
    from .social_classifier import _COMMERCIAL_DISQUALIFIERS  # noqa: PLC0415

    return any(_commerce_keyword_hit(norm, kw) for kw in _COMMERCIAL_DISQUALIFIERS)


def _has_strong_commerce(norm: str) -> bool:
    # Vision/OCR often says "no products" / "بدون منتجات" — not buying intent.
    if re.search(
        r"(?:بدون|without|no|not)\s+.{0,24}(?:منتج|product|شراء|buy)",
        norm,
    ):
        return False
    if _has_commercial_signal_bounded(norm):
        return True
    hits = 0
    for kw in _STRONG_COMMERCE_MARKERS:
        if _commerce_keyword_hit(norm, kw):
            hits += 1
    return hits >= 2


def classify_non_commerce(
    message: str,
    *,
    media_type: Optional[str] = None,
    topic_hints: Optional[List[str]] = None,
    intent_name: Optional[str] = None,
    intent_confidence: Optional[float] = None,
) -> Optional[NonCommerceMatch]:
    """Return a non-commerce match or ``None`` when commerce may proceed.

    Safe to call on any input; never raises.
    """
    if not message or not isinstance(message, str):
        return None

    raw = message.strip()
    if not raw:
        return None

    # 0. Explicit media tag from normalizer — highest priority.
    if any(tag in raw for tag in _NON_COMMERCE_TAGS):
        body_norm = _norm(_strip_media_framing(raw))
        cat = _match_strong_patterns(body_norm) or NC_RELIGIOUS_MEDIA
        return NonCommerceMatch(
            category=cat,
            confidence=0.98,
            source="media_tag",
        )

    # 1. Delegate to short social classifier (thanks / basmala / …).
    social = classify_social(raw)
    if social is not None:
        return NonCommerceMatch(
            category=social.category,
            confidence=max(social.confidence, 0.94),
            source="social_delegate",
        )

    body = _strip_media_framing(raw)
    norm = _norm(body)
    if not norm:
        return None

    # 2. Strong pattern pass — no length limit (Eid cards, long dua).
    strong_cat = _match_strong_patterns(norm)
    if strong_cat and not _has_strong_commerce(norm):
        # Short standalone morning/evening text greetings stay on the
        # INTENT_GREETING path — non-commerce media safety targets OCR
        # dumps and forwarded cards, not a bare "صباح الخير".
        if (
            strong_cat == NC_MORNING_GREETING
            and len(norm.split()) <= 5
            and not _is_media_origin(raw)
            and media_type is None
        ):
            pass
        else:
            return NonCommerceMatch(
                category=strong_cat,
                confidence=0.97,
                source="text",
            )

    # 3. Video topic hint advisory (from normalizer).
    hints = [str(h) for h in (topic_hints or []) if h]
    if hints:
        if any("دعاء" in h or "تهنئة" in h for h in hints):
            if not any("منتج" in h or "شراء" in h for h in hints):
                if not _has_strong_commerce(norm):
                    return NonCommerceMatch(
                        category=NC_DUA if "دعاء" in hints[0] else NC_EID_GREETING,
                        confidence=0.96,
                        source="topic_hint",
                    )

    # 4. Score-based dominance for long OCR / forwards.
    scored = _score_categories(norm)
    if scored and not _has_strong_commerce(norm):
        top_cat, top_hits = scored[0]
        commerce_hits = _count_keyword_hits(norm, _STRONG_COMMERCE_MARKERS)
        forward_hit = _count_keyword_hits(norm, _FORWARD_MARKERS) > 0

        # Long religious OCR: ≥2 independent non-commerce hits wins.
        if top_hits >= 2:
            return NonCommerceMatch(
                category=top_cat,
                confidence=0.95,
                source="ocr" if _is_media_origin(raw) else "text",
            )

        # Single strong religious token on media-origin input.
        if top_hits >= 1 and (_is_media_origin(raw) or media_type in {"image", "video"}):
            if top_cat in {NC_EID_GREETING, NC_DUA, NC_RELIGIOUS_MEDIA, NC_CONDOLENCE}:
                return NonCommerceMatch(
                    category=top_cat,
                    confidence=0.94,
                    source="ocr" if _is_media_origin(raw) else "media_semantics",
                )

        # WhatsApp forward + any greeting/dua signal.
        if forward_hit and top_hits >= 1 and commerce_hits == 0:
            return NonCommerceMatch(
                category=NC_SOCIAL_FORWARD,
                confidence=0.93,
                source="ocr" if _is_media_origin(raw) else "text",
            )

    # 5. Weak-intent media guard — unknown intent on image/video OCR must
    #    NOT fall through to recommendation fallback.
    if media_type in {"image", "video"} or _is_media_origin(raw):
        weak_intent = (
            intent_name in {None, "", "general"}
            or (intent_confidence is not None and intent_confidence < 0.70)
        )
        if weak_intent and scored and not _has_strong_commerce(norm):
            top_cat, top_hits = scored[0]
            if top_hits >= 1:
                return NonCommerceMatch(
                    category=top_cat,
                    confidence=0.92,
                    source="media_semantics",
                )

    return None


def resolve_commerce_block(
    message: str,
    *,
    inbound_metadata: Optional[dict] = None,
    intent_name: Optional[str] = None,
    intent_confidence: Optional[float] = None,
) -> Optional[NonCommerceMatch]:
    """Turn-level helper: metadata flag OR live classification."""
    meta = inbound_metadata or {}
    if meta.get("block_commerce_escalation"):
        cat = str(meta.get("non_commerce_category") or NC_RELIGIOUS_MEDIA)
        return NonCommerceMatch(
            category=cat,
            confidence=0.98,
            source=str(meta.get("non_commerce_source") or "media_tag"),
        )
    media_type = meta.get("source_type") or meta.get("normalized_type")
    hints = meta.get("topic_hints")
    if isinstance(hints, list):
        topic_hints: Optional[List[str]] = [str(x) for x in hints]
    else:
        topic_hints = None
    return classify_non_commerce(
        message,
        media_type=str(media_type) if media_type else None,
        topic_hints=topic_hints,
        intent_name=intent_name,
        intent_confidence=intent_confidence,
    )


def has_positive_commerce_intent(
    intent_name: Optional[str],
    intent_confidence: Optional[float] = None,
    *,
    min_confidence: float = 0.82,
) -> bool:
    """Catalog / product-card escalation requires explicit commerce intent."""
    name = (intent_name or "").strip().lower()
    if name not in POSITIVE_COMMERCE_INTENTS:
        return False
    if intent_confidence is None:
        return True
    return float(intent_confidence) >= float(min_confidence)


def commerce_escalation_allowed(
    message: str,
    *,
    intent_name: Optional[str] = None,
    intent_confidence: Optional[float] = None,
    inbound_metadata: Optional[dict] = None,
    attachment_confidence: Optional[str] = None,
    has_active_product_focus: bool = False,
    is_continuation_pick: bool = False,
) -> bool:
    """Positive gate for catalog / recommendation escalation."""
    if resolve_commerce_block(
        message,
        inbound_metadata=inbound_metadata,
        intent_name=intent_name,
        intent_confidence=intent_confidence,
    ):
        return False

    if is_continuation_pick:
        return True

    if has_active_product_focus and intent_name in {"start_order", "pay_now", "general"}:
        conf = (attachment_confidence or "").strip().lower()
        if conf in {"strong", "high", "medium"}:
            return True

    if has_positive_commerce_intent(intent_name, intent_confidence):
        return True

    conf = (attachment_confidence or "").strip().lower()
    if conf in {"strong", "high"}:
        return has_positive_commerce_intent(intent_name, intent_confidence)

    return False


__all__ = [
    "NC_CONDOLENCE",
    "NC_DUA",
    "NC_EID_GREETING",
    "NC_EMOTIONAL",
    "NC_INFORMATIONAL",
    "NC_MORNING_GREETING",
    "NC_RELIGIOUS_MEDIA",
    "NC_SOCIAL_FORWARD",
    "NON_COMMERCE_IMAGE_TAG",
    "NON_COMMERCE_VIDEO_TAG",
    "NonCommerceMatch",
    "POSITIVE_COMMERCE_INTENTS",
    "classify_non_commerce",
    "commerce_escalation_allowed",
    "has_positive_commerce_intent",
    "resolve_commerce_block",
]
