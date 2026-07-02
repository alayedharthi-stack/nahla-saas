"""
coupon_salla_push.py
────────────────────
Nahla → Salla coupon push helpers and Full API readiness checks.

Coupon sync (import + push) requires the canonical Salla integration with
``api_sync_enabled``, a refresh_token, and ``integration.enabled``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from models import Coupon
from services.coupon_sync_visibility import extract_salla_coupon_id

logger = logging.getLogger("nahla-backend")

FULL_API_INCOMPLETE_MSG_AR = "ربط API الكامل من تطبيق سلة غير مكتمل — أكمله من تطبيق سلة لتفعيل مزامنة الكوبونات"
NO_SALLA_ADAPTER_MSG_AR = "لا يوجد متجر سلة متصل أو لا يدعم مزامنة الكوبونات"
NOT_MANUAL_COUPON_MSG_AR = "يمكن إرسال الكوبونات اليدوية فقط إلى سلة"


def format_salla_datetime(dt: datetime) -> str:
    """Format for Salla coupon API (date-only or datetime per docs)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_salla_coupon_date(dt: datetime) -> str:
    """Date-only field for Salla coupon create (review-safe)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def normalize_salla_coupon_push_dates(
    start_dt: datetime,
    expiry_dt: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> tuple[str, Optional[str]]:
    """Build Salla coupon push dates: YYYY-MM-DD only; start not before today."""
    now = now or datetime.now(timezone.utc)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    else:
        start_dt = start_dt.astimezone(timezone.utc)

    today = now.astimezone(timezone.utc).date()
    start_day = max(start_dt.date(), today)

    expiry_day: Optional[date] = None
    if expiry_dt is not None:
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        else:
            expiry_dt = expiry_dt.astimezone(timezone.utc)
        expiry_day = expiry_dt.date()
        if expiry_day < start_day:
            expiry_day = start_day

    return (
        format_salla_coupon_date(datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)),
        format_salla_coupon_date(datetime.combine(expiry_day, datetime.min.time(), tzinfo=timezone.utc))
        if expiry_day
        else None,
    )


def coerce_salla_coupon_date_string(raw: Optional[str], *, fallback: str) -> str:
    """Strip a Nahla/Salla datetime down to YYYY-MM-DD for coupon create."""
    text = str(raw or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().strftime("%Y-%m-%d")
    except ValueError:
        return fallback


def parse_salla_datetime(raw: Any) -> Optional[str]:
    """Normalise Salla date/datetime strings to ISO for Nahla metadata."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return text


