"""
core/salla_onboarding_email.py
──────────────────────────────
Fire-and-forget onboarding email for Salla auto-provisioning.

Uses a single-use /set-password link (PasswordSetupToken) for new users —
never emails a plaintext temporary password. Existing merchants receive a
connection-success email without password instructions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.salla_onboarding_email")

_DERIVED_SUFFIXES = ("@salla-merchant.nahlah.ai", "@zid-merchant.nahlah.ai")
_CONFIG_FLAG = "onboarding_email_sent_at"


def is_deliverable_merchant_email(email: str) -> bool:
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr:
        return False
    return not any(addr.endswith(suffix) for suffix in _DERIVED_SUFFIXES)


def onboarding_email_already_sent(cfg: dict) -> bool:
    return bool((cfg or {}).get(_CONFIG_FLAG))


def queue_salla_onboarding_email(
    *,
    email: str,
    store_name: str,
    dashboard_url: str,
    set_password_url: Optional[str],
    integration_id: Optional[int],
    tenant_id: int,
    user_id: int,
) -> None:
    """Schedule onboarding email; never raises and never blocks OAuth."""
    if not is_deliverable_merchant_email(email):
        logger.info(
            "[SallaOnboardingEmail] skipped — no deliverable inbox | tenant=%s user=%s email=%r",
            tenant_id, user_id, email,
        )
        return

    asyncio.ensure_future(
        _send_and_mark(
            email=email,
            store_name=store_name or "متجرك",
            dashboard_url=dashboard_url,
            set_password_url=set_password_url,
            integration_id=integration_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    )


async def _send_and_mark(
    *,
    email: str,
    store_name: str,
    dashboard_url: str,
    set_password_url: Optional[str],
    integration_id: Optional[int],
    tenant_id: int,
    user_id: int,
) -> None:
    from core.audit import audit  # noqa: PLC0415
    from core.database import SessionLocal  # noqa: PLC0415
    from core.notifications import (  # noqa: PLC0415
        email_salla_store_connected,
        email_set_password,
        send_email,
    )
    from models import Integration  # noqa: PLC0415

    db: Session = SessionLocal()
    try:
        if integration_id is not None:
            integration = db.query(Integration).filter(Integration.id == integration_id).first()
            if integration is not None and onboarding_email_already_sent(integration.config or {}):
                logger.info(
                    "[SallaOnboardingEmail] skipped — already sent | tenant=%s integration=%s",
                    tenant_id, integration_id,
                )
                return

        is_new_user = bool(set_password_url)
        if is_new_user:
            subject = "تم ربط متجرك بنجاح مع نحلة الذكية"
            html = email_set_password(
                store_name=store_name,
                email=email,
                set_password_url=set_password_url,
                dashboard_url=dashboard_url,
                source_label="سلة",
            )
        else:
            subject = "تم ربط متجرك بنجاح مع نحلة الذكية"
            html = email_salla_store_connected(
                store_name=store_name,
                email=email,
                dashboard_url=dashboard_url,
            )

        ok = await send_email(to=email, subject=subject, html=html)
        if not ok:
            audit(
                "salla_onboarding_email_failed",
                sub=email,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="provider_returned_failure",
            )
            logger.warning(
                "[SallaOnboardingEmail] NOT sent (provider failure) tenant=%s user=%s",
                tenant_id, user_id,
            )
            return

        if integration_id is not None:
            integration = db.query(Integration).filter(Integration.id == integration_id).first()
            if integration is not None:
                cfg = dict(integration.config or {})
                cfg[_CONFIG_FLAG] = datetime.now(timezone.utc).isoformat()
                integration.config = cfg
                flag_modified(integration, "config")
                db.commit()

        audit(
            "salla_onboarding_email_sent",
            sub=email,
            tenant_id=tenant_id,
            user_id=user_id,
            is_new_user=is_new_user,
        )
        logger.info(
            "[SallaOnboardingEmail] sent | tenant=%s user=%s new_user=%s",
            tenant_id, user_id, is_new_user,
        )
    except Exception as exc:  # noqa: BLE001
        audit(
            "salla_onboarding_email_failed",
            sub=email,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="exception",
        )
        logger.exception(
            "[SallaOnboardingEmail] crashed tenant=%s user=%s: %s",
            tenant_id, user_id, exc,
        )
    finally:
        db.close()
