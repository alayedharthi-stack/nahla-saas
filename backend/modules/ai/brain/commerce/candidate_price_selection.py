"""
candidate_price_selection.py
────────────────────────────
Use customer-stated prices as product-selection constraints (not order prices).

Reuses ``extract_reply_prices`` / ``parse_price_amount`` from
``product_claim_grounding_evidence`` — no parallel parsing system.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from modules.ai.brain.postprocess.product_claim_grounding_evidence import (
    _CURRENCY_TOKEN_RE,
    extract_reply_prices,
    parse_price_amount,
)

_AR_DEF_ARTICLE = "\u0627\u0644"

_STATED_PRICE_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"سعر(?:ه|ها|هم)?\s*[:=]?\s*(\d[\d,\.]{0,7})"
    r"|بسعر\s*(\d[\d,\.]{0,7})"
    r"|(?:^|\s)(\d[\d,\.]{0,7})\s*(?:ريال|r\.?\s?س\.?|sar)\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\u0621-\u064Aa-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_stated_price_constraint(message: str) -> Optional[int]:
    """Return an explicit inbound price used as a product-selection constraint."""
    text = str(message or "").strip()
    if not text:
        return None
    match = _STATED_PRICE_CONSTRAINT_RE.search(text)
    if match:
        for group in match.groups():
            if group:
                amount = parse_price_amount(group)
                if amount is not None:
                    return amount
    prices = extract_reply_prices(text)
    if len(prices) == 1 and _CURRENCY_TOKEN_RE.search(text):
        return next(iter(prices))
    return None


def _candidate_price_amount(candidate: Dict[str, Any]) -> Optional[int]:
    return parse_price_amount(candidate.get("price"))


def _title_match_score(msg_norm: str, title: str) -> int:
    title_norm = _normalize_ar(title)
    if not msg_norm or not title_norm or len(title) < 2:
        return 0
    if title_norm == msg_norm:
        return 100
    if title_norm in msg_norm:
        return 80
    if msg_norm in title_norm and len(msg_norm) >= 3:
        return 60
    title_words = [w for w in title_norm.split() if len(w) >= 2]
    if title_words and all(w in msg_norm for w in title_words):
        return 40 + len(title_words) * 5
    bare_title = (
        title_norm[len(_AR_DEF_ARTICLE):]
        if title_norm.startswith(_AR_DEF_ARTICLE) and len(title_norm) > 3
        else title_norm
    )
    if bare_title and bare_title in msg_norm:
        return 75
    bare_msg = (
        msg_norm[len(_AR_DEF_ARTICLE):]
        if msg_norm.startswith(_AR_DEF_ARTICLE) and len(msg_norm) > 3
        else msg_norm
    )
    if bare_title and bare_msg and bare_title == bare_msg:
        return 90
    return 0


def filter_candidates_matching_message(
    message: str,
    candidates: Sequence[Dict[str, Any]],
    *,
    min_score: int = 40,
) -> List[Dict[str, Any]]:
    """Return candidates whose title matches the inbound message (same-name pool)."""
    msg_norm = _normalize_ar(message)
    if not msg_norm:
        return []
    matched: List[Dict[str, Any]] = []
    for prod in candidates:
        title = str(prod.get("title") or "").strip()
        if _title_match_score(msg_norm, title) >= min_score:
            matched.append(prod)
    return matched


@dataclass(frozen=True)
class CandidatePriceResolution:
    kind: str
    selected: Optional[Dict[str, Any]] = None
    candidates: tuple[Dict[str, Any], ...] = ()
    stated_price: Optional[int] = None


def resolve_candidates_by_stated_price(
    message: str,
    candidates: Sequence[Dict[str, Any]],
) -> CandidatePriceResolution:
    """
    Narrow same-name candidates by an exact stated price (no fuzzy matching).

    Returns:
      - selected: exactly one name+price match
      - clarify: multiple name+price matches at the same stated price
      - no_match: stated price absent from the name-matched pool
      - unchanged: no stated price to apply
    """
    stated = extract_stated_price_constraint(message)
    if stated is None or not candidates:
        return CandidatePriceResolution(kind="unchanged")

    name_pool = filter_candidates_matching_message(message, candidates)
    pool = name_pool if name_pool else list(candidates)

    price_matched = [
        prod for prod in pool if _candidate_price_amount(prod) == stated
    ]
    if len(price_matched) == 1:
        return CandidatePriceResolution(
            kind="selected",
            selected=price_matched[0],
            stated_price=stated,
        )
    if len(price_matched) > 1:
        return CandidatePriceResolution(
            kind="clarify",
            candidates=tuple(price_matched),
            stated_price=stated,
        )
    return CandidatePriceResolution(
        kind="no_match",
        candidates=tuple(pool),
        stated_price=stated,
    )


__all__ = [
    "CandidatePriceResolution",
    "extract_stated_price_constraint",
    "filter_candidates_matching_message",
    "resolve_candidates_by_stated_price",
]
