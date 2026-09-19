"""
core/customer_address_persistence_evidence.py
─────────────────────────────────────────────
Structured evidence for what an address operation actually persisted.

The scope of an address write is not one thing. These are different facts
and the customer-facing claim they support is different:

* ``CONVERSATION_STATE`` — the turn's order/conversation state holds the
  address. Nothing durable about the customer changed.
* ``IMPORTED_CANDIDATE`` — a durable, source-labelled candidate exists on
  the customer record. It has NOT been adopted as the delivery address.
* ``SELECTED_DELIVERY_ADDRESS`` — the customer explicitly selected this
  exact address revision as their delivery address.

Evidence is only produced by re-reading the committed row. A writer that
returned ``True``, a ``db.add`` that has not been committed, a bridge skip,
an unavailable capability, an ambiguous identity and a failed commit all
resolve to ``NONE`` and support no save claim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.customer_address_persistence_evidence")


class AddressPersistenceScope(str, Enum):
    NONE = "none"
    CONVERSATION_STATE = "conversation_state"
    IMPORTED_CANDIDATE = "imported_candidate"
    SELECTED_DELIVERY_ADDRESS = "selected_delivery_address"


_RANK = {
    AddressPersistenceScope.NONE: 0,
    AddressPersistenceScope.CONVERSATION_STATE: 1,
    AddressPersistenceScope.IMPORTED_CANDIDATE: 2,
    AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS: 3,
}


@dataclass(frozen=True)
class CustomerAddressPersistenceEvidence:
    scope: AddressPersistenceScope = AddressPersistenceScope.NONE
    reason: str = ""
    address_id: Optional[int] = None
    fingerprint: str = ""
    components: Dict[str, Any] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        """True only for a scope proven by a committed customer-address row."""
        return _RANK[self.scope] >= _RANK[AddressPersistenceScope.IMPORTED_CANDIDATE]

    def allows_saved_address_claim(self) -> bool:
        """May the reply say an address is held on the customer record?"""
        return self.committed

    def allows_adopted_address_claim(self) -> bool:
        """May the reply say the address was adopted as THE delivery address?"""
        return self.scope is AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope.value,
            "reason": self.reason,
            "address_id": self.address_id,
            "fingerprint": self.fingerprint,
            "committed": self.committed,
            "allows_saved_address_claim": self.allows_saved_address_claim(),
            "allows_adopted_address_claim": self.allows_adopted_address_claim(),
        }


def state_only_evidence(reason: str) -> CustomerAddressPersistenceEvidence:
    """A conversation/order-state write — never durable customer-address proof."""
    return CustomerAddressPersistenceEvidence(
        scope=AddressPersistenceScope.CONVERSATION_STATE,
        reason=reason or "conversation_state_only",
    )


def no_evidence(reason: str) -> CustomerAddressPersistenceEvidence:
    return CustomerAddressPersistenceEvidence(
        scope=AddressPersistenceScope.NONE,
        reason=reason or "no_persistence",
    )


def resolve_customer_address_persistence_evidence(
    db: Any,
    *,
    tenant_id: Optional[int],
    customer_id: Optional[int],
    address_id: Optional[int] = None,
    fallback_reason: str = "",
) -> CustomerAddressPersistenceEvidence:
    """Re-read the customer record and report what is actually there.

    Called AFTER the transaction the caller expects to have committed. A
    row that is not there — because the commit failed, because the write
    was skipped, or because it never happened — yields ``NONE``.
    """
    if db is None or not tenant_id or not customer_id:
        return no_evidence(fallback_reason or "missing_scope")

    try:
        from core.customer_address_candidates import (  # noqa: PLC0415
            resolve_customer_address_selection,
        )

        resolution = resolve_customer_address_selection(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[CUSTOMER_ADDRESS_EVIDENCE] resolution failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return no_evidence(fallback_reason or "resolution_failed")

    target_id = int(address_id) if address_id else None
    selected = resolution.selected
    if selected is not None and (target_id is None or selected.address_id == target_id):
        return CustomerAddressPersistenceEvidence(
            scope=AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS,
            reason="explicit_selection_committed",
            address_id=selected.address_id,
            fingerprint=selected.fingerprint,
            components=selected.components.as_dict(),
        )

    pool = list(resolution.candidates)
    if selected is not None:
        pool.append(selected)
    for candidate in pool:
        if target_id is not None and candidate.address_id != target_id:
            continue
        return CustomerAddressPersistenceEvidence(
            scope=AddressPersistenceScope.IMPORTED_CANDIDATE,
            reason="candidate_committed",
            address_id=candidate.address_id,
            fingerprint=candidate.fingerprint,
            components=candidate.components.as_dict(),
        )

    return no_evidence(fallback_reason or "no_committed_customer_address")


__all__ = [
    "AddressPersistenceScope",
    "CustomerAddressPersistenceEvidence",
    "no_evidence",
    "resolve_customer_address_persistence_evidence",
    "state_only_evidence",
]
