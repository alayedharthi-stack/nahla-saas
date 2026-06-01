"""
brain/interpret/semantic_turn_interpreter.py
────────────────────────────────────────────
Phase 1 — context-aware repair for short, ambiguous inbound turns.

Claude/LLM improves *interpretation*; guards still own *execution*.
This module runs BEFORE final intent routing on a narrow trigger set:
short replies, typo-like tokens, and deictic references when a strong
conversation anchor exists (size question, listed options, product focus,
active order).

Does NOT call an LLM in Phase 1 — deterministic context + pattern repair
only, to keep latency predictable and avoid overcorrection.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.semantic_interpreter")

INTENT_SHOW_ALL_VARIANTS_OR_PRICES = "show_all_variants_or_prices"
INTENT_SELECT_LIST_OPTION = "select_list_option"
INTENT_ASK_PRICE_SPECIFIC_VARIANT = "ask_price_specific_variant"
INTENT_FULFILLMENT_LOCATION_UPDATE = "fulfillment_location_update"
INTENT_CLARIFY_VARIANTS_NATURAL = "clarify_variants_natural"
INTENT_REFER_LAST_PRODUCT = "refer_last_product"

ANCHOR_LAST_ASSISTANT_SIZE_QUESTION = "last_assistant_size_question"
ANCHOR_LAST_ASSISTANT_LISTED_OPTIONS = "last_assistant_listed_options"
ANCHOR_LAST_PRICE_QUESTION = "last_price_question"
ANCHOR_ACTIVE_PRODUCT_FOCUS = "active_product_focus"
ANCHOR_ACTIVE_ORDER_CONTEXT = "active_order_context"
ANCHOR_VARIANT_SELECTION_PENDING = "variant_selection_pending"

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")

_SIZE_TOKENS = (
    "حجم", "احجام", "أحجام", "حجام", "مقاس", "مقاسات", "size", "sizes",
    "اي حجم", "أي حجم", "which size", "what size",
)

_OPTION_LIST_TOKENS = (
    "اختر", "اختار", "رقم الخيار", "اكتب رقم", "الخيار", "option",
)

_ALL_SIZES_RE = re.compile(
    r"^(?:كل|جميع)\s*(?:ال)?(?:احجام|حجام|احجام|مقاسات|الخيارات|sizes?)?\s*$",
    re.UNICODE | re.IGNORECASE,
)
_ALL_PRONOUN_RE = re.compile(
    r"^(?:كلها|كلهم|كل\s*ال\s*احجام|كل\s*ال\s*حجام|كل\s*ال\s*مقاسات)\s*$",
    re.UNICODE | re.IGNORECASE,
)

_ORDINAL_INDEX: Dict[str, int] = {
    "الاول": 1, "الأول": 1, "اول": 1, "١": 1, "1": 1,
    "الثاني": 2, "الثانيه": 2, "الثانية": 2, "ثاني": 2, "٢": 2, "2": 2,
    "الثالث": 3, "الثالثه": 3, "الثالثة": 3, "ثالث": 3, "٣": 3, "3": 3,
    "الرابع": 4, "رابع": 4, "٤": 4, "4": 4,
}

_SIZE_HINT_MAP: Dict[str, str] = {
    "كبير": "large",
    "كبيره": "large",
    "كبيرة": "large",
    "large": "large",
    "صغير": "small",
    "صغيره": "small",
    "صغيرة": "small",
    "small": "small",
    "وسط": "medium",
    "متوسط": "medium",
    "medium": "medium",
}

_FULFILLMENT_SHORT_RE = re.compile(
    r"(?:ارسل(?:ه|ها)?\s*هنا|أرسل(?:ه|ها)?\s*هنا|وصل(?:ه|ها)?\s*هنا|"
    r"وصل(?:ه|ها)?|الموقع\s*ذا|هذا\s*الموقع|موقعي\s*هنا)",
    re.UNICODE | re.IGNORECASE,
)

_DEICTIC_PRODUCT_RE = re.compile(
    r"^(?:هذا|هذي|هذه|نفس(?:ه|ها|ه)|same\s*one)\s*$",
    re.UNICODE | re.IGNORECASE,
)

_SOCIAL_ONLY_TOKENS = frozenset({
    "آمين", "امين", "amen", "ameen",
    "جزاك الله خير", "جزاكم الله خير",
})


@dataclass(frozen=True)
class SemanticTurnInterpretation:
    """Structured meaning hypothesis for one inbound turn."""
    canonical_text: str
    interpreted_intent: str
    context_anchor: str
    confidence: float
    is_typo_repair: bool = False
    repair_notes: str = ""
    commerce_frame: str = ""
    topic_shift: bool = False
    should_override_social: bool = False
    override_reason: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_text": self.canonical_text,
            "interpreted_intent": self.interpreted_intent,
            "context_anchor": self.context_anchor,
            "confidence": self.confidence,
            "is_typo_repair": self.is_typo_repair,
            "repair_notes": self.repair_notes,
            "commerce_frame": self.commerce_frame,
            "topic_shift": self.topic_shift,
            "should_override_social": self.should_override_social,
            "override_reason": self.override_reason,
            "slots": dict(self.slots or {}),
            "raw_text": self.raw_text,
        }


def normalize_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"[؟?!.,؛:]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _last_assistant_body(history: List[Dict[str, Any]]) -> str:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        direction = str(turn.get("direction") or "").lower()
        if direction in ("out", "outbound", "assistant"):
            return str(turn.get("body") or "").strip()
    return ""


def _has_active_order(state: Any) -> bool:
    op = getattr(state, "order_prep", None)
    if op is None:
        return False
    return bool(
        str(getattr(op, "product_id", "") or "").strip()
        or bool(getattr(op, "missing_fields", None))
        or getattr(op, "awaiting_payment_receipt", False)
        or str(getattr(op, "order_status", "") or "").strip()
        or getattr(op, "awaiting_variant_choice", False)
    )


def detect_context_anchor(
    *,
    state: Any,
    history: List[Dict[str, Any]],
) -> Optional[str]:
    """Return the strongest persisted anchor for this turn, or None."""
    last_q = str(getattr(state, "last_question_asked", "") or "").strip()
    last_asst = _last_assistant_body(history)
    combined = normalize_ar(f"{last_q} {last_asst}")

    op = getattr(state, "order_prep", None)
    if getattr(op, "awaiting_variant_choice", False):
        return ANCHOR_VARIANT_SELECTION_PENDING

    pending_opts = list(getattr(state, "pending_option_groups", None) or [])
    if pending_opts:
        return ANCHOR_LAST_ASSISTANT_SIZE_QUESTION

    if any(tok in combined for tok in _SIZE_TOKENS):
        return ANCHOR_LAST_ASSISTANT_SIZE_QUESTION

    if str(getattr(state, "last_intent", "") or "") == "ask_price":
        return ANCHOR_LAST_PRICE_QUESTION

    if list(getattr(state, "last_search_candidates", None) or []):
        if any(tok in combined for tok in _OPTION_LIST_TOKENS) or "1." in last_asst:
            return ANCHOR_LAST_ASSISTANT_LISTED_OPTIONS
        if getattr(state, "current_product_focus", None):
            return ANCHOR_ACTIVE_PRODUCT_FOCUS

    if _has_active_order(state):
        return ANCHOR_ACTIVE_ORDER_CONTEXT

    if getattr(state, "current_product_focus", None):
        return ANCHOR_ACTIVE_PRODUCT_FOCUS

    return None


def _is_ambiguous_reference(norm: str) -> bool:
    if not norm:
        return False
    if _ALL_SIZES_RE.match(norm) or _ALL_PRONOUN_RE.match(norm):
        return True
    if norm in _ORDINAL_INDEX:
        return True
    if _DEICTIC_PRODUCT_RE.match(norm):
        return True
    if re.search(r"^كم\s+(?:ال)?(?:كبير|صغير|وسط)", norm):
        return True
    if _FULFILLMENT_SHORT_RE.search(norm):
        return True
    if "الحجام" in norm or "حجام" in norm:
        return True
    return False


def _is_typo_like(norm: str, anchor: Optional[str]) -> bool:
    if "الحجام" in norm or re.search(r"\bحجام\b", norm):
        return anchor in (
            ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
            ANCHOR_LAST_PRICE_QUESTION,
            ANCHOR_ACTIVE_PRODUCT_FOCUS,
        )
    return False


def should_run_semantic_interpreter(
    message: str,
    state: Any,
    history: List[Dict[str, Any]],
    *,
    media_semantics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Narrow trigger — avoid running on every message."""
    raw = (message or "").strip()
    if not raw:
        return False

    norm = normalize_ar(raw)

    try:
        from ..order_context_gate import has_explicit_commerce_topic_change  # noqa: PLC0415

        if has_explicit_commerce_topic_change(raw):
            return False
    except Exception:  # noqa: BLE001
        pass

    try:
        from ..commerce.solution_seeking import classify_solution_seeking_commerce  # noqa: PLC0415

        if classify_solution_seeking_commerce(raw) is not None:
            return False
    except Exception:  # noqa: BLE001
        pass

    if norm in {normalize_ar(x) for x in _SOCIAL_ONLY_TOKENS}:
        return False

    anchor = detect_context_anchor(state=state, history=history)
    ambiguous = _is_ambiguous_reference(norm)
    short = len(norm) <= 48
    typo_like = _is_typo_like(norm, anchor)

    if media_semantics and media_semantics.get("block_commerce"):
        return False

    if anchor and (short or ambiguous or typo_like):
        return True

    if ambiguous and short:
        return True

    return False


