"""
commerce/price_turn_classifier.py
─────────────────────────────────
Platform-wide price-turn evidence and subject normalization (Phase 2a).

Classifies inbound price-shaped turns using general linguistic categories —
not merchant, product, or dialect-specific phrase lists.

Categories
──────────
* ``unit_price_reference``   — price ask about unit/size only (كيلو، per kg)
* ``product_price_ask``      — resolved product subject + price intent
* ``pronoun_reference``      — سعره/ثمنه/price it with active focus, no new SKU
* ``price_comment``          — evaluation adjective on price (سمح، cheap, fair…)
* ``bare_price_ask``         — بكم / how much with no subject
* ``not_price``              — outside price-turn handling
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, FrozenSet, Optional

logger = logging.getLogger("nahla.brain.price_turn_classifier")


class PriceTurnKind(str, Enum):
    NOT_PRICE = "not_price"
    UNIT_PRICE_REFERENCE = "unit_price_reference"
    PRODUCT_PRICE_ASK = "product_price_ask"
    PRONOUN_REFERENCE = "pronoun_reference"
    PRICE_COMMENT = "price_comment"
    BARE_PRICE_ASK = "bare_price_ask"


# Price-evaluation vocabulary — sentiment about cost, not product names.
_EVALUATION_ADJECTIVES: FrozenSet[str] = frozenset({
    "سمح", "زين", "ممتاز", "مناسب", "غالي", "رخيص", "حلو", "طيب", "عالي",
    "منخفض", "معقول", "مرتفع", "جيد", "سيء", "وافي", "وافيه",
    "good", "great", "cheap", "expensive", "fair", "fine", "nice", "ok", "okay",
    "reasonable", "steep", "high", "low", "affordable",
})

# Leading filler verbs between a price marker and the product subject.
_FILLER_VERB_TOKENS: FrozenSet[str] = frozenset({
    "يطلع", "يكلف", "يجي", "يصير", "يوصل", "تطلع", "تكلف", "تجي", "تصير",
    "costs", "worth", "comes",
})

_PRONOUN_FRAGMENTS: FrozenSet[str] = frozenset({
    "ه", "هو", "هي", "هم", "هن", "it", "its",
})

_UNIT_TOKENS: FrozenSet[str] = frozenset({
    "كilo", "كيلo", "كيلو", "كيلوغرام", "كيلограм", "kg", "gram", "جرام",
    "كجم", "g", "لتر", "ml", "piece", "pack", "حبه", "حبة", "kilo", "litre",
    "liter", "size", "small", "medium", "large", "xl", "xxl",
})

_PRONOUN_PRICE_RE = re.compile(
    r"^(?:سعر|ثمن|تكلفة|price|cost)"
    r"(?:ه|ها|هم|هما)?"
    r"\s*(.*)$",
    re.UNICODE | re.IGNORECASE,
)

_BARE_PRICE_RE = re.compile(
    r"^(?:بكم|كم\s*سعر|سعر|قد\s*ايش|how\s*much|price|cost)\s*$",
    re.UNICODE | re.IGNORECASE,
)

_EN_PRICE_EVAL_RE = re.compile(
    r"^(?:the\s+)?(?:price|cost)\s+(?:is\s+)?(.+)$",
    re.IGNORECASE,
)


def _bare_token(token: str) -> str:
    return re.sub(r"^ال", "", (token or "").strip())


def _is_unit_token(token: str) -> bool:
    bare = _bare_token(token)
    return bare in _UNIT_TOKENS or token in _UNIT_TOKENS


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokenize(subject: str) -> list[str]:
    norm = _normalize_ar(subject)
    norm = re.sub(r"^ال", "", norm)
    return [t for t in norm.split() if t]


def _is_eval_only_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    stripped = [_bare_token(t) for t in tokens]
    return all(t in _EVALUATION_ADJECTIVES for t in stripped if t)


def _has_product_substance_tokens(tokens: list[str]) -> bool:
    """True when tokens contain a catalog subject beyond filler/unit/pronoun."""
    for token in tokens:
        bare = _bare_token(token)
        if bare in _FILLER_VERB_TOKENS or bare in _PRONOUN_FRAGMENTS:
            continue
        if _is_unit_token(token):
            continue
        if len(bare) >= 2:
            return True
    return False


def _strip_subject_tokens(raw_subject: str) -> str:
    """Remove filler verbs and standalone unit tokens from a price subject."""
    tokens = _tokenize(raw_subject)
    while tokens and _bare_token(tokens[0]) in _FILLER_VERB_TOKENS:
        tokens.pop(0)
    tokens = [t for t in tokens if _bare_token(t) not in _PRONOUN_FRAGMENTS]
    if not tokens:
        return ""
    if _is_eval_only_tokens(tokens):
        return ""
    if all(_is_unit_token(t) for t in tokens):
        return ""

    # Trailing unit suffix: ``<product> بكم الكيلو`` → ``<product>``.
    while tokens and _is_unit_token(tokens[-1]):
        tokens.pop()

    if not tokens:
        return ""
    if _is_eval_only_tokens(tokens):
        return ""
    if all(_is_unit_token(t) for t in tokens):
        return ""

    # Leading unit only when it is not part of a product phrase (``كيلو الطلح``).
    while tokens and _is_unit_token(tokens[0]):
        remainder = tokens[1:]
        if remainder and _has_product_substance_tokens(remainder):
            break
        tokens.pop(0)

    if not tokens:
        return ""
    if _is_eval_only_tokens(tokens):
        return ""
    if all(_is_unit_token(t) for t in tokens):
        return ""
    return " ".join(tokens)


def _extract_price_subject_raw(message: str) -> str:
    """Legacy prefix/suffix extraction — unchanged shape detection."""
    from ..product_discovery_gate import _extract_price_subject  # noqa: PLC0415

    return _extract_price_subject(message)


def _has_active_focus(ctx: Any) -> bool:
    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None) or {}
    return bool(isinstance(focus, dict) and str(focus.get("title") or "").strip())


def _is_price_intent(ctx: Any) -> bool:
    from ..intent.rules import INTENT_ASK_PRICE, INTENT_ASK_PRODUCT  # noqa: PLC0415

    name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    return name in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT)


def _is_unit_only_message(message: str) -> bool:
    from ..product_discovery_gate import _is_unit_only_price_message  # noqa: PLC0415

    return _is_unit_only_price_message(message)


def classify_price_turn(ctx: Any) -> PriceTurnKind:
    """Evidence-based price turn classification (platform-wide)."""
    if not _is_price_intent(ctx):
        return PriceTurnKind.NOT_PRICE

    msg = str(getattr(ctx, "message", "") or "").strip()
    if not msg:
        return PriceTurnKind.NOT_PRICE

    norm = _normalize_ar(msg)
    has_focus = _has_active_focus(ctx)

    if _BARE_PRICE_RE.match(norm):
        return PriceTurnKind.BARE_PRICE_ASK

    en_eval_m = _EN_PRICE_EVAL_RE.match(norm)
    if en_eval_m and has_focus:
        tail_tokens = _tokenize(en_eval_m.group(1) or "")
        if tail_tokens and _is_eval_only_tokens(tail_tokens):
            return PriceTurnKind.PRICE_COMMENT

    pronoun_m = _PRONOUN_PRICE_RE.match(norm)
    if pronoun_m and has_focus:
        tail = (pronoun_m.group(1) or "").strip()
        tail_tokens = _tokenize(tail) if tail else []
        if not tail_tokens or _is_eval_only_tokens(tail_tokens):
            if tail_tokens:
                return PriceTurnKind.PRICE_COMMENT
            return PriceTurnKind.PRONOUN_REFERENCE

    if _is_unit_only_message(msg):
        return PriceTurnKind.UNIT_PRICE_REFERENCE

    raw_subject = _extract_price_subject_raw(msg)
    if not raw_subject:
        return PriceTurnKind.BARE_PRICE_ASK

    cleaned = _strip_subject_tokens(raw_subject)
    if not cleaned:
        residual = [
            t for t in _tokenize(raw_subject)
            if _bare_token(t) not in _FILLER_VERB_TOKENS
            and _bare_token(t) not in _PRONOUN_FRAGMENTS
            and not _is_unit_token(t)
        ]
        if _is_eval_only_tokens(residual):
            return PriceTurnKind.PRICE_COMMENT if has_focus else PriceTurnKind.BARE_PRICE_ASK
        raw_tokens = _tokenize(raw_subject)
        if (
            _is_unit_only_message(msg)
            or all(_is_unit_token(t) for t in raw_tokens)
            or (
                not _has_product_substance_tokens(raw_tokens)
                and any(_is_unit_token(t) for t in raw_tokens)
            )
        ):
            return PriceTurnKind.UNIT_PRICE_REFERENCE
        return PriceTurnKind.BARE_PRICE_ASK

    return PriceTurnKind.PRODUCT_PRICE_ASK


def normalize_price_subject(ctx: Any) -> str:
    """
    Return a catalog-safe product query for price turns, or ``""`` when focus
    or clarification should take precedence.
    """
    kind = classify_price_turn(ctx)
    has_focus = _has_active_focus(ctx)

    if has_focus and kind in {
        PriceTurnKind.PRONOUN_REFERENCE,
        PriceTurnKind.PRICE_COMMENT,
        PriceTurnKind.UNIT_PRICE_REFERENCE,
    }:
        return ""

    msg = str(getattr(ctx, "message", "") or "").strip()
    raw = _extract_price_subject_raw(msg)
    if not raw:
        return ""

    cleaned = _strip_subject_tokens(raw)
    if not cleaned:
        return ""

    if kind == PriceTurnKind.PRODUCT_PRICE_ASK:
        return cleaned

    return ""


def log_price_turn_classification(
    ctx: Any,
    *,
    kind: PriceTurnKind,
    normalized: str = "",
) -> None:
    try:
        logger.info(
            "[PRICE_TURN] tenant=%s kind=%s normalized=%r preview=%r focus=%s",
            getattr(ctx, "tenant_id", None),
            kind.value,
            (normalized or "")[:60],
            str(getattr(ctx, "message", "") or "")[:80],
            _has_active_focus(ctx),
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "PriceTurnKind",
    "classify_price_turn",
    "log_price_turn_classification",
    "normalize_price_subject",
]
