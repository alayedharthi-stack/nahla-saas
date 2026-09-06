"""
Lifecycle 24h service-window policy — one deterministic interface.

OPEN iff last customer inbound is strictly less than 24 hours ago.
Canonical read: ``wa_usage.has_open_service_window``
(``WaConversationWindow.last_customer_inbound_at``).

Missing last-inbound, or a pre-0105 schema without
``last_customer_inbound_at``, is a normal CLOSED result
(``WINDOW_SOURCE_WA_USAGE``). Other query/read exceptions fail closed to
the template path with ``WINDOW_SOURCE_ERROR_FAIL_CLOSED``.
Do not add a second window implementation here.
"""
from __future__ import annotations

import logging
from typing import Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.commerce_lifecycle.window")

WINDOW_SOURCE_WA_USAGE = "wa_usage.has_open_service_window"
WINDOW_SOURCE_ERROR_FAIL_CLOSED = "window_check_error_fail_closed"


def lifecycle_service_window_is_open(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> Tuple[bool, str]:
    """
    Return ``(window_open, source)``.

    Fail closed: any exception → ``(False, WINDOW_SOURCE_ERROR_FAIL_CLOSED)``
    so dispatch uses the approved Meta template path.
    """
    from core.wa_usage import has_open_service_window  # noqa: PLC0415

    try:
        opened = bool(has_open_service_window(db, int(tenant_id), str(customer_phone or "")))
        return opened, WINDOW_SOURCE_WA_USAGE
    except Exception as exc:  # noqa: BLE001 — fail closed to template path
        logger.warning(
            "[LifecycleWindow] check_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False, WINDOW_SOURCE_ERROR_FAIL_CLOSED


__all__ = [
    "WINDOW_SOURCE_ERROR_FAIL_CLOSED",
    "WINDOW_SOURCE_WA_USAGE",
    "lifecycle_service_window_is_open",
]
