"""Deterministic customer-request coupon issuance (Phase 2A).

This service is the coupon-truth owner for a future customer_coupon_request
capability. It does not classify customer wording. Brain live routing and
live issuance remain OFF in this PR — callers must pass allow_issuance=True.

Existing CRM/autopilot/campaign APIs are not used as the public issuance path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from models import Coupon, Customer
from services.coupon_generator import (
    CouponGeneratorService,
    _get_ai_policy,
    _get_coupon_dashboard_block,
)
from services.coupon_level_contract import resolve_coupon_level_for_order_count
from services.customer_intelligence import CustomerIntelligenceService
from services.order_countability_policy import is_countable_order

# Production Brain wiring stays off. Tests call issue_customer_coupon with
# allow_issuance=True. The shadow capability probe must never call this.
CUSTOMER_COUPON_LIVE_ROUTING = False
CUSTOMER_COUPON_LIVE_ISSUANCE = False

COUNT_SOURCE_CI_PHONE_INDEX = "customer_intelligence_phone_index"

REASON_ISSUED = "issued"
REASON_REUSED = "reused_existing_assignment"
REASON_IDENTITY_UNAVAILABLE = "identity_unavailable"
REASON_NO_LEVEL = "no_level"
REASON_LEVEL_DISABLED = "level_disabled"
REASON_LEVEL_NOT_ALLOWED_FOR_AI = "level_not_allowed_for_ai"
REASON_AI_POLICY_DISABLED = "ai_policy_disabled"
REASON_CHANNEL_NOT_ALLOWED = "channel_not_allowed"
REASON_POOL_EMPTY = "pool_empty"
REASON_GENERATION_NOT_AUTHORIZED = "generation_not_authorized"
REASON_SALLA_UNAVAILABLE = "salla_unavailable"
REASON_EXPIRED_OR_INSUFFICIENT_TTL = "expired_or_insufficient_ttl"
REASON_ALLOCATION_CONFLICT = "allocation_conflict"
REASON_LIVE_ISSUANCE_DISABLED = "live_issuance_disabled"
REASON_TENANT_MISMATCH = "tenant_mismatch"
REASON_CONSUMED = "coupon_consumed"

CLOSED_REASON_CODES = frozenset(
    {
        REASON_ISSUED,
        REASON_REUSED,
        REASON_IDENTITY_UNAVAILABLE,
        REASON_NO_LEVEL,
        REASON_LEVEL_DISABLED,
        REASON_LEVEL_NOT_ALLOWED_FOR_AI,
        REASON_AI_POLICY_DISABLED,
        REASON_CHANNEL_NOT_ALLOWED,
        REASON_POOL_EMPTY,
        REASON_GENERATION_NOT_AUTHORIZED,
        REASON_SALLA_UNAVAILABLE,
        REASON_EXPIRED_OR_INSUFFICIENT_TTL,
        REASON_ALLOCATION_CONFLICT,
        REASON_LIVE_ISSUANCE_DISABLED,
        REASON_TENANT_MISMATCH,
        REASON_CONSUMED,
    }
)

POOL_MODE_POOL_FIRST = "pool_first"
POOL_MODE_POOL_ONLY = "pool_only"
POOL_MODE_ON_DEMAND_ONLY = "on_demand_only"


@dataclass(frozen=True)
class CustomerOrderCount:
    customer_id: int
    raw_orders: int
    countable_orders: int
    excluded_orders: int
    count_source: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "raw_orders": self.raw_orders,
            "countable_orders": self.countable_orders,
            "excluded_orders": self.excluded_orders,
            "count_source": self.count_source,
        }


@dataclass(frozen=True)
class CustomerCouponIssuanceResult:
    customer_id: Optional[int]
    countable_orders: int
    resolved_level: Optional[str]
    policy_allowed: bool
    issued: bool
    coupon_id: Optional[int]
    code: Optional[str]
    discount_type: Optional[str]
    discount_value: Optional[str]
    expires_at: Optional[str]
    min_order_amount: Optional[float]
    restrictions: Dict[str, Any] = field(default_factory=dict)
    reason_code: str = REASON_NO_LEVEL
    count_source: Optional[str] = None
    raw_orders: Optional[int] = None
    excluded_orders: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "countable_orders": self.countable_orders,
            "resolved_level": self.resolved_level,
            "policy_allowed": self.policy_allowed,
            "issued": self.issued,
            "coupon_id": self.coupon_id,
            "code": self.code,
            "discount_type": self.discount_type,
            "discount_value": self.discount_value,
            "expires_at": self.expires_at,
            "min_order_amount": self.min_order_amount,
            "restrictions": dict(self.restrictions or {}),
            "reason_code": self.reason_code,
            "count_source": self.count_source,
            "raw_orders": self.raw_orders,
            "excluded_orders": self.excluded_orders,
        }


def count_customer_orders(
    db: Session,
    tenant_id: int,
    customer_id: int,
) -> Optional[CustomerOrderCount]:
    """Authoritative countable-order lookup via Customer Intelligence phone index."""
    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.id == int(customer_id))
        .first()
    )
    if customer is None:
        return None
    intel = CustomerIntelligenceService(db, tenant_id)
    orders = intel._orders_for_customer(customer)
    countable = [row for row in orders if is_countable_order(row)]
    excluded = len(orders) - len(countable)
    return CustomerOrderCount(
        customer_id=int(customer.id),
        raw_orders=len(orders),
        countable_orders=len(countable),
        excluded_orders=excluded,
        count_source=COUNT_SOURCE_CI_PHONE_INDEX,
    )


def _first_purchase_rule_from_dashboard(block: Mapping[str, Any]) -> Any:
    rules = block.get("rules") or []
    if isinstance(rules, Mapping):
        return rules.get("first_purchase")
    if isinstance(rules, list):
        for entry in rules:
            if isinstance(entry, Mapping) and str(entry.get("id") or "") == "first_purchase":
                return entry
    return block.get("first_purchase_rule") or block.get("first_purchase")


def _level_entry(block: Mapping[str, Any], level_id: str) -> Dict[str, Any]:
    raw = block.get("levels") or []
    if isinstance(raw, Mapping):
        entry = raw.get(level_id)
        return dict(entry) if isinstance(entry, Mapping) else {"id": level_id}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping) and str(entry.get("id") or "").lower() == level_id:
                return dict(entry)
    return {"id": level_id}


def _global_defaults(block: Mapping[str, Any]) -> Dict[str, Any]:
    raw = block.get("global_defaults")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _coupon_consumed(coupon: Coupon) -> bool:
    meta = dict(coupon.extra_metadata or {})
    if _truthy(meta.get("redeemed")) or _truthy(meta.get("consumed")):
        return True
    try:
        usage_count = int(meta.get("usage_count") or meta.get("usages") or 0)
    except (TypeError, ValueError):
        usage_count = 0
    raw_limit = meta.get("usage_limit")
    if raw_limit in (None, ""):
        raw_limit = meta.get("limit")
    try:
        limit_val = int(raw_limit) if raw_limit not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        limit_val = None
    return bool(limit_val is not None and usage_count >= limit_val)


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _assignment_remaining_ok(
    coupon: Coupon,
    *,
    min_remaining_hours: int,
    now: datetime,
) -> Tuple[bool, Optional[str]]:
    exp = _as_aware(coupon.expires_at)
    if exp is not None and exp <= now:
        return False, REASON_EXPIRED_OR_INSUFFICIENT_TTL
    if min_remaining_hours > 0 and exp is not None:
        if (exp - now) < timedelta(hours=min_remaining_hours):
            return False, REASON_EXPIRED_OR_INSUFFICIENT_TTL
    if _coupon_consumed(coupon):
        return False, REASON_CONSUMED
    return True, None


def find_reusable_assigned_coupon(
    db: Session,
    tenant_id: int,
    customer_id: int,
    *,
    min_remaining_hours: int = 0,
    now: Optional[datetime] = None,
) -> Optional[Coupon]:
    now = now or datetime.now(timezone.utc)
    owner = str(int(customer_id))
    rows = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id)
        .order_by(Coupon.id.desc())
        .limit(200)
        .all()
    )
    for coupon in rows:
        meta_owner = (coupon.extra_metadata or {}).get("customer_id")
        if meta_owner in (None, "", "null"):
            continue
        if str(meta_owner) != owner:
            continue
        ok, _reason = _assignment_remaining_ok(
            coupon, min_remaining_hours=min_remaining_hours, now=now
        )
        if ok:
            return coupon
    return None


def _empty_result(
    *,
    customer_id: Optional[int],
    countable_orders: int,
    resolved_level: Optional[str],
    reason_code: str,
    policy_allowed: bool = False,
    count: Optional[CustomerOrderCount] = None,
) -> CustomerCouponIssuanceResult:
    return CustomerCouponIssuanceResult(
        customer_id=customer_id,
        countable_orders=countable_orders if count is None else count.countable_orders,
        resolved_level=resolved_level,
        policy_allowed=policy_allowed,
        issued=False,
        coupon_id=None,
        code=None,
        discount_type=None,
        discount_value=None,
        expires_at=None,
        min_order_amount=None,
        restrictions={},
        reason_code=reason_code,
        count_source=None if count is None else count.count_source,
        raw_orders=None if count is None else count.raw_orders,
        excluded_orders=None if count is None else count.excluded_orders,
    )


def _result_from_coupon(
    coupon: Coupon,
    *,
    count: CustomerOrderCount,
    resolved_level: str,
    reason_code: str,
    min_order_amount: Optional[float],
    restrictions: Dict[str, Any],
) -> CustomerCouponIssuanceResult:
    exp = _as_aware(coupon.expires_at)
    return CustomerCouponIssuanceResult(
        customer_id=count.customer_id,
        countable_orders=count.countable_orders,
        resolved_level=resolved_level,
        policy_allowed=True,
        issued=True,
        coupon_id=int(coupon.id),
        code=str(coupon.code or "") or None,
        discount_type=str(coupon.discount_type or "") or None,
        discount_value=str(coupon.discount_value) if coupon.discount_value is not None else None,
        expires_at=exp.isoformat() if exp is not None else None,
        min_order_amount=min_order_amount,
        restrictions=dict(restrictions or {}),
        reason_code=reason_code,
        count_source=count.count_source,
        raw_orders=count.raw_orders,
        excluded_orders=count.excluded_orders,
    )


async def issue_customer_coupon(
    db: Session,
    tenant_id: int,
    customer_id: int,
    *,
    for_channel: str = "ai",
    allow_issuance: bool = False,
) -> CustomerCouponIssuanceResult:
    """Issue or reuse a customer-owned coupon. No customer-facing prose."""
    if int(tenant_id) <= 0:
        return _empty_result(
            customer_id=None,
            countable_orders=0,
            resolved_level=None,
            reason_code=REASON_TENANT_MISMATCH,
        )

    count = count_customer_orders(db, tenant_id, customer_id)
    if count is None:
        return _empty_result(
            customer_id=None,
            countable_orders=0,
            resolved_level=None,
            reason_code=REASON_IDENTITY_UNAVAILABLE,
        )

    block = _get_coupon_dashboard_block(db, tenant_id)
    policy = _get_ai_policy(db, tenant_id)
    first_purchase = _first_purchase_rule_from_dashboard(block)
    resolution = resolve_coupon_level_for_order_count(
        block.get("levels"),
        count.countable_orders,
        first_purchase_rule=first_purchase,
    )
    resolved_level = resolution.level_id
    defaults = _global_defaults(block)
    try:
        min_order_amount = float(defaults.get("min_order_amount") or 0)
    except (TypeError, ValueError):
        min_order_amount = 0.0
    restrictions = {
        "min_order_amount": min_order_amount,
        "max_uses": resolution.max_uses,
        "per_customer_usage": resolution.per_customer_usage,
        "combinable_with_offers": bool(defaults.get("combinable_with_offers", False)),
    }

    if resolved_level is None:
        return _empty_result(
            customer_id=count.customer_id,
            countable_orders=count.countable_orders,
            resolved_level=None,
            reason_code=REASON_NO_LEVEL,
            count=count,
        )

    level_cfg = _level_entry(block, resolved_level)
    if not bool(level_cfg.get("enabled", True)):
        return _empty_result(
            customer_id=count.customer_id,
            countable_orders=count.countable_orders,
            resolved_level=resolved_level,
            reason_code=REASON_LEVEL_DISABLED,
            count=count,
        )

    channel = str(for_channel or "ai").lower()
    allowed_channels = [
        str(c).lower()
        for c in (level_cfg.get("allowed_channels") or resolution.allowed_channels or [])
        if c
    ]
    if allowed_channels and channel not in allowed_channels:
        return _empty_result(
            customer_id=count.customer_id,
            countable_orders=count.countable_orders,
            resolved_level=resolved_level,
            reason_code=REASON_CHANNEL_NOT_ALLOWED
            if channel != "ai"
            else REASON_LEVEL_NOT_ALLOWED_FOR_AI,
            count=count,
        )

    if channel == "ai":
        if not bool(policy.get("enabled", True)):
            return _empty_result(
                customer_id=count.customer_id,
                countable_orders=count.countable_orders,
                resolved_level=resolved_level,
                reason_code=REASON_AI_POLICY_DISABLED,
                count=count,
            )
        allowed_levels = [str(x).lower() for x in (policy.get("allowed_levels") or [])]
        if allowed_levels and resolved_level not in allowed_levels:
            return _empty_result(
                customer_id=count.customer_id,
                countable_orders=count.countable_orders,
                resolved_level=resolved_level,
                reason_code=REASON_LEVEL_NOT_ALLOWED_FOR_AI,
                count=count,
            )

    if not allow_issuance:
        return _empty_result(
            customer_id=count.customer_id,
            countable_orders=count.countable_orders,
            resolved_level=resolved_level,
            policy_allowed=True,
            reason_code=REASON_LIVE_ISSUANCE_DISABLED,
            count=count,
        )

    min_remaining_hours = int(policy.get("min_remaining_hours") or 0) if channel == "ai" else 0
    existing = find_reusable_assigned_coupon(
        db,
        tenant_id,
        count.customer_id,
        min_remaining_hours=min_remaining_hours,
    )
    if existing is not None:
        return _result_from_coupon(
            existing,
            count=count,
            resolved_level=str(existing.coupon_level or resolved_level),
            reason_code=REASON_REUSED,
            min_order_amount=min_order_amount,
            restrictions=restrictions,
        )

    pool_mode = str(policy.get("pool_mode") or POOL_MODE_POOL_FIRST).lower()
    if pool_mode not in {POOL_MODE_POOL_FIRST, POOL_MODE_POOL_ONLY, POOL_MODE_ON_DEMAND_ONLY}:
        pool_mode = POOL_MODE_POOL_FIRST

    generator = CouponGeneratorService(db, tenant_id)
    coupon: Optional[Coupon] = None

    if pool_mode != POOL_MODE_ON_DEMAND_ONLY:
        coupon = generator.pick_coupon_for_level(
            resolved_level,
            count.customer_id,
            for_channel=channel,
        )

    if coupon is None and pool_mode == POOL_MODE_POOL_ONLY:
        return _empty_result(
            customer_id=count.customer_id,
            countable_orders=count.countable_orders,
            resolved_level=resolved_level,
            policy_allowed=True,
            reason_code=REASON_POOL_EMPTY,
            count=count,
        )

    if coupon is None:
        coupon = await generator.create_on_demand_for_level(
            resolved_level,
            customer_id=count.customer_id,
            for_channel=channel,
        )
        if coupon is None:
            adapter = generator._get_adapter()
            reason = REASON_SALLA_UNAVAILABLE if adapter is None else REASON_POOL_EMPTY
            return _empty_result(
                customer_id=count.customer_id,
                countable_orders=count.countable_orders,
                resolved_level=resolved_level,
                policy_allowed=True,
                reason_code=reason,
                count=count,
            )

    return _result_from_coupon(
        coupon,
        count=count,
        resolved_level=resolved_level,
        reason_code=REASON_ISSUED,
        min_order_amount=min_order_amount,
        restrictions=restrictions,
    )


__all__ = [
    "CLOSED_REASON_CODES",
    "COUNT_SOURCE_CI_PHONE_INDEX",
    "CUSTOMER_COUPON_LIVE_ISSUANCE",
    "CUSTOMER_COUPON_LIVE_ROUTING",
    "CustomerCouponIssuanceResult",
    "CustomerOrderCount",
    "count_customer_orders",
    "find_reusable_assigned_coupon",
    "issue_customer_coupon",
]
