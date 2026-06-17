"""
commerce_reply_quality_guard.py
───────────────────────────────
Strip internal/footer/template residue and English leakage from Brain
commerce replies before WhatsApp dispatch. Replaces empty results with
safe Arabic commerce fallbacks.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Pattern, Tuple

from modules.ai.brain.intent_priority.types import (
    GOAL_GREETING_ONLY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SOCIAL_ONLY,
)

logger = logging.getLogger("nahla.brain.postprocess.commerce_reply_quality_guard")

_FALLBACK_AVAILABILITY_AR = "التوفر قيد التحقق."
_FALLBACK_PRODUCT_UNRESOLVED_AR = "حدّد المنتج أو المقاس المطلوب."
_FALLBACK_DELIVERY_AR = "التوصيل لمنطقتك قيد التحقق."
_FALLBACK_SOCIAL_AR = "حياك الله، وصلت رسالتك."
_FALLBACK_GREETING_AR = "وعليكم السلام، حياك الله."

_MIN_MEANINGFUL_CHARS = 6

_FORBIDDEN_RESIDUE_RES: Tuple[Pattern[str], ...] = (
    re.compile(r"powered\s+by\s+nahla", re.IGNORECASE),
    re.compile(r"let\s+me\s+verify", re.IGNORECASE),
    re.compile(r"current\s+availability", re.IGNORECASE),
    re.compile(r"same-day\s+delivery\s+availability", re.IGNORECASE),
    re.compile(r"availability\s+for\s+your\s+area", re.IGNORECASE),
)

_GENERIC_AR_VERIFY_RES: Tuple[Pattern[str], ...] = (
    re.compile(
        r"س+[اأ]تحقق\s+من\s+توفر\s+المنتج\s+لك",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"س+[اأ]تحقق\s+من\s+إ?م?ك?ا?ن?ي?ة?\s+التوصيل",
        re.UNICODE | re.IGNORECASE,
    ),
)

_COMMERCE_SUBSTANCE_RE = re.compile(
    r"(?:منتج|عسل|متوفر|متاح|سعر|حجم|وزن|طلب|ريال|أبشر|كيلو|جرام)",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_INBOUND_RE = re.compile(
    r"(?:\u0647\u0644|\u0639\u0646\u062f\u0643\u0645|\u0639\u0646\u062f\u0643|\u0645\u062a\u0648\u0641\u0631|\u0628\u0643\u0645|\u0643\u0645\s|\u0623\u064a\s|\u0648\u0634\s)",
    re.UNICODE | re.IGNORECASE,
)

_DELIVERY_INBOUND_RE = re.compile(
    r"(?:موقع|الموقع|عنوان|العنوان|توصيل|استلام|منطقت|المنطقة|"
    r"maps\.google|goo\.gl/maps|short\s+address|العنوان\s+الوطني|"
    r"delivery|address|location)",
    re.UNICODE | re.IGNORECASE,
)

_COMMERCE_INTENTS = frozenset({
    "solution_seeking_commerce",
    "ask_product",
    "ask_price",
    "product_availability",
    "product_reference",
    "ask_shipping",
})

_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_EMOJI_CHECK_RE = re.compile(r"[✅✔️]\s*")


@dataclass(frozen=True)
class CommerceReplyQualityGuardResult:
    reply: str
    replaced: bool
    stripped_residue: bool
    stripped_english: bool
    used_fallback: bool
    fallback_kind: str = ""


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640\u06D6-\u06ED]", "", t)
    return t


def inbound_is_arabic(inbound_text: str, *, locale: str = "ar") -> bool:
    loc = (locale or "ar").strip().lower()
    if loc.startswith("en"):
        return False
    text = (inbound_text or "").strip()
    if not text:
        return True
    if _ARABIC_CHAR_RE.search(text):
        return True
    return not _LATIN_WORD_RE.search(text)


def _segment_is_primarily_english(segment: str) -> bool:
    raw = (segment or "").strip()
    if not raw:
        return False
    latin = len(_LATIN_WORD_RE.findall(raw))
    arabic = len(_ARABIC_CHAR_RE.findall(raw))
    if latin >= 3 and latin >= arabic:
        return True
    if latin >= 8 and latin > arabic:
        return True
    return False


def _has_commerce_substance(text: str) -> bool:
    return bool(_COMMERCE_SUBSTANCE_RE.search(text or ""))


def _strip_forbidden_residue(text: str) -> Tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    lines_out: List[str] = []

    for line in raw.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if any(pattern.search(ln) for pattern in _GENERIC_AR_VERIFY_RES):
            stripped_any = True
            continue
        cleaned = ln
        for pattern in _FORBIDDEN_RESIDUE_RES:
            new = pattern.sub("", cleaned)
            if new != cleaned:
                stripped_any = True
                cleaned = new
        cleaned = _EMOJI_CHECK_RE.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .،,!…-")
        if cleaned:
            lines_out.append(cleaned)

    return "\n".join(lines_out).strip(), stripped_any


def _strip_english_from_arabic_reply(text: str) -> Tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    paragraphs: List[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        if _segment_is_primarily_english(p):
            stripped_any = True
            continue

        kept_lines: List[str] = []
        for line in p.splitlines():
            ln = line.strip()
            if not ln:
                continue
            segments = re.split(r"(?<=[.!?])\s+", ln)
            kept_segments: List[str] = []
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                if _segment_is_primarily_english(seg):
                    stripped_any = True
                    continue
                inline = seg
                for pattern in _FORBIDDEN_RESIDUE_RES:
                    new = pattern.sub("", inline)
                    if new != inline:
                        stripped_any = True
                        inline = new
                inline = re.sub(r"\s{2,}", " ", inline).strip(" .،,!…-")
                if inline:
                    kept_segments.append(inline)
            if kept_segments:
                kept_lines.append(" ".join(kept_segments))
        if kept_lines:
            paragraphs.append("\n".join(kept_lines))

    return "\n\n".join(paragraphs).strip(), stripped_any


def _is_delivery_turn(
    *,
    intent_name: str,
    primary_customer_goal: str,
    inbound_text: str,
) -> bool:
    intent = (intent_name or "").strip().lower()
    goal = (primary_customer_goal or "").strip().lower()
    if intent == "ask_shipping" or goal == "shipping_inquiry":
        return True
    return bool(_DELIVERY_INBOUND_RE.search(inbound_text or ""))


def _is_short_product_probe(inbound_text: str) -> bool:
    text = _normalize_for_match(inbound_text)
    if not text or len(text) > 16:
        return False
    if _AVAILABILITY_INBOUND_RE.search(inbound_text or ""):
        return False
    if _DELIVERY_INBOUND_RE.search(inbound_text or ""):
        return False
    return bool(re.search(r"[\u0600-\u06FFa-z]", inbound_text or ""))


_NON_COMMERCE_INTENTS = frozenset({
    "social",
    "greeting",
    "general",
    "persona_interaction",
    "who_are_you",
})


def select_arabic_commerce_fallback(
    *,
    intent_name: str = "",
    primary_customer_goal: str = "",
    inbound_text: str = "",
    conversation_objective: str = "",
) -> Tuple[str, str]:
    try:
        from modules.ai.brain.intent.education_context_classifier import (  # noqa: PLC0415
            education_clarify_reply,
            is_education_non_commerce_context,
        )

        if is_education_non_commerce_context(inbound_text):
            return education_clarify_reply(inbound_text), "education"
    except Exception:  # noqa: silent-ok — education gate must not break fallback
        pass

    if _is_delivery_turn(
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        inbound_text=inbound_text,
    ):
        return _FALLBACK_DELIVERY_AR, "delivery"
    goal = (primary_customer_goal or "").strip().lower()
    intent = (intent_name or "").strip().lower()
    if goal in {GOAL_GREETING_ONLY, GOAL_SOCIAL_ONLY} or intent in _NON_COMMERCE_INTENTS:
        norm = _normalize_for_match(inbound_text)
        if norm.startswith("السلام") or "سلام عليكم" in norm:
            return _FALLBACK_GREETING_AR, "greeting"
        return _FALLBACK_SOCIAL_AR, "social"
    if _is_short_product_probe(inbound_text) and (
        goal == GOAL_PRODUCT_AVAILABILITY
        or intent in {"ask_product", "solution_seeking_commerce", "product_availability"}
    ):
        return _FALLBACK_PRODUCT_UNRESOLVED_AR, "product_unresolved"

    try:
        from modules.ai.brain.intent.conversation_objective_guard import (  # noqa: PLC0415
            should_block_availability_fallback,
        )

        if should_block_availability_fallback(
            inbound_text=inbound_text,
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            conversation_objective=conversation_objective,
        ):
            norm = _normalize_for_match(inbound_text)
            if norm.startswith("السلام") or "سلام عليكم" in norm:
                return _FALLBACK_GREETING_AR, "greeting"
            return _FALLBACK_SOCIAL_AR, "social"
    except Exception:  # noqa: silent-ok — objective gate must not break fallback
        pass

    if (
        goal == GOAL_PRODUCT_AVAILABILITY
        or intent in _COMMERCE_INTENTS
    ) and _AVAILABILITY_INBOUND_RE.search(inbound_text or ""):
        return _FALLBACK_AVAILABILITY_AR, "availability"
    norm = _normalize_for_match(inbound_text)
    if norm.startswith("السلام") or "سلام عليكم" in norm:
        return _FALLBACK_GREETING_AR, "greeting"
    return _FALLBACK_SOCIAL_AR, "social"


def _meaningful_length(text: str) -> int:
    return len(re.sub(r"\s+", "", (text or "").strip()))


def apply_commerce_reply_quality_guard(
    reply: str,
    *,
    inbound_text: str = "",
    intent_name: str = "",
    primary_customer_goal: str = "",
    conversation_objective: str = "",
    locale: str = "ar",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> CommerceReplyQualityGuardResult:
    original = (reply or "").strip()
    if not original:
        fallback, kind = select_arabic_commerce_fallback(
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_text=inbound_text,
            conversation_objective=conversation_objective,
        )
        return CommerceReplyQualityGuardResult(
            reply=fallback,
            replaced=True,
            stripped_residue=False,
            stripped_english=False,
            used_fallback=True,
            fallback_kind=kind,
        )

    text = original
    stripped_residue = False
    stripped_english = False

    cleaned, did_residue = _strip_forbidden_residue(text)
    if did_residue:
        stripped_residue = True
        text = cleaned

    if inbound_is_arabic(inbound_text, locale=locale):
        cleaned, did_en = _strip_english_from_arabic_reply(text)
        if did_en:
            stripped_english = True
            text = cleaned

    used_fallback = False
    fallback_kind = ""
    needs_fallback = _meaningful_length(text) < _MIN_MEANINGFUL_CHARS
    if (
        not needs_fallback
        and (stripped_residue or stripped_english)
        and text
        and not _has_commerce_substance(text)
    ):
        needs_fallback = True
    if needs_fallback:
        text, fallback_kind = select_arabic_commerce_fallback(
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_text=inbound_text,
            conversation_objective=conversation_objective,
        )
        used_fallback = True

    replaced = text != original
    if replaced:
        logger.info(
            "[COMMERCE_REPLY_QUALITY_GUARD] tenant=%s conversation=%s "
            "stripped_residue=%s stripped_english=%s used_fallback=%s "
            "fallback_kind=%s orig_len=%d new_len=%d intent=%s",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            stripped_residue,
            stripped_english,
            used_fallback,
            fallback_kind or "-",
            len(original),
            len(text),
            intent_name or "-",
        )

    return CommerceReplyQualityGuardResult(
        reply=text,
        replaced=replaced,
        stripped_residue=stripped_residue,
        stripped_english=stripped_english,
        used_fallback=used_fallback,
        fallback_kind=fallback_kind,
    )


__all__ = [
    "CommerceReplyQualityGuardResult",
    "apply_commerce_reply_quality_guard",
    "inbound_is_arabic",
    "select_arabic_commerce_fallback",
]
