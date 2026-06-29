"""
postprocess/general_image_reply_post_guard.py
──────────────────────────────────────────────
PR-D6D.1 — enforce safe-image compose contract on outbound general media replies.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.brain.postprocess.general_image_reply_post_guard")

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")

_ASK_DESCRIBE_IMAGE_RE = re.compile(
    r"(?:"
    r"لو\s+ت(?:خبر|ق(?:ol|ول)|ع(?:لمن|رف))(?:ني|نا)?\s*(?:"
    r"(?:وش|شنو|إ?يش|ما)\s*(?:ال)?(?:لي\s*)?(?:في\s*)?(?:ال)?صور(?:ه|ة)?"
    r"|(?:عن|في)\s*(?:ال)?(?:محتوى|محتوا)\s*(?:ال)?صور(?:ه|ة)?"
    r")"
    r"|(?:"
    r"(?:وش|شنو|إ?يش|ما)\s*(?:ال)?(?:لي\s*)?(?:في\s*)?(?:ال)?صور(?:ه|ة)?"
    r"|(?:عرف|و(?:ضح|صف))\s*(?:لي\s*)?(?:ال)?(?:محتوى|محتوا)\s*(?:ال)?صور(?:ه|ة)?"
    r")"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_STALE_TOPIC_OFFER_RE = re.compile(
    r"(?:"
    r"(?:ولا|أ?و)\s*استفسار\s*(?:عن|حول)\s+[\w\u0600-\u06FF]{2,40}"
    r"|(?:هل\s+ت(?:ب(?:ي|غ(?:ى|a)?)|(?:ود|د(?:ي|ه))))\s+[\w\u0600-\u06FF]{2,40}\s*\?"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class GeneralImageReplyPostGuardResult:
    reply: str
    replaced: bool
    reason: str = ""


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = _DIA.sub("", t)
    return (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
        .strip()
        .lower()
    )


def _caption_tokens(caption: str) -> set[str]:
    norm = _norm(caption)
    if not norm:
        return set()
    return {tok for tok in norm.split() if len(tok) >= 3}


def _strip_stale_topic_offers(reply: str, *, caption: str) -> Tuple[str, bool]:
    caption_tokens = _caption_tokens(caption)
    updated = reply or ""
    replaced = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal replaced
        phrase = match.group(0)
        tokens = {tok for tok in _norm(phrase).split() if len(tok) >= 3}
        if tokens & caption_tokens:
            return phrase
        replaced = True
        return ""

    updated = _STALE_TOPIC_OFFER_RE.sub(_replace, updated)
    updated = re.sub(r"\s{2,}", " ", updated)
    updated = re.sub(r"\s+([،.!؟?])", r"\1", updated)
    updated = re.sub(r"^[،\s\-–—]+|[،\s\-–—]+$", "", updated.strip())
    return updated, replaced


def apply_general_image_reply_post_guard(
    reply: str,
    *,
    topic: str = "",
    chosen_path: str = "",
    safe_image_facts: Optional[Dict[str, Any]] = None,
    customer_caption: str = "",
    tenant_id: Optional[int] = None,
) -> GeneralImageReplyPostGuardResult:
    raw = reply or ""
    if not raw.strip():
        return GeneralImageReplyPostGuardResult(reply=raw, replaced=False)
    route_topic = str(topic or "").strip()
    if route_topic != "image_ack_or_clarify" and str(chosen_path or "").strip() != "image_ack_or_clarify":
        return GeneralImageReplyPostGuardResult(reply=raw, replaced=False)
    facts = dict(safe_image_facts or {})
    if not facts:
        return GeneralImageReplyPostGuardResult(reply=raw, replaced=False)

    updated = raw
    replaced = False
    reason = ""

    if _ASK_DESCRIBE_IMAGE_RE.search(updated):
        updated = _ASK_DESCRIBE_IMAGE_RE.sub("", updated)
        replaced = True
        reason = "ask_describe_image_removed"

    updated, topic_replaced = _strip_stale_topic_offers(
        updated,
        caption=customer_caption or "",
    )
    if topic_replaced:
        replaced = True
        reason = reason or "stale_topic_offer_removed"

    updated = re.sub(r"\s{2,}", " ", updated).strip()
    if replaced:
        logger.info(
            "[GENERAL_IMAGE_REPLY_POST_GUARD] replaced tenant=%s reason=%s",
            tenant_id,
            reason or "-",
        )
    return GeneralImageReplyPostGuardResult(
        reply=updated,
        replaced=replaced,
        reason=reason,
    )


__all__ = [
    "GeneralImageReplyPostGuardResult",
    "apply_general_image_reply_post_guard",
]
