"""
services/coupon_generator.py
─────────────────────────────
Automatic coupon pool management.

Maintains a pool of pre-generated coupons per customer segment so the AI
agent can immediately hand out a real coupon during a conversation.

Pool size: 15 coupons per segment (5 segments = 75 coupons max per tenant).
Coupons are created both in Salla and stored locally.
"""
from __future__ import annotations

import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

_THIS = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Coupon, Tenant, TenantSettings  # noqa: E402

logger = logging.getLogger("nahla-backend")

POOL_SIZE_PER_SEGMENT = 15
SHORT_CODE_PREFIX = "NHL"
SHORT_CODE_DIGITS = 3
SHORT_CODE_LENGTH = len(SHORT_CODE_PREFIX) + SHORT_CODE_DIGITS
KSA_TZ = timezone(timedelta(hours=3))

SEGMENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "new":     {"discount_pct": 15, "expiry_days": 1, "label": "عميل جديد"},
    "active":  {"discount_pct": 5,  "expiry_days": 3, "label": "عميل نشط"},
    "vip":     {"discount_pct": 20, "expiry_days": 3, "label": "عميل مميز"},
    "at_risk": {"discount_pct": 25, "expiry_days": 1, "label": "في خطر المغادرة"},
    "inactive": {"discount_pct": 30, "expiry_days": 1, "label": "عميل غير نشط"},
}

SEGMENT_ALIASES: Dict[str, str] = {
    "churned": "inactive",
}


def _canonical_segment(segment: str) -> str:
    raw = str(segment or "").strip().lower()
    return SEGMENT_ALIASES.get(raw, raw or "active")


def _is_short_coupon_code(code: Optional[str]) -> bool:
    value = str(code or "").strip().upper()
    return value.startswith(SHORT_CODE_PREFIX) and len(value) == SHORT_CODE_LENGTH and value[3:].isdigit()


def _next_short_code(reserved_codes: set[str]) -> str:
    available = [
        f"{SHORT_CODE_PREFIX}{index:03d}"
        for index in range(10 ** SHORT_CODE_DIGITS)
        if f"{SHORT_CODE_PREFIX}{index:03d}" not in reserved_codes
    ]
    if not available:
        raise RuntimeError("No short coupon codes left in NHL000-NHL999 range for this tenant.")
    code = random.choice(available)
    reserved_codes.add(code)
    return code


def _parse_provider_expiry(raw_value: Any) -> Optional[datetime]:
    if raw_value in (None, ""):
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_coupon_expiry(provider_result: Optional[Dict[str, Any]], fallback_days: int) -> datetime:
    raw_expiry = None
    if isinstance(provider_result, dict):
        raw_expiry = (
            provider_result.get("expire_date")
            or provider_result.get("expiry_date")
            or provider_result.get("expires_at")
        )
    parsed = _parse_provider_expiry(raw_expiry)
    if parsed:
        return parsed
    return datetime.now(timezone.utc) + timedelta(days=fallback_days)


def build_coupon_send_payload(coupon: Coupon) -> Dict[str, Optional[str]]:
    expires_at = getattr(coupon, "expires_at", None)
    if expires_at and getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_iso = expires_at.astimezone(timezone.utc).isoformat() if expires_at else None
    expires_text = None
    if expires_at:
        local_expiry = expires_at.astimezone(KSA_TZ)
        expires_text = f"{local_expiry:%Y-%m-%d} الساعة {local_expiry:%H:%M} بتوقيت السعودية"
    return {
        "code": str(getattr(coupon, "code", "") or ""),
        "expires_at": expires_iso,
        "expires_text": expires_text,
    }


_DEFAULT_MAX_DISCOUNT = 10

