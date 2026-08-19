"""First tenant-scoped customer-contact email for merchants.

Canonical SoT is the Customer row (tenant_id + normalized_phone), not a
conversation. A successful claim sets ``first_contact_notified_at`` and is
the only permission to enqueue the merchant email.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import update

logger = logging.getLogger("nahla.merchant_first_contact")

WHATSAPP_ACQUISITION_CHANNELS = frozenset({
    "whatsapp_inbound",
    "whatsapp_lead",
})

EVENT_FIRST_CUSTOMER_CONTACT = "first_customer_contact"


def try_claim_first_contact(
    *,
    db,
    tenant_id: int,
    customer,
) -> dict:
    """Atomically claim the first-contact email for this tenant customer.

    Returns ``{"send": bool, "reason": str, "reason_ar": str}``.
    ``send=True`` means this caller uniquely won the claim and must enqueue.
    """
    from models import Customer  # noqa: PLC0415

    if customer is None or getattr(customer, "id", None) is None:
        return {
            "send": False,
            "reason": "no_customer",
            "reason_ar": "لا يوجد سجل عميل",
        }

    channel = str(getattr(customer, "acquisition_channel", None) or "").strip()
    if channel not in WHATSAPP_ACQUISITION_CHANNELS:
        return {
            "send": False,
            "reason": "existing_relationship",
            "reason_ar": "العميل لديه علاقة سابقة مع المتجر",
        }

    if getattr(customer, "first_contact_notified_at", None) is not None:
        return {
            "send": False,
            "reason": "already_notified",
            "reason_ar": "تم إشعار التاجر عند أول تواصل",
        }

    now = datetime.now(timezone.utc)
    result = db.execute(
        update(Customer)
        .where(
            Customer.id == int(customer.id),
            Customer.tenant_id == int(tenant_id),
            Customer.first_contact_notified_at.is_(None),
        )
        .values(first_contact_notified_at=now)
        .returning(Customer.id)
    )
    claimed = result.first()
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "[FirstContact] claim commit failed tenant=%s customer=%s",
            tenant_id, getattr(customer, "id", None),
        )
        return {
            "send": False,
            "reason": "claim_failed",
            "reason_ar": "تعذر تثبيت إشعار أول تواصل",
        }

    if not claimed:
        return {
            "send": False,
            "reason": "already_notified",
            "reason_ar": "تم إشعار التاجر عند أول تواصل",
        }

    try:
        db.refresh(customer)
    except Exception:
        customer.first_contact_notified_at = now
    return {
        "send": True,
        "reason": EVENT_FIRST_CUSTOMER_CONTACT,
        "reason_ar": "أول تواصل من هذا العميل مع المتجر",
    }


def maybe_notify_first_customer(
    *,
    db,
    tenant_id: int,
    customer,
    customer_phone: str = "",
    customer_name: str = "",
    message_preview: str = "",
    conversation_url: Optional[str] = None,
    log_notification=None,
) -> dict:
    """Claim + enqueue the merchant first-contact email. Never raises."""
    result = try_claim_first_contact(
        db=db, tenant_id=tenant_id, customer=customer,
    )
    customer_id = getattr(customer, "id", None) if customer is not None else None

    def _log(*, status: str, reason: str = "", extra: Optional[dict] = None) -> None:
        if log_notification is None:
            return
        try:
            log_notification(
                db=db,
                tenant_id=tenant_id,
                customer_id=customer_id,
                event=EVENT_FIRST_CUSTOMER_CONTACT,
                status=status,
                reason=reason,
                details=extra or {"phone": customer_phone},
            )
        except Exception as exc:
            logger.warning("[FirstContact] notification log failed: %s", exc)

    if not result["send"]:
        _log(status="skipped", reason=result.get("reason_ar") or result["reason"])
        return result

    try:
        from models import User  # noqa: PLC0415
        from services.email_service import enqueue_email  # noqa: PLC0415
        from core.config import DASHBOARD_URL  # noqa: PLC0415

        merchant = db.query(User).filter(
            User.tenant_id == tenant_id, User.role == "merchant",
        ).first()
        if not merchant or not merchant.email:
            _log(status="skipped", reason="لا يوجد بريد تاجر")
            result = {
                "send": False,
                "reason": "no_merchant_email",
                "reason_ar": "لا يوجد بريد تاجر",
            }
            return result

        enqueue_email(
            to=merchant.email,
            subject="عميل جديد بدأ محادثة عبر واتساب 🎉",
            template="first_whatsapp_message",
            sender_type="growth",
            variables={
                "merchant_name": merchant.username or "",
                "customer_name": customer_name or "",
                "customer_phone": customer_phone,
                "message_preview": message_preview,
                "conversation_url": conversation_url or f"{DASHBOARD_URL}/conversations",
            },
        )
        _log(
            status="sent",
            extra={
                "phone": customer_phone,
                "preview": (message_preview or "")[:80],
            },
        )
        return result
    except Exception as exc:
        logger.warning("[FirstContact] enqueue failed tenant=%s: %s", tenant_id, exc)
        _log(status="skipped", reason="تعذر إرسال البريد")
        return {
            "send": False,
            "reason": "enqueue_failed",
            "reason_ar": "تعذر إرسال البريد",
        }
