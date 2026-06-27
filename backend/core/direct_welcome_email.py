"""
core/direct_welcome_email.py
────────────────────────────
Direct merchant welcome email after email verification.

Dedupe is stored on ``TenantSettings.notification_settings`` so a failed
send can be retried on a later verify-link click without re-verifying email.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.direct_welcome_email")

_WELCOME_FLAG = "direct_welcome_email_sent_at"


def welcome_email_already_sent(notification_settings: Optional[dict]) -> bool:
    return bool((notification_settings or {}).get(_WELCOME_FLAG))


def _get_or_create_tenant_settings(db: Session, tenant_id: int):
    from models import TenantSettings  # noqa: PLC0415

    ts = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if ts:
        return ts
    ts = TenantSettings(
        tenant_id=tenant_id,
        show_nahla_branding=True,
        branding_text="🐝 Powered by Nahla",
    )
    db.add(ts)
    db.flush()
    return ts


def get_notification_settings(db: Session, tenant_id: int) -> dict:
    from models import TenantSettings  # noqa: PLC0415

    ts = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not ts:
        return {}
    return dict(ts.notification_settings or {})


def mark_welcome_email_sent(db: Session, tenant_id: int) -> None:
    ts = _get_or_create_tenant_settings(db, tenant_id)
    ns = dict(ts.notification_settings or {})
    ns[_WELCOME_FLAG] = datetime.now(timezone.utc).isoformat()
    ts.notification_settings = ns
    flag_modified(ts, "notification_settings")
    db.commit()


def queue_direct_welcome_email(
    *,
    email: str,
    store_name: str,
    dashboard_url: str,
    tenant_id: int,
) -> None:
    asyncio.ensure_future(
        _send_and_mark(
            email=email,
            store_name=store_name,
            dashboard_url=dashboard_url,
            tenant_id=tenant_id,
        )
    )


async def _send_and_mark(
    *,
    email: str,
    store_name: str,
    dashboard_url: str,
    tenant_id: int,
) -> None:
    from core.audit import audit  # noqa: PLC0415
    from core.database import SessionLocal  # noqa: PLC0415
    from core.notifications import email_welcome, send_email  # noqa: PLC0415

    ok = await send_email(
        to=email,
        subject="مرحباً بك في نحلة الذكية 🎉",
        html=email_welcome(store_name, dashboard_url, email),
    )
    if not ok:
        logger.warning(
            "[DirectWelcomeEmail] send failed — will retry on next verify click | tenant=%s email=%s",
            tenant_id,
            email,
        )
        audit("direct_welcome_email_failed", sub=email, tenant_id=tenant_id)
        return

    db = SessionLocal()
    try:
        mark_welcome_email_sent(db, tenant_id)
        audit("direct_welcome_email_sent", sub=email, tenant_id=tenant_id)
        logger.info("[DirectWelcomeEmail] sent and marked | tenant=%s email=%s", tenant_id, email)
    except Exception:
        db.rollback()
        logger.exception(
            "[DirectWelcomeEmail] sent but failed to mark dedupe | tenant=%s email=%s",
            tenant_id,
            email,
        )
    finally:
        db.close()
