"""Pure A1 tuple-link classification shared by write and report paths."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from services.order_customer_identity_contract import (
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_VERIFIED,
)

LINKED = "linked"
UNMAPPED = "unmapped"
MISLINKED = "mislinked"


@dataclass(frozen=True)
class TupleLinkageCounts:
    linked: int = 0
    unmapped: int = 0
    mislinked: int = 0

    @property
    def orders_in_scope(self) -> int:
        return self.linked + self.unmapped + self.mislinked


def classify_external_tuple_order(*, order: Any, profile_id: Any) -> str:
    """Classify one order relative to its authoritative external tuple owner."""
    if (
        order.external_customer_profile_id == profile_id
        and order.external_identity_link_state == LINK_STATE_VERIFIED
        and order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE
    ):
        return LINKED
    if order.external_customer_profile_id is None:
        return UNMAPPED
    return MISLINKED


def classify_internal_customer_order(*, order: Any) -> str:
    """Classify one internal order relative to its canonical customer owner."""
    if (
        order.customer_link_state == LINK_STATE_VERIFIED
        and order.customer_link_evidence_class == EVIDENCE_AUTHORITATIVE
    ):
        return LINKED
    if order.customer_id is None:
        return UNMAPPED
    return MISLINKED


def count_classifications(classifications: Iterable[str]) -> TupleLinkageCounts:
    """Deterministically aggregate classifications emitted by either path."""
    linked = unmapped = mislinked = 0
    for classification in classifications:
        if classification == LINKED:
            linked += 1
        elif classification == UNMAPPED:
            unmapped += 1
        elif classification == MISLINKED:
            mislinked += 1
        else:
            raise ValueError("unknown_tuple_linkage_classification")
    return TupleLinkageCounts(linked=linked, unmapped=unmapped, mislinked=mislinked)


__all__ = [
    "LINKED",
    "MISLINKED",
    "UNMAPPED",
    "TupleLinkageCounts",
    "classify_external_tuple_order",
    "classify_internal_customer_order",
    "count_classifications",
]
