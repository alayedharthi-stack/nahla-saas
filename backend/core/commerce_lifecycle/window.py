"""
Lifecycle 24h service-window policy — one deterministic interface.

OPEN iff the current WaConversationWindow implementation reports an open
service window. Unknown/error fails closed to the closed-window path.

BLOCKED BY ROLLING-24H DEFECT:
``has_open_service_window`` is not last-customer-inbound truth. It uses the
billable window row (category sticky, window_start clock). Do not pretend
that is Meta's 24h customer-service window. Fix that in a dedicated PR.
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