def _get_merchant_limits(db: Session, tenant_id: int) -> Dict[str, int]:
    """Read max discount from TenantSettings.ai_settings (dashboard source of truth),
    falling back to Tenant.coupon_policy, then to _DEFAULT_MAX_DISCOUNT."""
    # Primary source: ai_settings.allowed_discount_levels (set from dashboard)
    ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if ts:
        ai = ts.ai_settings or {}
        try:
            max_disc = int(ai.get("allowed_discount_levels", 0))
            if max_disc > 0:
                return {"min_discount": 0, "max_discount": max_disc}
        except (ValueError, TypeError):
            pass

    # Fallback: Tenant.coupon_policy
    tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if tenant:
        policy = tenant.coupon_policy or {}
        max_val = policy.get("max_discount")
        if max_val is not None:
            return {
                "min_discount": int(policy.get("min_discount", 0)),
                "max_discount": int(max_val),
            }

    return {"min_discount": 0, "max_discount": _DEFAULT_MAX_DISCOUNT}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


class CouponGeneratorService:
    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id

    def _reserved_codes(self) -> set[str]:
        rows = (
            self.db.query(Coupon.code)
            .filter(Coupon.tenant_id == self.tenant_id)
            .all()
        )
        return {str(code or "").strip().upper() for (code,) in rows if code}

    def _mark_coupon_sent(
        self,
        coupon: Coupon,
        *,
        sent_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> None:
        sent_at = sent_at or datetime.now(timezone.utc)
        meta = dict(coupon.extra_metadata or {})
        meta["used"] = "true"
        meta["used_at"] = sent_at.isoformat()
        meta["sent_at"] = sent_at.isoformat()
        send_payload = build_coupon_send_payload(coupon)
        if send_payload.get("expires_at"):
            meta["sent_expiry_at"] = send_payload["expires_at"]
        if send_payload.get("expires_text"):
            meta["sent_expiry_text"] = send_payload["expires_text"]
        coupon.extra_metadata = meta
        flag_modified(coupon, "extra_metadata")
        if commit:
            self.db.commit()

    def _count_pool(self, segment: str) -> int:
        """Count unused auto-coupons for a segment that haven't expired."""
        segment = _canonical_segment(segment)
        now = datetime.now(timezone.utc)
        return (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                Coupon.code.like(f"{SHORT_CODE_PREFIX}___"),
                func.length(Coupon.code) == SHORT_CODE_LENGTH,
                Coupon.extra_metadata["source"].astext == "auto",
                Coupon.extra_metadata["target_segment"].astext == segment,
                Coupon.extra_metadata["used"].astext != "true",
                Coupon.extra_metadata["salla_synced"].astext == "true",
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            )
            .count()
        )

    async def ensure_coupon_pool(self) -> Dict[str, int]:
        """Top up the coupon pool for all segments. Returns counts created per segment."""
        limits = _get_merchant_limits(self.db, self.tenant_id)
        adapter = self._get_adapter()
        created: Dict[str, int] = {}
        reserved_codes = self._reserved_codes()

        for segment, defaults in SEGMENT_DEFAULTS.items():
            current = self._count_pool(segment)
            needed = POOL_SIZE_PER_SEGMENT - current
            if needed <= 0:
                created[segment] = 0
                continue

            discount = _clamp(
                defaults["discount_pct"],
                limits["min_discount"],
                limits["max_discount"],
            )
            expiry_days = defaults["expiry_days"]
            count = 0
            for _ in range(needed):
                try:
                    code = _next_short_code(reserved_codes)
                except RuntimeError as exc:
                    logger.error("tenant=%s coupon pool exhausted: %s", self.tenant_id, exc)
                    break
                salla_ok = False
                result = None
                if adapter:
                    try:
                        result = await adapter.create_coupon(
                            code=code,
                            discount_type="percentage",
                            discount_value=discount,
                            expiry_days=expiry_days,
                        )
                        salla_ok = result is not None
                    except Exception as exc:
                        logger.warning("Salla coupon create failed: %s", exc)

                if not salla_ok:
                    logger.warning(
                        "tenant=%s skipping local auto coupon because Salla creation failed | segment=%s code=%s",
                        self.tenant_id, segment, code,
                    )
                    continue

                exp_dt = _resolve_coupon_expiry(result if isinstance(result, dict) else None, expiry_days)

                self.db.add(Coupon(
                    tenant_id=self.tenant_id,
                    code=code,
                    description=f"كوبون تلقائي - {defaults['label']}",
                    discount_type="percentage",
                    discount_value=str(discount),
                    expires_at=exp_dt,
                    extra_metadata={
                        "source": "auto",
                        "target_segment": segment,
                        "discount_pct": discount,
                        "used": "false",
                        "salla_synced": salla_ok,
                        "category": "auto",
                        "active": True,
                    },
                ))
                count += 1

            created[segment] = count

        self.db.commit()
        total = sum(created.values())
        if total:
            logger.info(
                "tenant=%s coupon pool topped up: %s",
                self.tenant_id, created,
            )
        return created

    def pick_coupon_for_segment(self, segment: str) -> Optional[Coupon]:
        """Pick an available auto-coupon for the given segment."""
        segment = _canonical_segment(segment)
        now = datetime.now(timezone.utc)
        coupon = (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                Coupon.code.like(f"{SHORT_CODE_PREFIX}___"),
                func.length(Coupon.code) == SHORT_CODE_LENGTH,
                Coupon.extra_metadata["source"].astext == "auto",
                Coupon.extra_metadata["target_segment"].astext == segment,
                Coupon.extra_metadata["used"].astext != "true",
                Coupon.extra_metadata["salla_synced"].astext == "true",
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            )
            .first()
        )
        if coupon:
            self._mark_coupon_sent(coupon, sent_at=now, commit=True)
        return coupon

    async def create_on_demand(
        self,
        segment: str,
        requested_discount_pct: Optional[int] = None,
    ) -> Optional[Coupon]:
        """Create a single coupon on-demand when the pool is empty."""
        segment = _canonical_segment(segment)
        limits = _get_merchant_limits(self.db, self.tenant_id)
        defaults = SEGMENT_DEFAULTS.get(segment, SEGMENT_DEFAULTS["active"])
        base_discount = defaults["discount_pct"]
        if isinstance(requested_discount_pct, int):
            base_discount = requested_discount_pct
        discount = _clamp(base_discount, limits["min_discount"], limits["max_discount"])
        expiry_days = defaults["expiry_days"]
        reserved_codes = self._reserved_codes()
        try:
            code = _next_short_code(reserved_codes)
        except RuntimeError as exc:
            logger.error("tenant=%s cannot create on-demand coupon: %s", self.tenant_id, exc)
            return None

        adapter = self._get_adapter()
        salla_ok = False
        result = None
        if adapter:
            try:
                result = await adapter.create_coupon(
                    code=code, discount_type="percentage",
                    discount_value=discount, expiry_days=expiry_days,
                )
                salla_ok = result is not None
            except Exception as exc:
                logger.warning("On-demand Salla coupon failed: %s", exc)

        if not salla_ok:
            logger.warning(
                "tenant=%s on-demand coupon creation aborted because Salla rejected coupon | segment=%s code=%s",
                self.tenant_id, segment, code,
            )
            return None

        exp_dt = _resolve_coupon_expiry(result if isinstance(result, dict) else None, expiry_days)
        now = datetime.now(timezone.utc)

        coupon = Coupon(
            tenant_id=self.tenant_id,
            code=code,
            description=f"كوبون فوري - {defaults.get('label', segment)}",
            discount_type="percentage",
            discount_value=str(discount),
            expires_at=exp_dt,
            extra_metadata={
                "source": "auto",
                "target_segment": segment,
                "discount_pct": discount,
                "used": "true",
                "used_at": now.isoformat(),
                "salla_synced": salla_ok,
                "on_demand": True,
                "category": "auto",
                "active": True,
            },
        )
        self.db.add(coupon)
        self.db.flush()
        self._mark_coupon_sent(coupon, sent_at=now, commit=False)
        self.db.commit()
        return coupon

    def _get_adapter(self):
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(_THIS, "..")))
            from store_integration.registry import get_adapter
            return get_adapter(self.tenant_id)
        except Exception:
            return None
