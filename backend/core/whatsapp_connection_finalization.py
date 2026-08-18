"""Canonical platform lifecycle once WhatsApp is genuinely connected.

Provider / onboarding paths decide WHEN a connection is ready
(webhook, SMB, token, /register). This module owns WHAT happens next:

  - status = connected
  - connected_at stamped once (never moved)
  - whatsapp_ai_live_since stamped once if empty
  - start_trial_on_whatsapp_connect() via the existing trial owner

Do not put provider eligibility, webhook subscription, or SMB sync here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.billing import _coerce_utc
from core.trial_lifecycle import start_trial_on_whatsapp_connect

logger = logging.getLogger("nahla.whatsapp_connection_finalization")


def finalize_successful_whatsapp_connection(
    db: Session,
    conn: Any,
    *,
    connected_at: Optional[datetime] = None,
) -> bool:
    """Apply platform post-connect lifecycle for a ready WhatsApp row.

    Returns True when this call started the free trial, False on
    idempotent skip or non-fatal trial failure.
    """
    tenant_id = int(getattr(conn, "tenant_id", 0) or 0)
    now = _coerce_utc(connected_at) or datetime.now(timezone.utc)
    naive_now = now.replace(tzinfo=None) if now.tzinfo else now

    conn.status = "connected"
    if getattr(conn, "connected_at", None) is None:
        conn.connected_at = now

    try:
        from core.whatsapp_ai_live import stamp_whatsapp_ai_live_since_if_empty  # noqa: PLC0415
        stamp_whatsapp_ai_live_since_if_empty(conn)
    except Exception:
        logger.debug(
            "[WAFinalize] ai-live stamp skipped tenant=%s", tenant_id, exc_info=True,
        )

    logger.info(
        "[WAFinalize] successful connection tenant=%s conn_id=%s connected_at=%s",
        tenant_id,
        getattr(conn, "id", None),
        getattr(conn, "connected_at", None),
    )

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WAFinalize] connection commit failed tenant=%s: %s", tenant_id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False

    trial_at = getattr(conn, "connected_at", None) or naive_now
    try:
        return bool(
            start_trial_on_whatsapp_connect(db, tenant_id, connected_at=trial_at)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WAFinalize] trial start hook failed (non-fatal) tenant=%s: %s",
            tenant_id,
            exc,
        )
        return False
