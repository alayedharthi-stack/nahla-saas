"""
routers/notification_logs.py
─────────────────────────────
سجل الإشعارات البسيط — يُتيح للتاجر رؤية:
- متى أُرسل إيميل وما سببه
- متى لم يُرسل وما السبب
- ملخص سريع للإشعارات الأخيرة

Routes:
  GET /merchant/notification-logs          — آخر الإشعارات مع السبب
  GET /merchant/notification-logs/summary  — ملخص (كم sent / skipped)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla.notifications")
router = APIRouter()


def _fmt(dt) -> str | None:
    if dt is None:
        return None
    try:
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(dt)


EVENT_LABELS_AR = {
    "new_whatsapp_message": "رسالة واتساب جديدة",
    "returning_customer":   "عميل عاد بعد فترة",
    "new_order":            "طلب جديد",
    "support_request":      "طلب دعم",
}

STATUS_LABELS_AR = {
    "sent":    "تم الإرسال",
    "skipped": "لم يُرسَل",
}


@router.get("/merchant/notification-logs")
async def get_notification_logs(
    request: Request,
    db:      Session        = Depends(get_db),
    user:    Dict[str, Any] = Depends(get_current_user),
    limit:   int            = Query(default=50, ge=1, le=200),
    days:    int            = Query(default=7, ge=1, le=30),
):
    """
    آخر إشعارات الإيميل للتاجر مع السبب.

    يعرض:
    - event (نوع الحدث)
    - status (sent / skipped)
    - reason (سبب التخطي بالعربي)
    - phone / message preview
    - created_at
    """
    from database.models import NotificationLog  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(NotificationLog)
        .filter(
            NotificationLog.tenant_id >= tenant_id,
            NotificationLog.tenant_id <= tenant_id,
            NotificationLog.created_at >= cutoff,
        )
        .order_by(NotificationLog.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "logs": [
            {
                "id":          r.id,
                "event":       r.event,
                "event_ar":    EVENT_LABELS_AR.get(r.event, r.event),
                "type":        r.type,
                "status":      r.status,
                "status_ar":   STATUS_LABELS_AR.get(r.status, r.status),
                "reason_ar":   r.reason or "",
                "details":     r.details or {},
                "created_at":  _fmt(r.created_at),
            }
            for r in rows
        ],
        "count":     len(rows),
        "days":      days,
        "tenant_id": tenant_id,
    }


@router.get("/merchant/notification-logs/summary")
async def notification_summary(
    request: Request,
    db:      Session        = Depends(get_db),
    user:    Dict[str, Any] = Depends(get_current_user),
    days:    int            = Query(default=7, ge=1, le=30),
):
    """
    ملخص الإشعارات: كم تم الإرسال وكم تم التخطي وما الأسباب.
    مفيد لأيقونة الإشعارات الصغيرة في الـ Header.
    """
    from database.models import NotificationLog  # noqa: PLC0415
    from sqlalchemy import func                  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(NotificationLog)
        .filter(
            NotificationLog.tenant_id  == tenant_id,
            NotificationLog.created_at >= cutoff,
        )
        .all()
    )

    sent    = [r for r in rows if r.status == "sent"]
    skipped = [r for r in rows if r.status == "skipped"]

    # Count skip reasons
    skip_reasons: Dict[str, int] = {}
    for r in skipped:
        skip_reasons[r.reason or "غير محدد"] = skip_reasons.get(r.reason or "غير محدد", 0) + 1

    return {
        "days":          days,
        "total":         len(rows),
        "sent":          len(sent),
        "skipped":       len(skipped),
        "skip_reasons":  skip_reasons,
        "last_sent_at":  _fmt(sent[0].created_at) if sent else None,
    }
