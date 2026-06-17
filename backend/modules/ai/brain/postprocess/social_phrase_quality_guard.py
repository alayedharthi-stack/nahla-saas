"""
social_phrase_quality_guard.py
──────────────────────────────
Post-compose belt guard (P1-F): strip unnatural / non-local Saudi social
phrasing from outbound text.

Strip only — no replacement copy. Operational facts (payment, order,
price, tracking) are preserved when they share a segment with a
forbidden social marker.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.brain.postprocess.social_phrase_quality_guard")

# Normalized substring markers — whole segment dropped when segment norm
# is ONLY the marker, or segment is dominated by poetic social phrasing.
_FORBIDDEN_SEGMENT_MARKERS: tuple[str, ...] = (
    "يطري ايامك",
    "يطرى ايامك",
    "دوم بخير",
    "ولك بمثل ما دعيت",
    "ولك بالمثل",
    "تحت امرك",
    "بالخدمه",
    "بالخدمة",
)

# Inline patterns removed from a line while keeping operational prefix/suffix.
_INLINE_STRIP_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"الله\s*يطر[يىّ]?\s*أ?ي?ا?م?ك?",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(r"دوم\s+بخير", re.UNICODE | re.IGNORECASE),
    re.compile(r"(?:^|[\s،,.!])ت?دوم(?:[\s،,.!]|$)", re.UNICODE | re.IGNORECASE),
    re.compile(r"(?:^|[\s،,.!])دوم(?:[\s،,.!]|$)", re.UNICODE | re.IGNORECASE),
    re.compile(r"تحت\s+أ?مر(?:ك|كم|كن)", re.UNICODE | re.IGNORECASE),
    re.compile(r"(?:^|[\s،,.!])بالخدم(?:ة|ه)(?:[\s،,.!]|$)", re.UNICODE | re.IGNORECASE),
    re.compile(r"ولك\s+ب?مثل\s+ما\s+د?ع?ي?ت?", re.UNICODE | re.IGNORECASE),
    re.compile(r"ولك\s+بال?مثل", re.UNICODE | re.IGNORECASE),
)

_OPERATIONAL_MARKERS: tuple[str, ...] = (
    "طلبك",
    "الطلب",
    "ريال",
    "السعر",
    "الآيبان",
    "iban",
    "sa",
    "تتبع",
    "الشحن",
    "الدفع",
    "تحويل",
    "موظف",
    "تصعيد",
)


@dataclass(frozen=True)
class SocialPhraseQualityGuardResult:
    reply: str
    stripped: bool


def _normalize_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640\u06D6-\u06ED]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _segment_has_forbidden_marker(segment: str) -> bool:
    norm = _normalize_ar(segment)
    if not norm:
        return False
    if any(m in norm for m in _FORBIDDEN_SEGMENT_MARKERS):
        return True
    if re.search(r"يطر[يىّ]?\s*ايامك", norm):
        return True
    if re.search(r"(?:^|\s)دوم(?:\s|$)", norm) and "دوم بخير" not in norm:
        # Standalone «دوم» token (not part of a longer operational word).
        if norm == "دوم" or " دوم " in f" {norm} ":
            return True
    if norm in {"دوم", "تدوم"}:
        return True
    return False


def _looks_operational(segment: str) -> bool:
    norm = _normalize_ar(segment)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in _OPERATIONAL_MARKERS)


def _strip_inline_forbidden(line: str) -> tuple[str, bool]:
    raw = (line or "").strip()
    if not raw:
        return "", False
    cleaned = raw
    stripped = False
    for pattern in _INLINE_STRIP_RES:
        new = pattern.sub(" ", cleaned)
        if new != cleaned:
            stripped = True
            cleaned = new
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ،,.!…")
    return cleaned, stripped


def _strip_line(line: str) -> tuple[str, bool]:
    raw = (line or "").strip()
    if not raw:
        return "", False

    if not _segment_has_forbidden_marker(raw):
        return raw, False

    if _looks_operational(raw):
        cleaned, stripped = _strip_inline_forbidden(raw)
        if cleaned and not _segment_has_forbidden_marker(cleaned):
            return cleaned, stripped
        # Operational line still polluted — keep non-forbidden inline parts only.
        return cleaned, stripped or bool(cleaned != raw)

    cleaned, stripped = _strip_inline_forbidden(raw)
    if cleaned and not _segment_has_forbidden_marker(cleaned):
        return cleaned, stripped
    return "", True


def strip_social_phrase_violations(text: str) -> tuple[str, bool]:
    """Remove forbidden social phrase segments from ``text``."""
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    kept_paragraphs: list[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue

        if _segment_has_forbidden_marker(p) and not _looks_operational(p):
            inline_lines: list[str] = []
            for ln in p.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                cleaned, did = _strip_line(ln)
                if did:
                    stripped_any = True
                if cleaned:
                    inline_lines.append(cleaned)
            if inline_lines:
                kept_paragraphs.append("\n".join(inline_lines))
            continue

        lines_out: list[str] = []
        for ln in p.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            cleaned, did = _strip_line(ln)
            if did:
                stripped_any = True
            if cleaned:
                lines_out.append(cleaned)
        if lines_out:
            kept_paragraphs.append("\n".join(lines_out))

    result = "\n\n".join(kept_paragraphs).strip()
    return result, stripped_any


def apply_social_phrase_quality_guard(
    reply: str,
    *,
    inbound_text: str = "",
    tenant_id: Optional[int] = None,
) -> SocialPhraseQualityGuardResult:
    text = (reply or "").strip()
    if not text:
        return SocialPhraseQualityGuardResult(reply="", stripped=False)

    stripped = False
    try:
        from modules.ai.brain.postprocess.social_reply_context_guard import (  # noqa: PLC0415
            apply_social_reply_context_guard,
        )

        _ctx_guard = apply_social_reply_context_guard(
            text,
            inbound_text=inbound_text,
            tenant_id=tenant_id,
        )
        if _ctx_guard.replaced:
            text = _ctx_guard.reply
            stripped = True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — belt guard must never break outbound
        pass

    try:
        from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
            inbound_is_religious_dua_exchange,
        )

        if inbound_is_religious_dua_exchange(inbound_text):
            return SocialPhraseQualityGuardResult(reply=text, stripped=stripped)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — bypass must never break outbound
        pass

    cleaned, stripped_violations = strip_social_phrase_violations(text)
    if stripped_violations:
        stripped = True
    if stripped:
        logger.info(
            "[SOCIAL_PHRASE_QUALITY_GUARD] tenant=%s orig_len=%d new_len=%d "
            "preview_in=%r",
            tenant_id if tenant_id is not None else "-",
            len(text),
            len(cleaned),
            (inbound_text or "")[:60],
        )

    return SocialPhraseQualityGuardResult(reply=cleaned, stripped=stripped)


__all__ = [
    "SocialPhraseQualityGuardResult",
    "apply_social_phrase_quality_guard",
    "strip_social_phrase_violations",
]
