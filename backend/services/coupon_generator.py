"""
services/coupon_generator.py
─────────────────────────────
Automatic coupon pool management.

Maintains a pool of pre-generated coupons per customer segment so the AI
agent can immediately hand out a real coupon during a conversation.

Pool size: 3 coupons per segment (5 segments = 15 coupons max per tenant).
Coupons are created both in Salla and stored locally.
"""
from __future__ import annotations

import logging
import os
import random
import string
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

_THIS = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Coupon, Tenant  # noqa: E402

logger = logging.getLogger("nahla-backend")

POOL_SIZE_PER_SEGMENT = 3

SEGMENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "new":     {"discount_pct": 15, "expiry_days": 1, "label": "عميل جديد"},
    "active":  {"discount_pct": 5,  "expiry_days": 3, "label": "عميل نشط"},
    "vip":     {"discount_pct": 20, "expiry_days": 3, "label": "عميل مميز"},
    "at_risk": {"discount_pct": 25, "expiry_days": 1, "label": "في خطر المغادرة"},
    "churned": {"discount_pct": 30, "expiry_days": 1, "label": "عميل خامل"},
}


def _random_code(segment: str) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    seg_tag = segment.upper()[:3]
    return f"NAHLA-{seg_tag}-{suffix}"


def _get_merchant_limits(db: Session, tenant_id: int) -> Dict[str, int]:
    """Read merchant coupon_policy for min/max discount limits."""
    tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if not tenant:
        return {"min_discount": 0, "max_discount": 50}
    policy = tenant.coupon_policy or {}
    return {
        "min_discount": int(policy.get("min_discount", 0)),
        "max_discount": int(policy.get("max_discount", 50)),
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


class CouponGeneratorService:
    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id

    def _count_pool(self, segment: str) -> int:
        """Count unused auto-coupons for a segment that haven't expired."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
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
                code = _random_code(segment)
                salla_ok = False
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

                from datetime import timedelta
                exp_dt = datetime.now(timezone.utc) + timedelta(days=expiry_days)

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
        now = datetime.now(timezone.utc)
        coupon = (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                Coupon.extra_metadata["source"].astext == "auto",
                Coupon.extra_metadata["target_segment"].astext == segment,
                Coupon.extra_metadata["used"].astext != "true",
                Coupon.extra_metadata["salla_synced"].astext == "true",
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            )
            .first()
        )
        if coupon:
            meta = dict(coupon.extra_metadata or {})
            meta["used"] = "true"
            meta["used_at"] = now.isoformat()
            coupon.extra_metadata = meta
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(coupon, "extra_metadata")
            self.db.commit()
        return coupon

    async def create_on_demand(
        self,
        segment: str,
        requested_discount_pct: Optional[int] = None,
    ) -> Optional[Coupon]:
        """Create a single coupon on-demand when the pool is empty."""
        limits = _get_merchant_limits(self.db, self.tenant_id)
        defaults = SEGMENT_DEFAULTS.get(segment, SEGMENT_DEFAULTS["active"])
        base_discount = defaults["discount_pct"]
        if isinstance(requested_discount_pct, int):
            base_discount = requested_discount_pct
        discount = _clamp(base_discount, limits["min_discount"], limits["max_discount"])
        expiry_days = defaults["expiry_days"]
        code = _random_code(segment)

        adapter = self._get_adapter()
        salla_ok = False
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

        from datetime import timedelta
        exp_dt = datetime.now(timezone.utc) + timedelta(days=expiry_days)

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
                "used_at": datetime.now(timezone.utc).isoformat(),
                "salla_synced": salla_ok,
                "on_demand": True,
                "category": "auto",
                "active": True,
            },
        )
        self.db.add(coupon)
        self.db.commit()
        return coupon

    def _get_adapter(self):
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(_THIS, "..")))
            from store_integration.registry import get_adapter
            return get_adapter(self.tenant_id)
        except Exception:
            return None