def _repair_typo(norm: str, anchor: Optional[str]) -> tuple[str, bool, str]:
    if "حجام" in norm and anchor in (
        ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
        ANCHOR_LAST_PRICE_QUESTION,
        ANCHOR_ACTIVE_PRODUCT_FOCUS,
    ):
        return "كل الأحجام", True, "الحجام likely means الأحجام in price/size context"
    return norm, False, ""


def _parse_ordinal(norm: str) -> Optional[int]:
    clean = norm.strip(" .،,")
    if clean in _ORDINAL_INDEX:
        return _ORDINAL_INDEX[clean]
    return None


def _parse_size_price(norm: str) -> Optional[str]:
    m = re.match(r"^كم\s+(?:ال)?(\w+)\s*$", norm)
    if not m:
        return None
    return _SIZE_HINT_MAP.get(m.group(1))


def interpret_semantic_turn(
    *,
    raw_text: str,
    state: Any,
    history: List[Dict[str, Any]],
    media_semantics: Optional[Dict[str, Any]] = None,
) -> Optional[SemanticTurnInterpretation]:
    """Return a structured interpretation or None when no repair applies."""
    raw = (raw_text or "").strip()
    if not raw:
        return None

    if not should_run_semantic_interpreter(raw, state, history, media_semantics=media_semantics):
        return None

    norm = normalize_ar(raw)
    anchor = detect_context_anchor(state=state, history=history)
    canonical, is_typo, repair_notes = _repair_typo(norm, anchor)
    display_canonical = canonical if is_typo else raw

    canon_norm = normalize_ar(canonical if is_typo else norm)
    if (
        _ALL_SIZES_RE.match(canon_norm)
        or _ALL_PRONOUN_RE.match(canon_norm)
        or canon_norm in {"كل الاحجام", "كل احجام", "كل الحجام"}
        or (is_typo and "احجام" in normalize_ar(canonical))
    ):
        if anchor in (
            ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
            ANCHOR_LAST_PRICE_QUESTION,
            ANCHOR_ACTIVE_PRODUCT_FOCUS,
            ANCHOR_VARIANT_SELECTION_PENDING,
        ):
            return SemanticTurnInterpretation(
                raw_text=raw,
                canonical_text=display_canonical if is_typo else "كل الأحجام",
                interpreted_intent=INTENT_SHOW_ALL_VARIANTS_OR_PRICES,
                context_anchor=anchor or ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
                confidence=0.91 if is_typo else 0.88,
                is_typo_repair=is_typo,
                repair_notes=repair_notes,
                commerce_frame="variant_price",
                should_override_social=True,
                override_reason="all_sizes_in_size_context",
            )
        return SemanticTurnInterpretation(
            raw_text=raw,
            canonical_text=display_canonical,
            interpreted_intent=INTENT_CLARIFY_VARIANTS_NATURAL,
            context_anchor=anchor or "",
            confidence=0.72,
            is_typo_repair=is_typo,
            repair_notes=repair_notes or "no size anchor — clarify instead of social",
            commerce_frame="variant_price",
            override_reason="clarify_without_anchor",
        )

    idx = _parse_ordinal(norm)
    if idx is not None and anchor in (
        ANCHOR_LAST_ASSISTANT_LISTED_OPTIONS,
        ANCHOR_VARIANT_SELECTION_PENDING,
        ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
    ):
        return SemanticTurnInterpretation(
            raw_text=raw,
            canonical_text=raw,
            interpreted_intent=INTENT_SELECT_LIST_OPTION,
            context_anchor=anchor,
            confidence=0.90,
            commerce_frame="option_selection",
            slots={"list_index": idx},
            should_override_social=True,
            override_reason=f"ordinal_pick_index_{idx}",
        )

    size_hint = _parse_size_price(norm)
    if size_hint and anchor in (
        ANCHOR_ACTIVE_PRODUCT_FOCUS,
        ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
        ANCHOR_LAST_PRICE_QUESTION,
    ):
        return SemanticTurnInterpretation(
            raw_text=raw,
            canonical_text=raw,
            interpreted_intent=INTENT_ASK_PRICE_SPECIFIC_VARIANT,
            context_anchor=anchor,
            confidence=0.87,
            commerce_frame="variant_price",
            slots={"size_hint": size_hint},
            should_override_social=True,
            override_reason=f"price_for_size_{size_hint}",
        )

    if _DEICTIC_PRODUCT_RE.match(norm) and getattr(state, "current_product_focus", None):
        return SemanticTurnInterpretation(
            raw_text=raw,
            canonical_text=raw,
            interpreted_intent=INTENT_REFER_LAST_PRODUCT,
            context_anchor=ANCHOR_ACTIVE_PRODUCT_FOCUS,
            confidence=0.85,
            commerce_frame="product_focus",
            should_override_social=True,
            override_reason="deictic_last_product",
        )

    if anchor == ANCHOR_ACTIVE_ORDER_CONTEXT and (
        _FULFILLMENT_SHORT_RE.search(norm)
        or _has_address_signal(raw)
    ):
        return SemanticTurnInterpretation(
            raw_text=raw,
            canonical_text=raw,
            interpreted_intent=INTENT_FULFILLMENT_LOCATION_UPDATE,
            context_anchor=anchor,
            confidence=0.89,
            commerce_frame="fulfillment",
            should_override_social=True,
            override_reason="fulfillment_shorthand_active_order",
        )

    return None


