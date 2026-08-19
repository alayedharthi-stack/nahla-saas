"""First tenant-scoped customer-contact email for merchants.

Canonical SoT is the Customer row (tenant_id + normalized_phone), not a
conversation. The one-time claim is stored on ``extra_metadata`` so the
customers table schema used by frozen A1 postgres suites stays unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.merchant_first_contact")

WHATSAPP_ACQUISITION_CHANNELS = frozenset({
    "whatsapp_inbound",
    "whatsapp_lead",
})

EVENT_FIRST_CUSTOMER_CONTACT = "first_customer_contact"
STAMP_KEY = "first_contact_notified_at"
# Existing/historical customers must not look like "new now" after deploy.
EXISTING_CUSTOMER_GRACE = timedelta(hours=1)


def stamp_value(customer) -> Optional[str]:
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    raw = meta.get(STAMP_KEY)
    return str(raw) if raw else None


def _tz(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _sync_instance_stamp(customer, iso: str) -> None:
    try:
        meta = dict(getattr(customer, "extra_metadata", None) or {})
        meta[STAMP_KEY] = iso
        customer.extra_metadata = meta
    except Exception:
        return


def _claim_stamp(db, *, tenant_id: int, customer, now: datetime) -> bool:
    """Return True iff this caller uniquely wrote the first-contact stamp."""
    iso = now.isoformat()
    cid = int(customer.id)
    tid = int(tenant_id)
    bind = db.get_bind()
    dialect = getattr(getattr(bind, "dialect", None), "name", "") or ""

    if dialect == "postgresql":
        result = db.execute(
            text(
                "UPDATE customers "
                "SET metadata = COALESCE(metadata, '{}'::jsonb) "
                " || CAST(:patch AS jsonb) "
                "WHERE id = :id AND tenant_id = :tid "
                "AND COALESCE(metadata->>'first_contact_notified_at', '') = '' "
                "RETURNING id"
            ),
            {
                "patch": json.dumps({STAMP_KEY: iso}),
                "id": cid,
                "tid": tid,
            },
        )
        claimed = result.first() is not None
        db.commit()
        if claimed:
            _sync_instance_stamp(customer, iso)
        return claimed

    # SQLite / generic: persist by primary key so a stale in-memory copy
    # cannot double-claim. Sequential tests cover this path; production
    # webhook traffic uses the PostgreSQL atomic UPDATE above.
    from models import Customer  # noqa: PLC0415

    row = (
        db.query(Customer)
        .filter(Customer.id == cid, Customer.tenant_id == tid)
        .first()
    )
    if row is None:
        return False
    meta = dict(row.extra_metadata or {})
    if meta.get(STAMP_KEY):
        return False
    meta[STAMP_KEY] = iso
    row.extra_metadata = meta
    flag_modified(row, "extra_metadata")
    db.commit()
    _sync_instance_stamp(customer, iso)
    _sync_instance_stamp(row, iso)
    return True


def suppress_first_contact(db, *, tenant_id: int, customer) -> dict:
    """Stamp without emailing — history import / already-known relationship."""
    if customer is None or getattr(customer, "id", None) is None:
        return {"send": False, "reason": "no_customer", "reason_ar": "لا يوجد سجل عميل"}
    if stamp_value(customer):
        return {
            "send": False,
            "reason": "already_notified",
            "reason_ar": "تم إشعار التاجر عند أول تواصل",
        }
    now = datetime.now(timezone.utc)
    _claim_stamp(db, tenant_id=tenant_id, customer=customer, now=now)
    return {
        "send": False,
        "reason": "suppressed_history",
        "reason_ar": "تواصل تاريخي — لا إشعار عميل جديد الآن",
    }


def try_claim_first_contact(
    *,
    db,
    tenant_id: int,
    customer,
) -> dict:
    """Atomically claim the first-contact email for this tenant customer."""
    if customer is None or getattr(customer, "id", None) is None:
        return {
            "send": False,
            "reason": "no_customer",
            "reason_ar": "لا يوجد سجل عميل",
        }

    channel = str(getattr(customer, "acquisition_channel", None) or "").strip()
    if channel not in WHATSAPP_ACQUISITION_CHANNELS:
        _claim_stamp(
            db,
            tenant_id=tenant_id,
            customer=customer,
            now=datetime.now(timezone.utc),
        )
        return {
            "send": False,
            "reason": "existing_relationship",
            "reason_ar": "العميل لديه علاقة سابقة مع المتجر",
        }

    if stamp_value(customer):
        return {
            "send": False,
            "reason": "already_notified",
            "reason_ar": "تم إشعار التاجر عند أول تواصل",
        }

    now = datetime.now(timezone.utc)
    first_seen = _tz(getattr(customer, "first_seen_at", None))
    if first_seen and (now - first_seen) > EXISTING_CUSTOMER_GRACE:
        _claim_stamp(db, tenant_id=tenant_id, customer=customer, now=now)
        return {
            "send": False,
            "reason": "existing_relationship",
            "reason_ar": "العميل لديه علاقة سابقة مع المتجر",
        }

    if not _claim_stamp(db, tenant_id=tenant_id, customer=customer, now=now):
        return {
            "send": False,
            "reason": "already_notified",
            "reason_ar": "تم إشعار التاجر عند أول تواصل",
        }
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
            return {
                "send": False,
                "reason": "no_merchant_email",
                "reason_ar": "لا يوجد بريد تاجر",
            }

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
