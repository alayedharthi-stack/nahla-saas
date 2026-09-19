"""
core/customer_address_persistence_evidence.py
─────────────────────────────────────────────
Structured evidence for what an address operation actually persisted.

Three things have to be true before a reply may claim an address was saved
or adopted, and each one is a separate question:

1. **An operation happened.** Some path in THIS turn attempted a specific
   save or adoption, naming the address and the revision it acted on. The
   mere existence of an older address proves nothing about a new claim —
   a customer told "your new address in Jeddah is saved" is not answered
   by a row from Riyadh that was already there.
2. **It targeted what the claim is about.** The evidence is bound to
   tenant, customer, address row and content revision. An unknown target
   authorizes nothing.
3. **It committed.** Durability is read back on a connection that cannot
   see the writer's uncommitted work, so a flushed-but-rolled-back write,
   an aborted transaction and a skipped writer all read as absent.

Scopes, narrowest first:

* ``CONVERSATION_STATE`` — the turn's order/conversation state holds the
  address. Nothing durable about the customer changed.
* ``IMPORTED_CANDIDATE`` — a durable, source-labelled candidate exists. It
  has NOT been adopted as the delivery address.
* ``SELECTED_DELIVERY_ADDRESS`` — the customer explicitly selected this
  exact address revision as their delivery address.
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


class AddressOperation(str, Enum):
    """What the turn attempted. ``NONE`` means: nothing did."""

    NONE = "none"
    SAVE_CANDIDATE = "save_candidate"
    ADOPT_SELECTION = "adopt_selection"


_RANK = {
    AddressPersistenceScope.NONE: 0,
    AddressPersistenceScope.CONVERSATION_STATE: 1,
    AddressPersistenceScope.IMPORTED_CANDIDATE: 2,
    AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS: 3,
}


@dataclass(frozen=True)
class AddressOperationAttempt:
    """The specific save/adoption a turn performed, and on what."""

    operation: AddressOperation = AddressOperation.NONE
    tenant_id: Optional[int] = None
    customer_id: Optional[int] = None
    address_id: Optional[int] = None
    fingerprint: str = ""

    @property
    def is_actionable(self) -> bool:
        return (
            self.operation is not AddressOperation.NONE
            and bool(self.tenant_id)
            and bool(self.customer_id)
            and bool(self.address_id)
        )


NO_OPERATION = AddressOperationAttempt()


@dataclass(frozen=True)
class CustomerAddressPersistenceEvidence:
    scope: AddressPersistenceScope = AddressPersistenceScope.NONE
    reason: str = ""
    operation: AddressOperation = AddressOperation.NONE
    address_id: Optional[int] = None
    fingerprint: str = ""
    components: Dict[str, Any] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        """True only for a scope proven by an independently read row."""
        return _RANK[self.scope] >= _RANK[AddressPersistenceScope.IMPORTED_CANDIDATE]

    def allows_saved_address_claim(self) -> bool:
        """May the reply say THIS address was saved to the customer record?"""
        return self.committed and self.operation is not AddressOperation.NONE

    def allows_adopted_address_claim(self) -> bool:
        """May the reply say it was adopted as THE delivery address?"""
        return (
            self.scope is AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS
            and self.operation is AddressOperation.ADOPT_SELECTION
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope.value,
            "operation": self.operation.value,
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


def _independent_session(db: Any) -> Any:
    """A session on the same engine that cannot see ``db``'s open work.

    Reading durability through the writer's own session proves nothing: a
    flushed row is visible there before — and even without — a commit.
    A separate connection sees only committed data, which is the fact the
    claim depends on.
    """
    from sqlalchemy.orm import Session  # noqa: PLC0415

    bind = db.get_bind()
    if bind is None:
        return None
    engine = getattr(bind, "engine", bind)
    return Session(bind=engine)


def resolve_customer_address_persistence_evidence(
    db: Any,
    *,
    tenant_id: Optional[int],
    customer_id: Optional[int],
    attempt: AddressOperationAttempt = NO_OPERATION,
    fallback_reason: str = "",
) -> CustomerAddressPersistenceEvidence:
    """Prove that THIS operation's target is durably there, or return NONE."""
    if db is None or not tenant_id or not customer_id:
        return no_evidence(fallback_reason or "missing_scope")

    if not attempt.is_actionable:
        # No address operation in this turn: nothing to have succeeded.
        return no_evidence(fallback_reason or "no_address_operation_in_turn")

    if int(attempt.tenant_id or 0) != int(tenant_id) or int(
        attempt.customer_id or 0
    ) != int(customer_id):
        return no_evidence("operation_scope_mismatch")

    session = None
    try:
        session = _independent_session(db)
        if session is None:
            return no_evidence("durability_unverifiable")
        from core.customer_address_candidates import (  # noqa: PLC0415
            resolve_customer_address_selection,
        )

        resolution = resolve_customer_address_selection(
            session,
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unverifiable read is reported as "no evidence", which blocks the claim rather than allowing it
        logger.debug(
            "[CUSTOMER_ADDRESS_EVIDENCE] independent read failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return no_evidence(fallback_reason or "durability_unverifiable")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — closing a read-only probe session cannot affect the result
                pass

    target_id = int(attempt.address_id or 0)
    selected = resolution.selected
    if selected is not None and selected.address_id == target_id:
        if attempt.fingerprint and attempt.fingerprint != selected.fingerprint:
            return no_evidence("committed_revision_mismatch")
        return CustomerAddressPersistenceEvidence(
            scope=AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS,
            reason="explicit_selection_committed",
            operation=attempt.operation,
            address_id=selected.address_id,
            fingerprint=selected.fingerprint,
            components=selected.components.as_dict(),
        )

    for candidate in resolution.selectable:
        if candidate.address_id != target_id:
            continue
        if attempt.fingerprint and attempt.fingerprint != candidate.fingerprint:
            return no_evidence("committed_revision_mismatch")
        if attempt.operation is AddressOperation.ADOPT_SELECTION:
            # The adoption did not take: the row is there, but it is not
            # the selected delivery address.
            return CustomerAddressPersistenceEvidence(
                scope=AddressPersistenceScope.IMPORTED_CANDIDATE,
                reason="adoption_not_committed",
                operation=AddressOperation.NONE,
                address_id=candidate.address_id,
                fingerprint=candidate.fingerprint,
                components=candidate.components.as_dict(),
            )
        return CustomerAddressPersistenceEvidence(
            scope=AddressPersistenceScope.IMPORTED_CANDIDATE,
            reason="candidate_committed",
            operation=attempt.operation,
            address_id=candidate.address_id,
            fingerprint=candidate.fingerprint,
            components=candidate.components.as_dict(),
        )

    return no_evidence(fallback_reason or "no_committed_customer_address")


__all__ = [
    "NO_OPERATION",
    "AddressOperation",
    "AddressOperationAttempt",
    "AddressPersistenceScope",
    "CustomerAddressPersistenceEvidence",
    "no_evidence",
    "resolve_customer_address_persistence_evidence",
    "state_only_evidence",
]
