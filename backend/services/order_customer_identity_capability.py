"""A1 order-customer identity platform capability state (expand vs validated)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.order_customer_identity_logging import log_capability_state_read_failure
from services.order_customer_identity_contract import (
    CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY,
    CAPABILITY_STATE_VALIDATED,
    CAPABILITY_STATES,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
)

_CAPABILITY_STATE_TABLE = "order_customer_identity_capability_state"


def read_order_customer_identity_capability_state(db: Session) -> Optional[str]:
    """Return expand|validated, or None when missing/unreadable/unknown (fail-closed)."""
    try:
        row = db.execute(
            text(
                f"""
                SELECT state
                FROM {_CAPABILITY_STATE_TABLE}
                WHERE capability_key = :capability_key
                """
            ),
            {"capability_key": CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY},
        ).first()
        if row is None:
            return None
        state = str(row[0]).strip()
        if state not in CAPABILITY_STATES:
            return None
        return state
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — capability read intentionally fails closed; traceback omitted to protect operational identifiers
        log_capability_state_read_failure(exception_class=type(exc).__name__)
        return None


def order_customer_identity_reconciliation_ready(db: Session) -> bool:
    return (
        read_order_customer_identity_capability_state(db) == CAPABILITY_STATE_VALIDATED
    )


def cap_coverage_status_for_capability(
    db: Session,
    *,
    completeness: str,
    forward_health: str,
) -> tuple[str, str]:
    """Fail-closed: no healthy/complete until capability state is validated."""
    if order_customer_identity_reconciliation_ready(db):
        return completeness, forward_health
    if completeness == SOURCE_HISTORY_COMPLETE:
        completeness = SOURCE_HISTORY_INCOMPLETE
    if forward_health == SYNC_HEALTH_HEALTHY:
        forward_health = SYNC_HEALTH_DEGRADED
    return completeness, forward_health


__all__ = [
    "cap_coverage_status_for_capability",
    "order_customer_identity_reconciliation_ready",
    "read_order_customer_identity_capability_state",
]
