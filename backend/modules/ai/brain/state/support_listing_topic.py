"""
brain/state/support_listing_topic.py
────────────────────────────────────
Detect Google Business Profile / merchant listing support topics so stale
order/fulfillment state does not hijack the conversation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

_MAPS_PLATFORM_RE = re.compile(
    r"(?:قوقل\s*ماب|google\s*maps?|google\s*business|business\s*profile|"
    r"خرائط\s*قوقل|maps\.google|goo\.gl/maps|google\s*listing)",
    re.UNICODE | re.IGNORECASE,
)

_LISTING_ISSUE_RE = re.compile(
    r"(?:ملاحظ[ةه]|مشكل[ةه]|تصنيف(?:\s*ال)?(?:نشاط|النشاط)?|"
    r"category|listing|profile|الملف\s*التجاري|متجر\s*سلع|"
    r"business\s*category|صفح[ةه]\s*قوقل|موقع\s*قوقل|"
    r"نشاط\s*التاجر|business\s*profile)",
    re.UNICODE | re.IGNORECASE,
)

_CATEGORY_MISMATCH_RE = re.compile(
    r"متجر\s*سلع\s*منزل",
    re.UNICODE | re.IGNORECASE,
)

_CATEGORY_CHANGE_RE = re.compile(
    r"(?:مكتوب|ظاهر|صار|صارت|صار\s*فيه|غلط|خطأ|بدل|مو\s*صح)",
    re.UNICODE | re.IGNORECASE,
)

_RELAY_TO_STAFF_RE = re.compile(
    r"(?:"
    r"ور(?:ي|يه(?:ا)?)\s*(?:ال)?(?:رسائل|رسايل|الرسائل|الرسايل)?\s*ل"
    r"|أ?رسل(?:ها)?\s*ل"
    r"|بلغ\s*(?:تركي|الاداره|الإدارة|الادارة)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_GBP_OCR_CATEGORY_RE = re.compile(
    r"(?:متجر\s*سلع\s*منزل|home\s*goods(?:\s*store)?|"
    r"business\s*category|google\s*business)",
    re.UNICODE | re.IGNORECASE,
)

_GBP_OCR_BUSINESS_META_RE = re.compile(
    r"(?:\.com|www\.|http|055\s*\d|\+966|تقييم|\d+\.\d+\s*نج|"
    r"reviews?|stars?|website|موقع\s*الكتروني)",
    re.UNICODE | re.IGNORECASE,
)

_GBP_OCR_DIRECTIONS_RE = re.compile(
    r"(?:اتجاهات|directions|drop\s*a\s*pin|dropped\s*pin)",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def _join_context(parts: Iterable[str]) -> str:
    return "\n".join(str(p or "").strip() for p in parts if str(p or "").strip())


def detect_support_listing_topic_shift(
    message: str,
    *,
    extra_context: str = "",
) -> bool:
    """True when the turn is about merchant listing / Google profile support."""
    blob = _join_context([message, extra_context])
    if not blob.strip():
        return False

    norm = _normalize(blob)
    has_maps = bool(_MAPS_PLATFORM_RE.search(norm))
    has_listing_issue = bool(_LISTING_ISSUE_RE.search(norm))

    # Maps platform + notice/issue wording (e.g. ملاحظة في قوقل ماب).
    if has_maps and has_listing_issue:
        return True

    # Category/listing clarification without needing a maps keyword.
    if _CATEGORY_MISMATCH_RE.search(norm) and _CATEGORY_CHANGE_RE.search(norm):
        return True

    # Relay screenshots/messages to merchant staff after a listing issue.
    if _RELAY_TO_STAFF_RE.search(norm) and (has_listing_issue or has_maps):
        return True

    return False


def detect_support_listing_from_image_metadata(
    metadata: Optional[Dict[str, Any]],
    recent_inbound_messages: Optional[Sequence[str]] = None,
) -> bool:
    """True when a map-classified image is likely a business listing screenshot."""
    md = metadata or {}
    recent = list(recent_inbound_messages or [])
    vision = str(md.get("vision_text") or md.get("frame_vision_text") or "")
    caption = str(md.get("caption") or "")
    combined = _join_context([*recent[-5:], caption, vision])

    if detect_support_listing_topic_shift("", extra_context=combined):
        return True

    blob = _normalize(combined)
    has_category = bool(_GBP_OCR_CATEGORY_RE.search(blob))
    has_directions = bool(_GBP_OCR_DIRECTIONS_RE.search(blob))
    has_business_meta = bool(_GBP_OCR_BUSINESS_META_RE.search(blob))

    # GBP screenshots often show category + directions + store metadata together.
    if has_category and (has_directions or has_business_meta):
        return True

    return False


def collect_support_listing_context(ctx: Any) -> str:
    """Recent inbound turns + optional vision metadata from the brain context."""
    chunks: List[str] = []
    history = getattr(ctx, "history", None) or []
    for row in history[-6:]:
        if not isinstance(row, dict):
            continue
        if str(row.get("direction") or "") != "inbound":
            continue
        body = str(row.get("body") or row.get("message") or "").strip()
        if body:
            chunks.append(body)

    profile = getattr(ctx, "profile", None) or {}
    if isinstance(profile, dict):
        meta = profile.get("inbound_metadata") or {}
        if isinstance(meta, dict):
            for key in ("vision_text", "frame_vision_text", "caption"):
                val = str(meta.get(key) or "").strip()
                if val:
                    chunks.append(val)

    return _join_context(chunks)


def support_listing_topic_shift_for_context(ctx: Any, *, message: str = "") -> bool:
    msg = message or str(getattr(ctx, "message", "") or "")
    extra = collect_support_listing_context(ctx)
    return detect_support_listing_topic_shift(msg, extra_context=extra)


__all__ = [
    "collect_support_listing_context",
    "detect_support_listing_from_image_metadata",
    "detect_support_listing_topic_shift",
    "support_listing_topic_shift_for_context",
]
