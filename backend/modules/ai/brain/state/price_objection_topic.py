"""
brain/state/price_objection_topic.py
Detect wholesale / competitor price objections and negotiation turns.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

# Strong signals — price objection without needing a competitor co-signal.
_WHOLESALE = r"\u0634\u0628\u0647\s*\u062c\u0645\u0644"
_WHY = r"\u0644\u0645\u0627\u0630\u0627"
_WHY2 = r"\u0644\u064a\u0634"
_PRICE_WORD = r"\u0633\u0639\u0631"
_EXPENSIVE = r"\u063a(?:\u0627\u0644\u064a|\u0644\u0649|\u0644\u064a)"
_CHEAPER = r"\u0623?\u0631\u062e\u0635"

_STRONG_PRICE_OBJECTION_RE = re.compile(
    r"(?:"
    + _WHY + r"\s*(?:\u0627\u0644)?(?:" + _PRICE_WORD + r"|\u0627\u0633\u0639\u0627\u0631|\u062b\u0645\u0646)"
    + r"|(?:" + _WHY2 + r"|\u0644\u064a\u0647|" + _WHY + r")\s*(?:\u0643\u0630\u0627|\u0627\u0644\u0633\u0639\u0631|\u0628\u0647\u0630\u0627\s*\u0627\u0644\u0633\u0639\u0631|"
    + _EXPENSIVE + r")"
    + r"|(?:" + _WHY + r"|" + _WHY2 + r"|\u0644\u064a\u0647).{0,60}(?:" + _PRICE_WORD + r"|\u062b\u0645\u0646|"
    + _EXPENSIVE + r")"
    + r"|(?:" + _WHOLESALE + r"[\u0629\u0647]?)"
    + r"|(?:\u062a\u062e\u0641\u064a\u0636)\s*\u062c(?:\u0645\u0644|\u0645\u0644)[\u0629\u0647]?"
    + r"|(?:\u0627\u0628\u063a(?:\u064a|\u0649)|\u0623\u0628\u063a(?:\u064a|\u0649)|\u0628\u063a\u064a\u062a)\s*\u062e\u0635\u0645\s*\u062c(?:\u0645\u0644|\u0645\u0644)[\u0629\u0647]?"
    + r"|\u062e\u0635\u0645\s*\u062c(?:\u0645\u0644|\u0645\u0644)[\u0629\u0647]?"
    + r"|(?:\u0627\u0644)?(?:" + _PRICE_WORD + r"|\u062b\u0645\u0646)\s*(?:"
    + _EXPENSIVE + r"|\u0639(?:\u0627\u0644\u064a|\u0627\u0644\u064a))"
    # Attached pronoun: سعره/سعرها غالي — not only «سعر غالي».
    + r"|(?:\u0627\u0644)?(?:" + _PRICE_WORD + r"|\u0627\u0633\u0639\u0627\u0631|\u062b\u0645\u0646)(?:\u0647|\u0647\u0627|\u0647\u0645)?\s*(?:"
    + _EXPENSIVE + r"|\u0639(?:\u0627\u0644\u064a|\u0627\u0644\u064a))"
    + r"|(?:" + _EXPENSIVE + r")\s*(?:\u064a\u0642\u0648\u0644|\u0642\u0627\u0644|\u0628|\u0639\u0646\u062f)"
    + r"|\u0645\u0642\u0627\u0631\u0646\u0629\s*\u0628\u0627\u0644\u0633\u0648\u0642"
    + r"|(?:" + _CHEAPER + r"\s*\u0645\u0646(?:\u0643\u0645|\u0643)?)"
    + r"|(?:\u0623?\u0631\u062e\u0635|\u0627\u0631\u062e\u0635)\s*\u0645\u0646(?:\u0643\u0645|\u0643)?"
    + r"|(?:\u0625?\u0630\u0627|\u0627\u0630\u0627|\u0644\u0648)\s+.{0,40}(?:\u0646\u0632\u0644|\u0628\u062e\u0635\u0645|\u0623?\u0631\u062e\u0635|\u0628\s*\d{2,4})"
    + r"|(?:\u0623?\u0641\u0643\u0631|\u0627\u0641\u0643\u0631)\s+.{0,40}(?:\u0625?\u0630\u0627|\u0627\u0630\u0627|\u0628\s*\d{2,4})"
    + r")",
    re.UNICODE | re.IGNORECASE,
)

# Competitor / alternate-source context.
_COMPETITOR_SIGNAL_RE = re.compile(
    r"(?:"
    r"\u0639\u0646\u062f\s+\u0645\u0646\u0627\u0641\u0633"
    r"|\u0639\u0646\u062f\s+\u063a\u064a\u0631(?:\u0643\u0645|\u0643|\u0647)?"
    r"|\u0639\u0646\u062f\s+(?:\u0648\u0627\u062d\u062f|\u0641\u0644\u0627\u0646|\u0641\u0644\u0627\u0646\u0647|\u0645\u062d\u0644|\u0645\u062a\u062c\u0631|\u0645\u0646\u0627\u0641\u0633)"
    r"|\b\u0645\u0646\u0627\u0641\u0633(?:\u064a\u0643\u0645|\u064a\u0646|\u0643)?"
    r"|\b\u063a\u064a\u0631(?:\u0643\u0645|\u0643|\u0647)\b"
    r"|\u0644\u0642\u064a\u062a(?:\u0647)?\s+\u0628"
    r"|\u0627\u0634\u062a\u0631\u064a\u062a\s+\u0645\u0646"
    r"|\u0634\u0631\u064a\u062a\s+\u0645\u0646"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_SIGNAL_RE = re.compile(
    r"(?:"
    r"\u0633\u0639\u0631|\u0627\u0633\u0639\u0627\u0631|\u062b\u0645\u0646"
    r"|\u0631\u064a\u0627\u0644"
    r"|\d+\s*\u0631\u064a\u0627\u0644?"
    + r"|"
    + _EXPENSIVE
    + r"|\u0639(?:\u0627\u0644\u064a|\u0627\u0644\u064a)"
    + r"|\u062e\u0635\u0645"
    + r"|\u062c(?:\u0645\u0644|\u0645\u0644)"
    + r"|\d{2,4}"
    + r")",
    re.UNICODE | re.IGNORECASE,
)

_COMPARISON_SIGNAL_RE = re.compile(
    r"(?:"
    + _CHEAPER
    + r"|\u0645\u0642\u0627\u0631\u0646\u0629"
    + r"|\u0623?\u063a\u0644\u0649"
    + r")",
    re.UNICODE | re.IGNORECASE,
)

_YA_GHALI_RE = re.compile(r"\u064a\u0627\s*\u063a(?:\u0627\u0644\u064a|\u0644\u0649)\b", re.UNICODE)

_BARE_EXPENSIVE_WITH_PRICE_CONTEXT_RE = re.compile(
    r"(?:"
    r"^" + _EXPENSIVE + r"(?:\s|$)"
    r"|(?:^|\s)" + _EXPENSIVE + r"\s*(?:\u064a\u0642\u0648\u0644|\u0642\u0627\u0644|\?\s*$)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PAST_PURCHASE_COMPARISON_RE = re.compile(
    r"(?:"
    r"\u0627\u0634\u062a\u0631\u064a\u062a|\u0627\u062e\u0630\u062a|\u0623\u062e\u0630\u062a|\u0634\u0631\u064a\u062a"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_CURRENT_BUY_RE = re.compile(
    r"(?:"
    r"(?:\u0623?\u0628\u063a(?:\u064a|\u0649)|\u0627\u0628\u063a(?:\u064a|\u0649)|\u0623?\u0628\u064a|\u0627\u0628\u064a|\u0628\u062f\u064a|\u0628\u062f\u064a)"
    r"\s*(?:\u0623?\u0637\u0644\u0628|\u0627\u0637\u0644\u0628|\u0623?\u0634\u062a\u0631\u064a|\u0627\u0634\u062a\u0631\u064a|\u0622\u062e\u0630|\u0623\u062e\u0630|\u0627\u062e\u0630|\u062e\u0630|\u062c\u0647\u0632|\u0627\u062c\u0647\u0632|\u0623\u062c\u0647\u0632)"
    r"|(?:\u0623?\u0637\u0644\u0628|\u0627\u0637\u0644\u0628|\u0623?\u0634\u062a\u0631\u064a|\u0627\u0634\u062a\u0631\u064a|\u0622\u062e\u0630|\u0623\u062e\u0630|\u0627\u062e\u0630|\u062e\u0630|\u062c\u0647\u0632|\u0627\u062c\u0647\u0632|\u0623\u062c\u0647\u0632)"
    r"\s*(?:\u0644\u064a\s+)?\d{1,4}"
    r"|(?:\u062c\u0647\u0632|\u0627\u062c\u0647\u0632|\u0623\u062c\u0647\u0632)\s+\u0644\u064a"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CONDITIONAL_QUANTITY_RE = re.compile(
    r"(?:"
    r"(?:\u0623?\u0641\u0643\u0631|\u0627\u0641\u0643\u0631|\u0644\u0648|\u0625?\u0630\u0627|\u0627\u0630\u0627)"
    r".{0,50}\d{1,4}"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_NUMBER_RE = re.compile(r"\d{2,4}(?:\.\d{1,2})?")


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def _extract_price_numbers(text: str) -> List[float]:
    nums: List[float] = []
    for raw in _PRICE_NUMBER_RE.findall(text or ""):
        try:
            nums.append(float(raw))
        except (TypeError, ValueError):
            continue
    return nums


def _has_price_objection_signals(norm: str) -> bool:
    if _STRONG_PRICE_OBJECTION_RE.search(norm):
        return True
    if _BARE_EXPENSIVE_WITH_PRICE_CONTEXT_RE.search(norm) and (
        _PRICE_SIGNAL_RE.search(norm) or _PRICE_NUMBER_RE.search(norm)
    ):
        return True
    has_competitor = bool(_COMPETITOR_SIGNAL_RE.search(norm))
    has_price = bool(_PRICE_SIGNAL_RE.search(norm))
    has_comparison = bool(_COMPARISON_SIGNAL_RE.search(norm))
    if has_competitor and (has_price or has_comparison):
        return True
    if _PAST_PURCHASE_COMPARISON_RE.search(norm) and (
        has_price or has_competitor or _PRICE_NUMBER_RE.search(norm)
    ):
        return True
    if _CONDITIONAL_QUANTITY_RE.search(norm) and (
        bool(re.search(_EXPENSIVE, norm))
        or has_competitor
        or "سعر" in norm
    ):
        return True
    return False


def detect_price_objection_topic_shift(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _normalize(raw)
    if not norm:
        return False
    if _YA_GHALI_RE.search(norm) and not _has_price_objection_signals(
        _YA_GHALI_RE.sub(" ", norm),
    ):
        return False
    return _has_price_objection_signals(norm)


def is_past_purchase_comparison_message(message: str) -> bool:
    """Past-tense purchase narrative used for comparison — not current buy intent."""
    norm = _normalize(message or "")
    if not norm:
        return False
    if not _PAST_PURCHASE_COMPARISON_RE.search(norm):
        return False
    if has_explicit_current_buy_quantity_intent(message):
        return False
    return bool(
        _COMPETITOR_SIGNAL_RE.search(norm)
        or _PRICE_NUMBER_RE.search(norm)
        or bool(re.search(_EXPENSIVE, norm))
        or "سعر" in norm
    )


def has_explicit_current_buy_quantity_intent(message: str) -> bool:
    """Present/future explicit buy intent with quantity — quantity follow-up allowed."""
    norm = _normalize(message or "")
    if not norm:
        return False
    if _EXPLICIT_CURRENT_BUY_RE.search(norm):
        return True
    return False


def should_suppress_quantity_followup(message: str) -> bool:
    if not detect_price_objection_topic_shift(message):
        return False
    return not has_explicit_current_buy_quantity_intent(message)


def customer_claimed_price_numbers(message: str) -> Set[int]:
    """Numbers the customer mentioned as competitor/negotiation prices — not store prices."""
    if not detect_price_objection_topic_shift(message):
        return set()
    out: Set[int] = set()
    for val in _extract_price_numbers(message):
        try:
            out.add(int(round(val)))
        except (TypeError, ValueError):
            continue
    return out


def build_price_objection_facts(message: str) -> Dict[str, Any]:
    """Structured negotiation facts for LLM compose — not a reply template."""
    nums = _extract_price_numbers(message)
    competitor_price: Optional[float] = None
    mentioned_catalog_or_expected: Optional[float] = None
    possible_bulk_qty: Optional[int] = None

    norm = _normalize(message)
    for match in re.finditer(
        r"(?:\u064a\u0642\u0648\u0644|\u0642\u0627\u0644|\u0628)\s*(\d{2,4})",
        norm,
    ):
        try:
            mentioned_catalog_or_expected = float(match.group(1))
        except (TypeError, ValueError):
            pass

    for match in re.finditer(
        r"(?:\u0628|\u0639\u0646\u062f)\s*(\d{2,4})",
        norm,
    ):
        try:
            competitor_price = float(match.group(1))
        except (TypeError, ValueError):
            pass

    qty_match = re.search(
        r"(?:\u0623?\u062e\u0630|\u0627\u062e\u0630|\u0623?\u062e\u0630\u062a|\u0627\u062e\u0630\u062a|\u0622\u062e\u0630)"
        r"\s*(\d{1,4})",
        norm,
    )
    if qty_match:
        try:
            possible_bulk_qty = int(qty_match.group(1))
        except (TypeError, ValueError):
            possible_bulk_qty = None

    if competitor_price is None and len(nums) >= 1:
        for val in nums:
            if mentioned_catalog_or_expected is not None and val == mentioned_catalog_or_expected:
                continue
            competitor_price = val
            break

    return {
        "customer_intent": "price_objection",
        "competitor_price_claim": competitor_price,
        "mentioned_catalog_or_expected_price": mentioned_catalog_or_expected,
        "possible_bulk_quantity": possible_bulk_qty,
        "customer_claimed_price_numbers": sorted({int(n) for n in nums}),
        "must_not_ask_quantity_yet": not has_explicit_current_buy_quantity_intent(message),
        "must_not_claim_catalog_price_missing_if_catalog_has_price": True,
        "must_not_offer_unapproved_discount": True,
        "must_not_confirm_discount": True,
    }


def enrich_price_objection_facts_with_active_order(
    facts: Dict[str, Any],
    *,
    state: Any = None,
    order_prep: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach active catalog-order context so price objections stay order-scoped."""
    out = dict(facts or {})
    prep: Dict[str, Any] = {}
    if isinstance(order_prep, dict):
        prep = dict(order_prep)
    elif order_prep is not None and hasattr(order_prep, "to_dict"):
        try:
            prep = dict(order_prep.to_dict())
        except Exception:  # noqa: BLE001
            prep = {}
    elif state is not None:
        raw_prep = getattr(state, "order_prep", None)
        if isinstance(raw_prep, dict):
            prep = dict(raw_prep)
        elif raw_prep is not None and hasattr(raw_prep, "to_dict"):
            try:
                prep = dict(raw_prep.to_dict())
            except Exception:  # noqa: BLE001
                prep = {}

    meta = dict(inbound_metadata or {})
    line_items = list(prep.get("line_items") or [])
    if not line_items and state is not None:
        line_items = list(getattr(state, "cart_items", None) or [])

    active = bool(
        prep.get("catalog_line_items_authoritative")
        or line_items
        or prep.get("catalog_checkout_total") is not None
        or str(meta.get("source_type") or "").strip().lower() == "catalog_order"
    )
    if not active:
        return out

    total = prep.get("catalog_checkout_total") or prep.get("order_total")
    if total is None:
        total = meta.get("total_price")

    claimed = out.get("mentioned_catalog_or_expected_price")
    if claimed is None and out.get("competitor_price_claim") is not None:
        out["customer_claimed_competitor_or_expected_price"] = out.get(
            "competitor_price_claim"
        )
    elif claimed is not None:
        out["customer_claimed_competitor_or_expected_price"] = claimed

    out.update({
        "active_catalog_order": True,
        "current_order_total": total,
        "current_order_line_items_count": len(line_items) or int(
            meta.get("line_items_count") or 0
        ) or None,
        "must_not_ask_which_product_if_active_order_exists": True,
    })
    return out


__all__ = [
    "build_price_objection_facts",
    "customer_claimed_price_numbers",
    "detect_price_objection_topic_shift",
    "enrich_price_objection_facts_with_active_order",
    "has_explicit_current_buy_quantity_intent",
    "is_past_purchase_comparison_message",
    "should_suppress_quantity_followup",
]
