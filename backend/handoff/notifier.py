"""
HandoffNotifier
───────────────
Dispatches notifications when a human handoff is triggered.

Supported delivery methods (configured per tenant):
  webhook   — HTTP POST to a staff endpoint
  whatsapp  — send message to a staff WhatsApp number (via existing WhatsApp integration)
  both      — both methods

Notification payload:
{
  "event":          "handoff_requested",
  "session_id":     123,
  "tenant_id":      1,
  "customer_phone": "966...",
  "customer_name":  "...",
  "last_message":   "...",
  "dashboard_url":  "https://app.nahla.ai/handoff-queue",
  "timestamp":      "2026-01-01T00:00:00Z"
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("nahla.handoff.notifier")

REQUEST_TIMEOUT = 10.0


async def notify_handoff(
    session_id: int,
    tenant_id: int,
    customer_phone: str,
    customer_name: str,
    last_message: str,
    handoff_settings: Dict[str, Any],
) -> bool:
    """
    Send handoff notification according to tenant configuration.
    Returns True if at least one notification was dispatched.
    """
    method = handoff_settings.get("notification_method", "none")
    if method == "none" or not method:
        logger.debug(f"[HandoffNotifier] notification_method=none, skipping")
        return False

    payload = {
        "event": "handoff_requested",
        "session_id": session_id,
        "tenant_id": tenant_id,
        "customer_phone": customer_phone,
        "customer_name": customer_name,
        "last_message": last_message[:300],
        "dashboard_url": "https://app.nahla.ai/handoff-queue",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    success = False

    if method in ("webhook", "both"):
        webhook_url = handoff_settings.get("webhook_url", "").strip()
        if webhook_url:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    resp = await client.post(
                        webhook_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    resp.raise_for_status()
                    logger.info(
                        f"[HandoffNotifier] Webhook delivered to {webhook_url} "
                        f"for session #{session_id}"
                    )
                    success = True
            except Exception as exc:
                logger.error(
                    f"[HandoffNotifier] Webhook delivery failed for session #{session_id}: {exc}"
                )
        else:
            logger.warning("[HandoffNotifier] webhook method configured but webhook_url is empty")

    # WhatsApp delivery would invoke the existing WhatsApp API here.
    # Placeholder: log for now, implement when WhatsApp integration is wired.
    if method in ("whatsapp", "both"):
        staff_number = handoff_settings.get("staff_whatsapp", "").strip()
        if staff_number:
            logger.info(
                f"[HandoffNotifier] WhatsApp notify TODO: send to {staff_number} "
                f"for session #{session_id} — wire WhatsApp send API here"
            )
            # TODO: call WhatsApp send message API
        else:
            logger.warning("[HandoffNotifier] whatsapp method configured but staff_whatsapp is empty")

    return success
