"""Privacy-safe logging for A1 order-customer identity (no PII identifiers)."""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("nahla.order_customer_identity")


def log_identity_sync_event(
    *,
    event: str,
    tenant_id: int,
    order_source_kind: Optional[str] = None,
    external_identity_link_state: Optional[str] = None,
    customer_link_state: Optional[str] = None,
    link_outcome: Optional[str] = None,
    matched_via: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    logger.info(
        "[A1 identity] event=%s tenant_id=%s order_source_kind=%s "
        "external_link_state=%s customer_link_state=%s link_outcome=%s "
        "matched_via=%s reason=%s",
        event,
        tenant_id,
        order_source_kind or "-",
        external_identity_link_state or "-",
        customer_link_state or "-",
        link_outcome or "-",
        matched_via or "-",
        reason or "-",
    )


def log_identity_sync_failure(
    *,
    tenant_id: int,
    ingest_source: str,
    exception_class: str,
    link_outcome: Optional[str] = None,
) -> None:
    logger.warning(
        "[A1 identity] sync_failure tenant_id=%s ingest_source=%s "
        "exception_class=%s link_outcome=%s",
        tenant_id,
        ingest_source,
        exception_class,
        link_outcome or "-",
    )


def log_capability_state_read_failure(*, exception_class: str) -> None:
    """Privacy-safe capability gate read failure (no traceback / DB context)."""
    logger.warning(
        "[A1 identity] event=capability_state_read_failure exception_class=%s",
        exception_class,
    )


def log_reconciliation_report_failure(*, exception_class: str) -> None:
    """Privacy-safe operator-report failure; omit tenant and database context."""
    logger.warning(
        "[A1 identity] event=reconciliation_report_failure exception_class=%s",
        exception_class,
    )


def log_reconciliation_write_failure(*, exception_class: str) -> None:
    """Privacy-safe operator-write failure; omit tenant and database context."""
    logger.warning(
        "[A1 identity] event=reconciliation_write_failure exception_class=%s",
        exception_class,
    )


def log_connection_resolution_status(
    *,
    status: str,
    tenant_id: Optional[int] = None,
    reason: Optional[str] = None,
    ingest_source: str = "webhook_dispatcher",
) -> None:
    """Privacy-safe connection resolution log (no store_id / refs / PII)."""
    logger.info(
        "[A1 identity] connection_resolution_status=%s tenant_id=%s "
        "ingest_source=%s reason=%s",
        status,
        tenant_id if tenant_id is not None else "-",
        ingest_source,
        reason or "-",
    )


__all__ = [
    "log_capability_state_read_failure",
    "log_connection_resolution_status",
    "log_identity_sync_event",
    "log_identity_sync_failure",
    "log_reconciliation_report_failure",
    "log_reconciliation_write_failure",
]