def evaluate_salla_coupon_sync_readiness(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return Full API readiness and optional adapter for coupon operations."""
    from store_integration.registry import (  # noqa: PLC0415
        get_adapter,
        pick_active_salla_integration,
    )

    integration = pick_active_salla_integration(db, tenant_id)
    if not integration:
        return {
            "full_api_ready": False,
            "adapter_ready": False,
            "reason": NO_SALLA_ADAPTER_MSG_AR,
            "adapter": None,
        }

    cfg = integration.config or {}
    has_refresh = bool(cfg.get("refresh_token"))
    full_api_ready = (
        bool(cfg.get("api_sync_enabled"))
        and has_refresh
        and bool(integration.enabled)
        and not bool(cfg.get("needs_reauth"))
    )

    adapter = get_adapter(tenant_id)
    adapter_ready = bool(
        adapter
        and hasattr(adapter, "create_coupon")
        and hasattr(adapter, "get_coupons")
    )

    if not full_api_ready:
        return {
            "full_api_ready": False,
            "adapter_ready": adapter_ready,
            "reason": FULL_API_INCOMPLETE_MSG_AR,
            "adapter": adapter if adapter_ready else None,
        }

    if not adapter_ready:
        return {
            "full_api_ready": True,
            "adapter_ready": False,
            "reason": NO_SALLA_ADAPTER_MSG_AR,
            "adapter": None,
        }

    return {
        "full_api_ready": True,
        "adapter_ready": True,
        "reason": "",
        "adapter": adapter,
    }


def _coupon_push_kwargs(coupon: Coupon) -> Dict[str, Any]:
    meta = coupon.extra_metadata or {}
    now = datetime.now(timezone.utc)

    starts_at = meta.get("starts_at")
    start_dt = now
    if starts_at:
        try:
            start_dt = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            start_dt = now

    expiry_dt: Optional[datetime] = None
    if coupon.expires_at:
        expiry_dt = coupon.expires_at
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

    expiry_days = 3
    if expiry_dt:
        delta = expiry_dt - now
        expiry_days = max(1, int(delta.total_seconds() // 86400) or 1)

    discount_value = 10
    try:
        discount_value = int(float(str(coupon.discount_value or "0").replace(",", ".")))
    except (ValueError, TypeError):
        pass

    usage_limit = meta.get("usage_limit")
    usage_limit_int: Optional[int] = None
    if usage_limit not in (None, "", 0):
        try:
            usage_limit_int = int(usage_limit)
        except (ValueError, TypeError):
            usage_limit_int = None

    minimum_order = meta.get("min_order_amount") or meta.get("minimum_order")
    minimum_order_float: Optional[float] = None
    if minimum_order not in (None, "", 0):
        try:
            minimum_order_float = float(minimum_order)
        except (ValueError, TypeError):
            minimum_order_float = None

    start_str, expiry_str = normalize_salla_coupon_push_dates(start_dt, expiry_dt, now=now)

    return {
        "code": coupon.code,
        "discount_type": coupon.discount_type or "percentage",
        "discount_value": discount_value,
        "start_date": start_str,
        "expiry_date": expiry_str,
        "expiry_days": expiry_days,
        "usage_limit": usage_limit_int,
        "minimum_order": minimum_order_float,
    }


def _extract_create_error(adapter: Any) -> str:
    getter = getattr(adapter, "get_last_coupon_create_error", None)
    if callable(getter):
        err = getter()
        if err:
            return str(err)
    return "فشل إنشاء الكوبون في سلة"


def apply_nahla_push_metadata(
    coupon: Coupon,
    *,
    success: bool,
    synced_at: datetime,
    salla_raw: Optional[Dict[str, Any]] = None,
    sync_error: Optional[str] = None,
) -> Dict[str, Any]:
    meta = dict(coupon.extra_metadata or {})
    meta["sync_direction"] = "nahla_to_salla"
    meta["last_synced_at"] = synced_at.isoformat()

    if success:
        meta["salla_synced"] = True
        meta["sync_status"] = "synced"
        meta.pop("sync_error", None)
        salla_id = extract_salla_coupon_id(salla_raw or {})
        if salla_id:
            meta["salla_coupon_id"] = salla_id
            meta["external_id"] = salla_id
    else:
        meta["salla_synced"] = False
        meta["sync_status"] = "failed"
        if sync_error:
            meta["sync_error"] = sync_error[:500]

    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")
    return meta


def apply_not_pushed_metadata(coupon: Coupon, *, reason: str) -> Dict[str, Any]:
    meta = dict(coupon.extra_metadata or {})
    meta["salla_synced"] = False
    meta["sync_status"] = "not_pushed"
    meta["sync_error"] = reason[:500]
    meta.setdefault("sync_direction", "nahla_to_salla")
    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")
    return meta


def is_pushable_manual_coupon(coupon: Coupon) -> bool:
    if coupon.source_type == "imported":
        return False
    meta = coupon.extra_metadata or {}
    if str(meta.get("source") or "").lower() == "salla":
        return False
    return coupon.source_type in (None, "", "manual") or str(meta.get("source") or "").lower() in (
        "dashboard",
        "manual",
        "",
    )


async def push_coupon_to_salla(
    db: Session,
    tenant_id: int,
    coupon: Coupon,
    *,
    adapter: Any = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Push an existing Nahla coupon to Salla. Returns (success, result_dict)."""
    if coupon.tenant_id != tenant_id:
        return False, {"sync_error": "Coupon not found for tenant"}

    if not is_pushable_manual_coupon(coupon):
        return False, {"sync_error": NOT_MANUAL_COUPON_MSG_AR}

    if adapter is None:
        readiness = evaluate_salla_coupon_sync_readiness(db, tenant_id)
        if not readiness["full_api_ready"]:
            meta = apply_not_pushed_metadata(coupon, reason=readiness["reason"] or FULL_API_INCOMPLETE_MSG_AR)
            db.add(coupon)
            return False, {
                "sync_status": meta.get("sync_status"),
                "sync_error": meta.get("sync_error"),
                "full_api_ready": False,
            }
        adapter = readiness.get("adapter")

    if not adapter or not hasattr(adapter, "create_coupon"):
        meta = apply_not_pushed_metadata(coupon, reason=NO_SALLA_ADAPTER_MSG_AR)
        db.add(coupon)
        return False, {
            "sync_status": meta.get("sync_status"),
            "sync_error": meta.get("sync_error"),
        }

    kwargs = _coupon_push_kwargs(coupon)
    synced_at = datetime.now(timezone.utc)

    try:
        salla_raw = await adapter.create_coupon(**kwargs)
    except Exception as exc:
        logger.warning("tenant=%s push coupon %s failed: %s", tenant_id, coupon.code, exc)
        apply_nahla_push_metadata(
            coupon,
            success=False,
            synced_at=synced_at,
            sync_error=str(exc)[:500],
        )
        db.add(coupon)
        return False, {"sync_status": "failed", "sync_error": str(exc)[:500]}

    if not salla_raw:
        err = _extract_create_error(adapter)
        apply_nahla_push_metadata(
            coupon,
            success=False,
            synced_at=synced_at,
            sync_error=err,
        )
        db.add(coupon)
        return False, {"sync_status": "failed", "sync_error": err}

    meta = apply_nahla_push_metadata(
        coupon,
        success=True,
        synced_at=synced_at,
        salla_raw=salla_raw if isinstance(salla_raw, dict) else None,
    )
    if not meta.get("starts_at"):
        meta["starts_at"] = synced_at.isoformat()
        coupon.extra_metadata = meta
        flag_modified(coupon, "extra_metadata")
    db.add(coupon)
    return True, {
        "sync_status": "synced",
        "salla_coupon_id": meta.get("salla_coupon_id"),
        "salla_synced": True,
    }
