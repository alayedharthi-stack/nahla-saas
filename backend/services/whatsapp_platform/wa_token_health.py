"""
Periodic WhatsApp Meta token health checks — no silent disconnects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("nahla.wa_token_health")


async def run_whatsapp_token_health_checks() -> dict:
    from core.database import SessionLocal  # noqa: PLC0415
    from database.models import WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_META, wa_provider  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import maybe_reencrypt_plaintext  # noqa: PLC0415
    from services.whatsapp_platform.wa_token_validation import validate_connection_health  # noqa: PLC0415

    db = SessionLocal()
    checked = 0
    warn_14 = 0
    expired = 0
    reencrypted = 0
    try:
        rows = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.status.in_(["connected", "needs_reauth", "pending"]),
                WhatsAppConnection.access_token.isnot(None),
                WhatsAppConnection.access_token != "",
            )
            .all()
        )
        for conn in rows:
            if wa_provider(conn) != WHATSAPP_PROVIDER_META:
                continue
            try:
                if maybe_reencrypt_plaintext(conn, tenant_id=conn.tenant_id):
                    reencrypted += 1
                result = await validate_connection_health(conn)
                checked += 1
                meta = dict(conn.extra_metadata or {})
                health = meta.get("health_status") or result.health_status
                if health == "token_expiring_soon":
                    warn_14 += 1
                    logger.warning(
                        "[wa_token_health] tenant=%s token_expiring_soon status=%s warnings=%s",
                        conn.tenant_id, result.token_status, result.warnings,
                    )
                elif health in {"token_expired", "meta_error", "permission_revoked"}:
                    expired += 1
                    logger.warning(
                        "[wa_token_health] tenant=%s health=%s error=%s — sending_enabled=%s (not disconnected)",
                        conn.tenant_id,
                        health,
                        meta.get("last_meta_validation_error"),
                        conn.sending_enabled,
                    )
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("[wa_token_health] tenant=%s check failed: %s", conn.tenant_id, exc)
    finally:
        db.close()

    summary = {
        "checked": checked,
        "reencrypted": reencrypted,
        "expiring_soon": warn_14,
        "degraded": expired,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("[wa_token_health] summary=%s", summary)
    return summary
