"""
Integration logger
──────────────────
Writes store integration events to the AutomationEvent table
so they appear in the AI Sales Logs UI.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.store_integration.logger")


def log_integration_event(
    db,
    tenant_id: int,
    event_type: str,           # "product_fetch" | "order_creation" | "payment_link" | "adapter_error"
    platform: str,
    success: bool,
    detail: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
):
    """
    Persist a store integration event as an AutomationEvent row
    (event_type='store_integration_log') so it is visible in dashboards.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        from database.models import AutomationEvent

        event = AutomationEvent(
            tenant_id=tenant_id,
            event_type="store_integration_log",
            customer_id=None,
            payload={
                "integration_event": event_type,
                "platform": platform,
                "success": success,
                "detail": detail or {},
                "error": error_message,
            },
            processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
    except Exception as exc:
        logger.warning(f"Could not write integration log event: {exc}")