def _has_address_signal(message: str) -> bool:
    try:
        from services.address_resolution import extract_address_signals  # noqa: PLC0415

        sig = extract_address_signals(message or "") or {}
        return bool(
            sig.get("google_maps_url")
            or sig.get("short_address_code")
            or sig.get("latitude")
        )
    except Exception:  # noqa: BLE001
        low = (message or "").lower()
        return "maps.google" in low or "goo.gl/maps" in low


def log_semantic_turn_interpretation(
    *,
    tenant_id: Any = None,
    interpretation: SemanticTurnInterpretation,
) -> None:
    """Structured prod log — grep ``[SEMANTIC_TURN_INTERPRETER]``."""
    try:
        i = interpretation
        logger.info(
            "[SEMANTIC_TURN_INTERPRETER] tenant=%s intent=%s canonical=%r "
            "anchor=%s confidence=%.2f typo_repair=%s topic_shift=%s "
            "override_reason=%s raw_preview=%r",
            tenant_id,
            i.interpreted_intent,
            (i.canonical_text or "")[:80],
            i.context_anchor or "-",
            float(i.confidence or 0.0),
            str(bool(i.is_typo_repair)).lower(),
            str(bool(i.topic_shift)).lower(),
            i.override_reason or "-",
            (i.raw_text or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "SemanticTurnInterpretation",
    "detect_context_anchor",
    "interpret_semantic_turn",
    "log_semantic_turn_interpretation",
    "normalize_ar",
    "should_run_semantic_interpreter",
    "INTENT_SHOW_ALL_VARIANTS_OR_PRICES",
    "INTENT_SELECT_LIST_OPTION",
    "INTENT_ASK_PRICE_SPECIFIC_VARIANT",
    "INTENT_FULFILLMENT_LOCATION_UPDATE",
    "INTENT_CLARIFY_VARIANTS_NATURAL",
    "INTENT_REFER_LAST_PRODUCT",
]
