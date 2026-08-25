"""Read-only coupon retention reporting helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import Coupon


def _coupon_status(coupon: Coupon, *, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    meta = coupon.extra_metadata or {}
    override = meta.get("active")
    if isinstance(override, bool) and not override:
        return "inactive"
    if str(meta.get("used", "")).lower() in {"true", "1", "yes"}:
        return "used"
    expires = coupon.expires_at
    if expires is not None:
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return "expired"
    return "active"


def _age_bucket(coupon: Coupon, *, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    created = getattr(coupon, "created_at", None)
    if created is None:
        return "unknown"
    if getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=timezone.utc)
    days = (now - created.astimezone(timezone.utc)).days
    if days <= 7:
        return "0_7d"
    if days <= 30:
        return "8_30d"
    if days <= 90:
        return "31_90d"
    return "90d_plus"


def build_coupon_retention_report(db: Session, tenant_id: int) -> Dict[str, Any]:
    rows = db.query(Coupon).filter(Coupon.tenant_id == tenant_id).all()
    groups: Dict[str, Dict[str, Any]] = {}
    for coupon in rows:
        meta = coupon.extra_metadata or {}
        source = str(coupon.source_type or meta.get("source") or "unknown").lower()
        bucket = groups.setdefault(
            source,
            {
                "source": source,
                "total": 0,
                "active": 0,
                "expired": 0,
                "used": 0,
                "inactive": 0,
                "age_buckets": {},
            },
        )
        bucket["total"] += 1
        status = _coupon_status(coupon)
        bucket[status] = bucket.get(status, 0) + 1
        age = _age_bucket(coupon)
        age_map = bucket["age_buckets"]
        age_map[age] = int(age_map.get(age, 0)) + 1
    return {"tenant_id": tenant_id, "total": len(rows), "groups": groups}


def build_nahla_auto_retention_dry_run(db: Session, tenant_id: int) -> Dict[str, Any]:
    rows = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id)
        .filter(Coupon.source_type.in_(["system", "auto"]))
        .all()
    )
    eligible: List[Dict[str, Any]] = []
    expired = 0
    used = 0
    for coupon in rows:
        meta = coupon.extra_metadata or {}
        if str(meta.get("source", "")).lower() not in {"", "auto", "system"}:
            continue
        status = _coupon_status(coupon)
        if status not in {"expired", "used"}:
            continue
        if status == "expired":
            expired += 1
        if status == "used":
            used += 1
        eligible.append(
            {
                "coupon_id": coupon.id,
                "code": coupon.code,
                "status": status,
                "source_type": coupon.source_type,
                "expires_at": coupon.expires_at.isoformat() if coupon.expires_at else None,
            }
        )
    return {
        "tenant_id": tenant_id,
        "eligible_count": len(eligible),
        "expired_count": expired,
        "used_count": used,
        "eligible": eligible,
    }
