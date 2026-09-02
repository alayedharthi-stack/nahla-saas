"""Canonical Order Support ownership — structured provenance, not wording.

Social-NC, product-information, ledger stamping, and checkout-resume must
all consult this contract. A bare low-confidence ``track_order`` label
without Layer-2 authoritative provenance is not Order Support.
"""
from __future__ import annotations

from typing import Any

from ..intent.classifier import PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
from ..types import (
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
    INTENT_TRACK_ORDER,
)

ORDER_SUPPORT_INTENTS = frozenset(
    {
        INTENT_TRACK_ORDER,
        INTENT_ORDER_HISTORY_COUNT,
        INTENT_LATEST_ORDER_SUMMARY,
        INTENT_ORDER_REFERENCE_LIST,
    }
)

# Same operational gate used by social-NC. Must not be lowered.
_AUTHORITATIVE_INTENT_CONFIDENCE = 0.80
_LAYER2_AUTHORITATIVE_WINNER = "layer2"


def _intent_name(intent: Any) -> str:
    return str(getattr(intent, "name", "") or "").strip().lower()


def _intent_confidence(intent: Any) -> float:
    try:
        return float(getattr(intent, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _layer2_authoritative_provenance(intent: Any) -> bool:
    slots = dict(getattr(intent, "slots", None) or {})
    provenance = str(slots.get("classification_provenance") or "").strip()
    winner = str(slots.get("precedence_winner") or "").strip()
    return (
        provenance == PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        and winner == _LAYER2_AUTHORITATIVE_WINNER
    )


def has_authoritative_order_support_ownership(
    intent: Any = None,
    *,
    state: Any = None,
) -> bool:
    """True when structured signals prove Order Support owns the current turn.

    Authoritative sources (any one is sufficient, all are structural):

    1. Order-support intent at the existing ``>= 0.80`` operational gate.
    2. Order-support intent with closed Layer-2 provenance
       (``LAYER2_SEMANTIC_OVERRIDE`` + winner ``layer2``).

    ``state`` is accepted so callers can pass conversation state uniformly.
    Ledger/recent-topic context alone must not activate this contract — a
    noisy low-confidence ``track_order`` label without provenance stays out
    even when a prior ledger topic is still active.

    Does not inspect customer wording.
    """
    # ``state`` is part of the shared caller contract. It must not activate
    # ownership on its own — noisy track_order plus leftover ledger topic
    # is still not Order Support.
    _ = state
    name = _intent_name(intent)
    if name not in ORDER_SUPPORT_INTENTS:
        return False
    if _intent_confidence(intent) >= _AUTHORITATIVE_INTENT_CONFIDENCE:
        return True
    return _layer2_authoritative_provenance(intent)


def should_stamp_ledger_context(
    intent: Any = None,
    *,
    state: Any = None,
) -> bool:
    """Ledger context may be stamped only on a proven Order Support-owned turn."""
    return has_authoritative_order_support_ownership(intent, state=state)


__all__ = [
    "ORDER_SUPPORT_INTENTS",
    "has_authoritative_order_support_ownership",
    "should_stamp_ledger_context",
]
