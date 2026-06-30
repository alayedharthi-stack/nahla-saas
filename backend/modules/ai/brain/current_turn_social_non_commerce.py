"""
current_turn_social_non_commerce.py
──────────────────────────────────
Single-turn ownership detector for social / non-commerce inbound messages.

This module decides whether the *current inbound* must block stale commerce
continuation, catalog replay, and catalog-grounding replacement. It does not
compose customer-facing text.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from .types import (
    INTENT_ASK_COD,
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_ONLINE_STORE_INQUIRY,
    INTENT_PAY_NOW,
    INTENT_PERSONA_INTERACTION,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_COMPLAINT_REFUND,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
)

logger = logging.getLogger("nahla.brain.current_turn_social_non_commerce")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_CLEAR_COMMERCE_INTENTS = frozenset({
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_ASK_SHIPPING,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_COD,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_ONLINE_STORE_INQUIRY,
})

_OPERATIONAL_NON_CATALOG_INTENTS = frozenset({
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_STORE_INFO,
    INTENT_TRACK_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_COMPLAINT_REFUND,
    INTENT_WHO_ARE_YOU,
    INTENT_PERSONA_INTERACTION,
})

_GREETING_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:ال)?سلام\s+عليكم(?:\s+ورحم(?:ة|ه)\s+الله(?:\s+وبركاته?)?)?"
    r"|وعليكم\s+(?:ال)?سلام"
    r"|هلا|يا\s*هلا|مرحبا|مرحب|اهلا|أهلا|اهلين|أهلين"
    r"|صباح\s+الخير|مساء\s+الخير"
    r"|hello|hi|hey"
    r")\s*[،,.!؟?]*$",
    re.UNICODE | re.IGNORECASE,
)

_THANKS_ONLY_RE = re.compile(
    r"^(?:"
    r"شكرا|شكر(?:ا|اً)?|مشكور(?:ه|ة|ين)?|تسلم(?:ين|ون)?|يعطيك\s+العافيه|"
    r"يعطيك\s+العافية|جزاك(?:\s+الله)?(?:\s+خير)?|thank\s*you|thanks"
    r")\s*[،,.!؟?🤍🌷💛🌹]*$",
    re.UNICODE | re.IGNORECASE,
)

_DUA_ONLY_RE = re.compile(
    r"^(?:"
    r"اللهم\s+امين|امين(?:\s+يا\s*رب)?|آمين(?:\s+يا\s*رب)?|"
    r"الله\s+(?:يوفق|يسعد|يرزق|يحفظ|يبارك|يعطيك|يعافيك|يجزاك|يجزيك)"
    r".{0,80}|يا\s*رب.{0,80}|يارب.{0,80}"
    r")\s*[،,.!؟?🤍🌷💛🌹]*$",
    re.UNICODE | re.IGNORECASE,
)

_PRAYER_FRAGMENT_RE = re.compile(
    r"^(?:"
    r"ب?شرك|"
    r"ب?شرك\s+الله|"
    r"ب?شرك\s+الله\s+بالخير"
    r")\s*[،,.!؟?🤍🌷]*$",
    re.UNICODE | re.IGNORECASE,
)

_CONGRATULATIONS_RE = re.compile(
    r"(?:"
    r"مبروك|الف\s+الف\s+مبروك|ألف\s+ألف\s+مبروك|تهانينا|تهنئ|"
    r"بالتوفيق|congrat|congratulations|celebrat"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_LAUGHTER_OR_JOKE_RE = re.compile(
    r"^(?:"
    r"(?:ه|ھ){2,}|(?:ها){2,}|(?:ههه+)|lol|lmao|😂|🤣|😁|😄|😅|"
    r"(?:امزح|أمزح|مزح|دعابه|دعابة|نكت(?:ه|ة)).{0,80}"
    r")\s*$",
    re.UNICODE | re.IGNORECASE,
)

_JOKE_CORRECTION_RE = re.compile(
    r"^(?:قصدي|اقصد|أقصد|كنت\s+امزح|كنت\s+أمزح).{0,120}(?:😂|🤣|😁|😄|مزح|امزح|أمزح|ضحك|طلاب|الطلاب)",
    re.UNICODE | re.IGNORECASE,
)

_UNCLEAR_AUDIO_RE = re.compile(
    r"(?:غير\s+واضح|مو\s+واضح|ما\s+هو\s+واضح|ما\s+فهمت|الصوت\s+غير|"
    r"audio\s+unclear|unclear\s+audio|transcript\s+unclear)",
    re.UNICODE | re.IGNORECASE,
)

_QUANTITY_LIKE_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:نص|نصف|ربع|كيلو|كيلوين|كجم|kg|جرام|gram|grams|علبه|علبة|حبه|حبة)(?:\s|$)"
    r"|(?:^|\s)\d+(?:\.\d+)?\s*(?:kg|كجم|كيلو|جرام|gram|g|علبه|علبة|حبه|حبة)(?:\s|$)"
    r"|(?:^|\s)(?:خذ|خذه|خذها|هب|اعط|أعط)\s+\S{0,24}\s*(?:نص|نصف|ربع|كيلو|كجم|kg)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_EVIDENCE_RE = re.compile(
    r"(?:عسل|سدر|سمر|طلح|ضهيان|شمع|منتج|صنف|كتالوج|catalog|product|honey)",
    re.UNICODE | re.IGNORECASE,
)

_STAFF_IDENTITY_QUESTION_RE = re.compile(
    r"^(?:من|مين|من\s+هو|مين\s+هو|وش\s+هو|ايش\s+هو)\s+\S.{0,40}[\?؟]?$",
    re.UNICODE | re.IGNORECASE,
)

_QUESTION_QUANTITY_MARKERS = (
    "كم الكمية",
    "كم كميه",
    "كم تحتاج",
    "كم تبغ",
    "quantity",
    "كم عدد",
    "الكمية",
    "الكميه",
)


@dataclass(frozen=True)
class CurrentTurnSocialNonCommerce:
    matched: bool
    category: str = ""
    reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "category": self.category,
            "reason": self.reason,
            "confidence": round(float(self.confidence or 0.0), 3),
        }


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )
    return _WS_RE.sub(" ", t).strip()


def _intent_name(intent: Optional[Intent]) -> str:
    return str(getattr(intent, "name", "") or "").strip().lower()


def _intent_confidence(intent: Optional[Intent]) -> float:
    try:
        return float(getattr(intent, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _metadata(inbound_metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    return dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}


def _is_catalog_order_event(inbound_metadata: Optional[dict[str, Any]]) -> bool:
    meta = _metadata(inbound_metadata)
    return str(meta.get("source_type") or "").strip().lower() == "catalog_order"


def _has_explicit_catalog_or_product_intent(
    message: str,
    *,
    intent: Optional[Intent] = None,
    inbound_metadata: Optional[dict[str, Any]] = None,
) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    if _is_catalog_order_event(inbound_metadata):
        return True

    name = _intent_name(intent)
    conf = _intent_confidence(intent)
    if name in _CLEAR_COMMERCE_INTENTS and conf >= 0.80:
        return True
    if name in _OPERATIONAL_NON_CATALOG_INTENTS and conf >= 0.80:
        # Operational ownership is still non-catalog, but not "social".
        return True

    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            has_explicit_product_commerce_intent,
        )

        if has_explicit_product_commerce_intent(raw):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] commerce_evidence_probe_failed")

    try:
        from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
            is_catalog_browse_message,
        )

        if is_catalog_browse_message(raw, intent_name=name):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] commerce_evidence_probe_failed")

    try:
        from modules.ai.brain.commerce.solution_seeking import (  # noqa: PLC0415
            classify_solution_seeking_commerce,
        )

        if classify_solution_seeking_commerce(raw) is not None:
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] commerce_evidence_probe_failed")

    return False


def _quantity_slot_expected(
    *,
    state: Any = None,
    last_question: str = "",
    inbound_metadata: Optional[dict[str, Any]] = None,
) -> bool:
    if _is_catalog_order_event(inbound_metadata):
        return True
    lq = _norm(last_question or "")
    if lq and any(marker in lq for marker in _QUESTION_QUANTITY_MARKERS):
        return True

    op = getattr(state, "order_prep", None) if state is not None else None
    if isinstance(state, dict):
        op = state.get("order_prep")
        lq = _norm(str(state.get("last_question_asked") or ""))
        if lq and any(marker in lq for marker in _QUESTION_QUANTITY_MARKERS):
            return True

    if op is None:
        return False
    if isinstance(op, dict):
        missing = {str(x) for x in (op.get("missing_fields") or [])}
        if "quantity" in missing:
            return True
        return bool(str(op.get("active_order_quantity_clarification") or "").strip())

    missing = {str(x) for x in (getattr(op, "missing_fields", None) or [])}
    if "quantity" in missing:
        return True
    return bool(str(getattr(op, "active_order_quantity_clarification", "") or "").strip())


def _has_product_evidence(message: str) -> bool:
    return bool(_PRODUCT_EVIDENCE_RE.search(_norm(message or "")))


def _is_staff_contact_non_product(message: str, *, intent: Optional[Intent] = None) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    name = _intent_name(intent)
    if name in {INTENT_TALK_HUMAN, INTENT_COMPLAINT_REFUND, INTENT_WHO_ARE_YOU, INTENT_PERSONA_INTERACTION} and _intent_confidence(intent) >= 0.80:
        return False
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            is_staff_or_contact_context,
        )

        if is_staff_or_contact_context(raw):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] staff_contact_probe_failed")

    return bool(_STAFF_IDENTITY_QUESTION_RE.search(_norm(raw)))


def resolve_current_turn_social_non_commerce(
    message: str,
    *,
    intent: Optional[Intent] = None,
    state: Any = None,
    inbound_metadata: Optional[dict[str, Any]] = None,
    nc_match: Any = None,
    last_question: str = "",
) -> CurrentTurnSocialNonCommerce:
    """Classify current inbound ownership before stale commerce can resume."""
    raw = (message or "").strip()
    if not raw:
        return CurrentTurnSocialNonCommerce(False)

    if _has_explicit_catalog_or_product_intent(
        raw,
        intent=intent,
        inbound_metadata=inbound_metadata,
    ):
        return CurrentTurnSocialNonCommerce(False, reason="explicit_commerce_or_operational_intent")

    name = _intent_name(intent)
    slots = dict(getattr(intent, "slots", None) or {})
    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            has_embedded_commerce_inquiry_beyond_greeting,
        )

        if has_embedded_commerce_inquiry_beyond_greeting(raw):
            return CurrentTurnSocialNonCommerce(
                False,
                reason="embedded_commerce_inquiry_beyond_greeting",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] commerce_inquiry_probe_failed")

    if name == INTENT_SOCIAL or slots.get("block_commerce_escalation"):
        return CurrentTurnSocialNonCommerce(
            True,
            category=str(slots.get("social_category") or "social"),
            reason="intent_blocks_commerce",
            confidence=max(_intent_confidence(intent), 0.90),
        )

    if name == INTENT_GREETING and not slots.get("embedded_greeting"):
        return CurrentTurnSocialNonCommerce(
            True,
            category="greeting",
            reason="intent_greeting_only",
            confidence=max(_intent_confidence(intent), 0.90),
        )

    if nc_match is not None and getattr(nc_match, "block_commerce", False):
        return CurrentTurnSocialNonCommerce(
            True,
            category=str(getattr(nc_match, "category", "") or "non_commerce"),
            reason=f"non_commerce:{getattr(nc_match, 'source', '') or 'classifier'}",
            confidence=float(getattr(nc_match, "confidence", 0.90) or 0.90),
        )

    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            classify_non_commerce,
        )

        meta = _metadata(inbound_metadata)
        media_type = str(meta.get("source_type") or meta.get("normalized_type") or "") or None
        nc = classify_non_commerce(raw, media_type=media_type)
        if nc is not None and nc.block_commerce:
            return CurrentTurnSocialNonCommerce(
                True,
                category=nc.category,
                reason=f"non_commerce:{nc.source}",
                confidence=float(nc.confidence or 0.90),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] detector_probe_failed")

    try:
        from modules.ai.brain.intent.social_classifier import classify_social  # noqa: PLC0415

        social = classify_social(raw)
        if social is not None:
            return CurrentTurnSocialNonCommerce(
                True,
                category=str(getattr(social, "category", "") or "social"),
                reason="social_classifier",
                confidence=float(getattr(social, "confidence", 0.90) or 0.90),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CURRENT_TURN_SOCIAL_NON_COMMERCE] detector_probe_failed")

    norm = _norm(raw)
    meta = _metadata(inbound_metadata)
    media_type = str(meta.get("normalized_type") or meta.get("source_type") or "").strip().lower()

    if _GREETING_ONLY_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "greeting", "greeting_only", 0.96)
    if _THANKS_ONLY_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "thanks", "thanks_only", 0.94)
    if _DUA_ONLY_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "dua", "dua_only", 0.94)
    if _PRAYER_FRAGMENT_RE.search(norm):
        return CurrentTurnSocialNonCommerce(
            True, "dua", "prayer_fragment", 0.93,
        )
    if _CONGRATULATIONS_RE.search(norm) and not _has_product_evidence(norm):
        return CurrentTurnSocialNonCommerce(True, "congratulations", "congratulations_only", 0.90)
    if _LAUGHTER_OR_JOKE_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "humor", "laughter_or_joke_only", 0.89)
    if _JOKE_CORRECTION_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "humor", "joke_correction", 0.88)
    if media_type in {"audio", "voice"} and _UNCLEAR_AUDIO_RE.search(norm):
        return CurrentTurnSocialNonCommerce(True, "unclear_audio", "unclear_audio", 0.88)
    if _is_staff_contact_non_product(raw, intent=intent):
        return CurrentTurnSocialNonCommerce(True, "staff_contact", "staff_contact_non_product", 0.88)
    if _QUANTITY_LIKE_RE.search(norm):
        if not _has_product_evidence(norm) and not _quantity_slot_expected(
            state=state,
            last_question=last_question,
            inbound_metadata=inbound_metadata,
        ):
            return CurrentTurnSocialNonCommerce(
                True,
                "quantity_without_product",
                "quantity_without_product_evidence",
                0.87,
            )

    if media_type in {"audio", "voice"} and name in {INTENT_GENERAL, INTENT_HESITATION, ""}:
        return CurrentTurnSocialNonCommerce(True, "unclear_audio", "audio_without_commerce_evidence", 0.80)

    return CurrentTurnSocialNonCommerce(False)


def is_current_turn_social_non_commerce(
    message: str,
    **kwargs: Any,
) -> bool:
    return resolve_current_turn_social_non_commerce(message, **kwargs).matched


__all__ = [
    "CurrentTurnSocialNonCommerce",
    "is_current_turn_social_non_commerce",
    "resolve_current_turn_social_non_commerce",
]
