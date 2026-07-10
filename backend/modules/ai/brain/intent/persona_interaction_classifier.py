"""
Persona interaction classifier — playful / emotional / social probes.

Platform-wide, tenant-agnostic. Routes relationship-oriented messages to
``persona_social`` (via ``INTENT_PERSONA_INTERACTION``) so they reach Claude
through a dedicated persona path instead of generic commerce LLM fallback.

Operational messages with complaint/order/problem signals are excluded —
especially bare tease tokens like «فاشلة» when paired with operational context.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger("nahla.brain.persona_interaction")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

PERSONA_KIND_AFFECTION = "affection"
PERSONA_KIND_APPEARANCE = "appearance"
PERSONA_KIND_TEASE = "tease"
PERSONA_KIND_UPSET = "upset"

PERSONA_TOPIC_SOCIAL = "persona_social"

_OPERATIONAL_CONTEXT_RE = re.compile(
    r"(?:"
    r"شكو(?:ى|ي)|مشك(?:لة|له)|خدم(?:ة|ه)\s*سي|"
    r"طلب(?:ي|يتي|ية)?|الطلب(?:ية)?|شحن(?:ة|تي)?|"
    r"تأخ(?:ير|ر)|تاخ(?:ير|ر)|ما\s*وصل|مو\s*وصل|"
    r"ما\s*ج(?:ى|ا)|لم\s*ي(?:صل|رد|ج)|"
    r"complaint|unacceptable|disappointed|frustrated"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_COMMERCIAL_SIGNAL_RE = re.compile(
    r"(?:"
    r"اب(?:ي|غ(?:ى|ا)?)|ار(?:يد|غب)|"
    r"سعر|بكم|كم\s*سعر|"
    r"اطلب|اشتري|"
    r"شحن|توصيل|دفع|حساب|"
    r"عندكم|لديكم|منتج|"
    r"order|price|buy|ship"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_APPEARANCE_RE = re.compile(
    r"^(?:"
    r"(?:انت|أنت|انتي|أنتِ|انتِ)\s+)?"
    r"(?:حلو(?:ة|ه)?|جميل(?:ة|ه)?|حلوو(?:ة|ه)?|"
    r"ذك(?:ية|يه)|ذكي(?:ة|ه)?|لطيف(?:ة|ه)?|كيوت|"
    r"حبيب(?:ة|تي|ي)?|رو(?:عة|عه))"
    r"\s*[\?؟]?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_AFFECTION_RE = re.compile(
    r"^(?:"
    r"اح(?:ب|ب)ك|أ(?:ح|hb)(?:ب|b)ك|"
    r"اشت(?:قت|اقت)(?:\s*(?:لك|الك|لج|الج))?|"
    r"و(?:ح)?(?:شت|ش)(?:ك|چ|ج|ني|نى)?|"
    r"م(?:شت|ش)(?:اق|اق)(?:ة|ه)?(?:\s*(?:لك|الك))?|"
    r"miss\s*you|i\s*love\s*you|love\s*you"
    r")\s*[\?؟!]?\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_UPSET_RE = re.compile(
    r"^(?:"
    r"زعل(?:ان|انه|نت|ت)?(?:\s*من(?:ك|چ|ج))?|"
    r"ز(?:عل|ل)ت(?:\s*من(?:ك|چ|ج))?|"
    r"م(?:ضا|ن)ج(?:ر|ر)|"
    r"ح(?:ز|z)(?:ين|ان)|"
    r"upset|angry(?:\s*at\s*you)?"
    r")\s*[\?؟!]?\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_TEASE_BARE_RE = re.compile(
    r"^(?:"
    r"(?:انت|أنت|انتي|أنتِ|انتِ)\s+)?"
    r"(?:"
    r"فاش(?:ل(?:ة|ه)?|له)|"
    r"ليش\s*ما\s*ت(?:ض|ض)(?:حك|ح)(?:ين|ي|ون)?|"
    r"لي\s*ش\s*ما\s*ت(?:ض|ض)(?:حك|ح)(?:ين|ي|ون)?|"
    r"why\s*(?:don'?t|do\s*not)\s*you\s*laugh"
    r")"
    r"\s*[\?؟!]?\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_TEASE_PLAYFUL_RE = re.compile(
    r"^(?:"
    r"ت(?:ض|ض)حك\s+علينا|"
    r"ت(?:ض|ض)حك\s+علي|"
    r"تمزح(?:ين|ون)?|"
    r"ت(?:مز|مز)ح(?:ين|ون)?|"
    r"(?:كنت\s+)?(?:امزح|أمزح)\s+معك|"
    r"(?:امزح|أمزح)\s+معك"
    r")\s*[\?؟!]?\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_PLAYFUL_NICKNAME_RE = re.compile(
    r"^يا\s+(?:نحله|نحلة|عسل)"
    r"(?:\s+يا\s+(?:نحله|نحلة|عسل))*"
    r"[\s\?؟!.,😄😁🤣😂🌹💛✨🙏]*$"
    ,
    re.IGNORECASE | re.UNICODE,
)

_LAUGHTER_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:ه|ھ){2,}|(?:ها){2,}|ههه+"
    r")[\s\?؟!😄😁🤣😂]*$"
    ,
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class PersonaInteractionMatch:
    persona_topic: str
    persona_kind: str
    confidence: float = 0.94
    pattern: str = ""


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def _has_operational_context(norm_msg: str) -> bool:
    if not norm_msg:
        return False
    if _OPERATIONAL_CONTEXT_RE.search(norm_msg):
        return True
    if _COMMERCIAL_SIGNAL_RE.search(norm_msg):
        return True
    return False


def _blocks_tease_persona(norm_msg: str) -> bool:
    return _has_operational_context(norm_msg)


def _match_kind(norm_msg: str) -> Optional[Tuple[str, str]]:
    if _PLAYFUL_NICKNAME_RE.search(norm_msg):
        if _has_operational_context(norm_msg):
            return None
        return PERSONA_KIND_TEASE, "playful_nickname"
    if _LAUGHTER_ONLY_RE.search(norm_msg):
        return PERSONA_KIND_TEASE, "laughter_only"
    if _TEASE_PLAYFUL_RE.search(norm_msg):
        if _blocks_tease_persona(norm_msg):
            return None
        return PERSONA_KIND_TEASE, "tease_playful"
    if _AFFECTION_RE.search(norm_msg):
        return PERSONA_KIND_AFFECTION, "affection_probe"
    if _APPEARANCE_RE.search(norm_msg):
        if _has_operational_context(norm_msg):
            return None
        return PERSONA_KIND_APPEARANCE, "appearance_compliment"
    if _UPSET_RE.search(norm_msg):
        if _has_operational_context(norm_msg):
            return None
        return PERSONA_KIND_UPSET, "upset_probe"
    if _TEASE_BARE_RE.search(norm_msg):
        if _blocks_tease_persona(norm_msg):
            return None
        return PERSONA_KIND_TEASE, "tease_probe"
    return None


def classify_persona_interaction(message: str) -> Optional[PersonaInteractionMatch]:
    """Return a persona-social match when the message is dominantly relational."""
    raw = (message or "").strip()
    if not raw:
        return None
    norm = _norm(raw)
    if not norm:
        return None
    if len(norm) > 80:
        return None

    hit = _match_kind(norm)
    if hit is None:
        return None

    kind, pattern = hit
    return PersonaInteractionMatch(
        persona_topic=PERSONA_TOPIC_SOCIAL,
        persona_kind=kind,
        confidence=0.94,
        pattern=pattern,
    )


def log_persona_route(
    *,
    tenant_id: Optional[int] = None,
    persona_topic: str = "",
    persona_kind: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[PERSONA_ROUTE] tenant=%s topic=%s kind=%s preview=%r",
            tenant_id if tenant_id is not None else "-",
            persona_topic or "-",
            persona_kind or "-",
            (preview or "")[:64],
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "PERSONA_KIND_AFFECTION",
    "PERSONA_KIND_APPEARANCE",
    "PERSONA_KIND_TEASE",
    "PERSONA_KIND_UPSET",
    "PERSONA_TOPIC_SOCIAL",
    "PersonaInteractionMatch",
    "classify_persona_interaction",
    "log_persona_route",
]
