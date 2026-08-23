"""Canonical platform lifecycle once WhatsApp is genuinely connected.

Provider / onboarding paths decide WHEN a connection is ready
(webhook, SMB, token, /register). This module owns WHAT happens next:

  - status = connected
  - connected_at stamped once (never moved)
  - whatsapp_ai_live_since stamped once if empty
  - start_trial_on_whatsapp_connect() via the existing trial owner

Do not put provider eligibility, webhook subscription, or SMB sync here.

Return contract:
  True  — this call newly started the free trial
  False — successful persist of connected truth, but trial did not newly
          start (already active, paid tenant, or reconnect)

Persistence / required lifecycle failure RAISES
WhatsAppConnectionFinalizationError. False is never a failed persist.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.billing import _coerce_utc
from core.trial_lifecycle import start_trial_on_whatsapp_connect

logger = logging.getLogger("nahla.whatsapp_connection_finalization")


class WhatsAppConnectionFinalizationError(Exception):
    """Canonical successful-connection persist or required post-connect lifecycle failed."""


def finalize_successful_whatsapp_connection(
    db: Session,
    conn: Any,
    *,
    connected_at: Optional[datetime] = None,
) -> bool:
    """Apply platform post-connect lifecycle for a ready WhatsApp row.

    Connection transition and trial/first-connect timestamps are applied
    in one transaction so callers cannot observe connected + trial_pending.
    """
    tenant_id = int(getattr(conn, "tenant_id", 0) or 0)
    if tenant_id <= 0:
        raise WhatsAppConnectionFinalizationError("missing tenant_id for WhatsApp finalization")

    from models import Tenant  # noqa: PLC0415
    if db.query(Tenant).filter(Tenant.id == tenant_id).first() is None:
        raise WhatsAppConnectionFinalizationError(
            f"tenant={tenant_id} not found for WhatsApp finalization"
        )

    now = _coerce_utc(connected_at) or datetime.now(timezone.utc)
    naive_now = now.replace(tzinfo=None) if now.tzinfo else now

    conn.status = "connected"
    if getattr(conn, "connected_at", None) is None:
        conn.connected_at = now

    try:
        from core.whatsapp_ai_live import stamp_whatsapp_ai_live_since_if_empty  # noqa: PLC0415
        stamp_whatsapp_ai_live_since_if_empty(conn)
    except Exception:  # noqa: silent-ok — AI-live stamp is best-effort after connect
        logger.warning(
            "[WAFinalize] ai-live stamp skipped tenant=%s", tenant_id, exc_info=True,
        )

    trial_at = getattr(conn, "connected_at", None) or naive_now
    try:
        trial_started = bool(
            start_trial_on_whatsapp_connect(
                db, tenant_id, connected_at=trial_at, commit=False,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WAFinalize] trial lifecycle apply failed tenant=%s: %s", tenant_id, exc,
        )
        _rollback_finalization(db, conn)
        raise WhatsAppConnectionFinalizationError(
            f"trial lifecycle failed for tenant={tenant_id}"
        ) from exc

    logger.info(
        "[WAFinalize] successful connection tenant=%s conn_id=%s connected_at=%s trial_started=%s",
        tenant_id,
        getattr(conn, "id", None),
        getattr(conn, "connected_at", None),
        trial_started,
    )

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WAFinalize] connection commit failed tenant=%s: %s", tenant_id, exc,
        )
        _rollback_finalization(db, conn)
        raise WhatsAppConnectionFinalizationError(
            f"failed to persist successful WhatsApp connection for tenant={tenant_id}"
        ) from exc

    try:
        from core.catalog_review_harness import (  # noqa: PLC0415
            is_catalog_review_harness_enabled,
        )
        if is_catalog_review_harness_enabled():
            from services.meta_catalog_review_harness import (  # noqa: PLC0415
                schedule_catalog_review_harness_best_effort,
            )
            schedule_catalog_review_harness_best_effort(tenant_id)
        else:
            from services.meta_catalog_reconnect import (  # noqa: PLC0415
                schedule_meta_catalog_reconnect_best_effort,
            )
            schedule_meta_catalog_reconnect_best_effort(tenant_id)
    except Exception:  # noqa: silent-ok — catalog bind must not fail WhatsApp connect
        logger.warning(
            "[WAFinalize] catalog reconnect schedule skipped tenant=%s",
            tenant_id,
            exc_info=True,
        )

    return trial_started


def _rollback_finalization(db: Session, conn: Any) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: silent-ok — rollback after failed persist must not hide the original error
        pass
    try:
        db.expire_all()
    except Exception:  # noqa: silent-ok — expire is best-effort after rollback
        try:
            db.expire(conn)
        except Exception:  # noqa: silent-ok — expire is best-effort after rollback
            pass
