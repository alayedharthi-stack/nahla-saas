"""
brain/state/product_correction.py
─────────────────────────────────
Detect customer product corrections / rejections so stale focus and
order_prep do not survive explicit negation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

_NEGATION_PRODUCT_RE = re.compile(
    r"(?:"
    r"لا\s*(?:مش|مو|ماهو|ما\s*هو|هذا|هذي|هذه)\s+"
    r"|(?:لا|مو)\s*,?\s*(?:هذا|هذي|هذه|المنتج)\s*(?:غلط|خطأ|مو\s*صح|مش\s*صح)?"
    r"|(?:هذا|هذي|هذه|المنتج)\s*(?:غلط|خطأ|مو\s*صح|مش\s*صح)"
    r"|(?:لا|مو)\s*,?\s*المنتج\s*(?:اللي|الي|الذي)\s*(?:بالصور(?:ه|ة)|المرسل(?:ه|ة)|ف(?:ي)?\s*الصور(?:ه|ة))"
    r"|not\s+this\s+product|wrong\s+product"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_REPLACEMENT_PREFIX_RE = re.compile(
    r"(?:"
    r"أ?قصد\s+"
    r"|(?:^|[\s،,.])(?:لا|مو)\s*(?:مش|مو)?\s+[^،,.!\n]{0,60}?\s*[,،]\s*"
    r"|(?:^|[\s،,.])(?:لا|مو)\s*[,،]\s*"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_HINT_RE = re.compile(
    r"(?:عسل|خلط(?:ه|ة)?|منتج(?:ات)?|طلح|سدر|سمر|برسيم|شمع|حلاو)",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


@dataclass(frozen=True)
class ProductCorrectionVerdict:
    detected: bool = False
    replacement_query: str = ""
    rejected_label: str = ""


def detect_product_correction(message: str) -> bool:
    return parse_product_correction(message).detected


def parse_product_correction(message: str) -> ProductCorrectionVerdict:
    raw = (message or "").strip()
    if not raw:
        return ProductCorrectionVerdict()

    norm = _normalize(raw)
    if not _NEGATION_PRODUCT_RE.search(norm) and not _REPLACEMENT_PREFIX_RE.search(norm):
        if not re.search(r"^\s*لا\s*[,،]", norm):
            return ProductCorrectionVerdict()

    replacement = extract_replacement_product_query(raw)
    rejected = ""
    m = re.search(
        r"(?:لا\s*(?:مش|مو)\s+|(?:لا|مو)\s+)([^\،,.!\n]{2,80})",
        norm,
    )
    if m:
        rejected = m.group(1).strip()

    return ProductCorrectionVerdict(
        detected=True,
        replacement_query=replacement,
        rejected_label=rejected,
    )


def extract_replacement_product_query(message: str) -> str:
    raw = (message or "").strip()
    if not raw:
        return ""

    norm = _normalize(raw)

    m = re.search(r"^\s*أ?قصد\s+(.+)$", norm)
    if m:
        return _clean_replacement(m.group(1))

    parts = re.split(r"[,،]", raw)
    if len(parts) >= 2:
        tail = parts[-1].strip()
        if _PRODUCT_HINT_RE.search(tail):
            return _clean_replacement(tail)

    m = re.search(
        r"(?:لا\s*(?:مش|مو)\s+[^،,.!\n]+[,،]\s*)(.+)$",
        raw,
        re.UNICODE | re.IGNORECASE,
    )
    if m and _PRODUCT_HINT_RE.search(m.group(1)):
        return _clean_replacement(m.group(1))

    if _NEGATION_PRODUCT_RE.search(norm) and _PRODUCT_HINT_RE.search(raw):
        for chunk in re.split(r"[,،]", raw):
            chunk = chunk.strip()
            if chunk and not re.match(r"^\s*لا\b", _normalize(chunk)):
                if _PRODUCT_HINT_RE.search(chunk) and len(chunk) >= 6:
                    return _clean_replacement(chunk)

    return ""


def _clean_replacement(text: str) -> str:
    t = re.sub(r"^\s*(?:لا|مو|مش|هذا|هذي|هذه|المنتج)\s+", "", (text or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" .،,!؟?")
    return t[:200]


def clear_stale_product_state_for_correction(state: Any, message: str = "") -> None:
    """Drop stale product assumptions after an explicit correction turn."""
    if state is None:
        return

    try:
        from ..commerce.commerce_focus_owner import try_ordinal_correction_focus_swap  # noqa: PLC0415

        if try_ordinal_correction_focus_swap(state, message or ""):
            return
    except Exception:  # noqa: BLE001
        pass

    state.current_product_focus = None
    state.previous_product_focus = None
    state.suspended_product_focus = None
    state.conversation_focus = ""
    state.last_search_candidates = []

    op = getattr(state, "order_prep", None)
    if op is None:
        return

    op.product_id = ""
    op.missing_fields = []
    op.awaiting_location_text = False
    if hasattr(op, "line_items"):
        op.line_items = []
    if hasattr(state, "cart_items"):
        state.cart_items = []

    stage = str(getattr(state, "stage", "") or "")
    if stage in ("ordering", "checkout", "deciding"):
        state.stage = "exploring"


__all__ = [
    "ProductCorrectionVerdict",
    "clear_stale_product_state_for_correction",
    "detect_product_correction",
    "extract_replacement_product_query",
    "parse_product_correction",
]
