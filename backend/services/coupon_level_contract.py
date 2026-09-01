"""Coupon level settings contract for customer-request issuance.

Numeric ``min_orders`` is the runtime authority. Presentation ``threshold``
text is UI-only and must never be parsed.

This module is independent of MerchantBrain and of CRM status mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

CANONICAL_COUPON_LEVEL_IDS = ("bronze", "silver", "gold", "vip")

CANONICAL_LEVEL_MIN_ORDERS: Dict[str, int] = {
    "bronze": 1,
    "silver": 3,
    "gold": 7,
    "vip": 15,
}

REASON_NO_LEVEL = "no_level"
REASON_FIRST_PURCHASE_AUTHORIZED = "first_purchase_authorized"
REASON_HIGHEST_ENABLED_MATCH = "highest_enabled_min_orders"


@dataclass(frozen=True)
class CouponLevelResolution:
    order_count: int
    level_id: Optional[str]
    min_orders: Optional[int]
    enabled: bool
    discount_default: Optional[float]
    discount_min: Optional[float]
    discount_max: Optional[float]
    validity_hours: Optional[int]
    max_uses: Optional[int]
    per_customer_usage: Optional[int]
    allowed_channels: tuple[str, ...]
    resolution_reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "order_count": self.order_count,
            "level_id": self.level_id,
            "min_orders": self.min_orders,
            "enabled": self.enabled,
            "discount_default": self.discount_default,
            "discount_min": self.discount_min,
            "discount_max": self.discount_max,
            "validity_hours": self.validity_hours,
            "max_uses": self.max_uses,
            "per_customer_usage": self.per_customer_usage,
            "allowed_channels": list(self.allowed_channels),
            "resolution_reason": self.resolution_reason,
        }


def min_orders_for_level(level_id: str, raw_level: Optional[Mapping[str, Any]] = None) -> int:
    """Return canonical numeric min_orders, backfilling saved rows that omit it."""
    lid = str(level_id or "").strip().lower()
    fallback = int(CANONICAL_LEVEL_MIN_ORDERS.get(lid, 0))
    if not isinstance(raw_level, Mapping):
        return fallback
    if "min_orders" not in raw_level or raw_level.get("min_orders") is None:
        return fallback
    try:
        return max(0, int(raw_level.get("min_orders")))
    except (TypeError, ValueError):
        return fallback


def _as_level_list(levels: Any) -> List[Dict[str, Any]]:
    if isinstance(levels, Mapping):
        out: List[Dict[str, Any]] = []
        for lid in CANONICAL_COUPON_LEVEL_IDS:
            entry = levels.get(lid)
            if isinstance(entry, dict):
                merged = dict(entry)
                merged.setdefault("id", lid)
                out.append(merged)
        return out
    if isinstance(levels, Sequence) and not isinstance(levels, (str, bytes)):
        return [dict(item) for item in levels if isinstance(item, dict)]
    return []


def first_purchase_rule_enabled(first_purchase_rule: Any) -> bool:
    if first_purchase_rule is True:
        return True
    if first_purchase_rule is False or first_purchase_rule is None:
        return False
    if isinstance(first_purchase_rule, Mapping):
        return bool(first_purchase_rule.get("enabled"))
    return bool(first_purchase_rule)


def resolve_coupon_level_for_order_count(
    levels: Any,
    countable_orders: int,
    first_purchase_rule: Any = None,
) -> CouponLevelResolution:
    """Choose the highest enabled configured level whose min_orders <= count.

    Zero orders: no level unless an enabled first-purchase rule authorizes a coupon
    (bronze, using that level's configured min_orders/economics).
    Disabled levels are skipped. AI policy is not applied here.
    """
    try:
        count = max(0, int(countable_orders))
    except (TypeError, ValueError):
        count = 0

    by_id: Dict[str, Dict[str, Any]] = {}
    for entry in _as_level_list(levels):
        lid = str(entry.get("id") or "").strip().lower()
        if lid in CANONICAL_LEVEL_MIN_ORDERS:
            by_id[lid] = entry

    def _empty(*, reason: str, level_id: Optional[str] = None) -> CouponLevelResolution:
        return CouponLevelResolution(
            order_count=count,
            level_id=level_id,
            min_orders=None if level_id is None else min_orders_for_level(level_id, by_id.get(level_id)),
            enabled=False,
            discount_default=None,
            discount_min=None,
            discount_max=None,
            validity_hours=None,
            max_uses=None,
            per_customer_usage=None,
            allowed_channels=(),
            resolution_reason=reason,
        )

    def _from_entry(entry: Dict[str, Any], *, reason: str) -> CouponLevelResolution:
        lid = str(entry.get("id") or "").strip().lower()
        channels = entry.get("allowed_channels") or []
        if not isinstance(channels, Sequence) or isinstance(channels, (str, bytes)):
            channels = ()
        return CouponLevelResolution(
            order_count=count,
            level_id=lid,
            min_orders=min_orders_for_level(lid, entry),
            enabled=bool(entry.get("enabled", True)),
            discount_default=_opt_float(entry.get("discount_default")),
            discount_min=_opt_float(entry.get("discount_min")),
            discount_max=_opt_float(entry.get("discount_max")),
            validity_hours=_opt_int(entry.get("validity_hours")),
            max_uses=_opt_int(entry.get("max_uses")),
            per_customer_usage=_opt_int(entry.get("per_customer_usage")),
            allowed_channels=tuple(str(c).lower() for c in channels if c),
            resolution_reason=reason,
        )

    if count <= 0:
        if first_purchase_rule_enabled(first_purchase_rule):
            bronze = by_id.get("bronze") or {"id": "bronze", "enabled": True}
            if bool(bronze.get("enabled", True)):
                return _from_entry(bronze, reason=REASON_FIRST_PURCHASE_AUTHORIZED)
        return _empty(reason=REASON_NO_LEVEL)

    matches: List[Dict[str, Any]] = []
    for lid in CANONICAL_COUPON_LEVEL_IDS:
        entry = by_id.get(lid) or {"id": lid, "enabled": True}
        if not bool(entry.get("enabled", True)):
            continue
        threshold = min_orders_for_level(lid, entry)
        if threshold <= count:
            matches.append(entry)

    if not matches:
        return _empty(reason=REASON_NO_LEVEL)

    chosen = max(matches, key=lambda item: min_orders_for_level(str(item.get("id")), item))
    return _from_entry(chosen, reason=REASON_HIGHEST_ENABLED_MATCH)


def _opt_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
