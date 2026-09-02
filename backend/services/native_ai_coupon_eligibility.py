"""Explicit Nahla-native AI coupon eligibility.

Additive and independent of the Salla/system warm pool. Missing
``ai_allocatable`` is never treated as true. Eligibility is never inferred
from code text, description, discount, tenant id, or Salla sync state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

NATIVE_AI_SOURCE_TYPE = "manual"
NATIVE_AI_LEVELS = frozenset({"bronze", "silver", "gold", "vip"})
NATIVE_AI_CHANNELS = frozenset({"ai", "shared"})
NATIVE_AI_USAGE_LIMIT = 1


def explicit_ai_allocatable(meta: Optional[Mapping[str, Any]]) -> bool:
    """True only for an explicit boolean/string true marker."""
    raw = (meta or {}).get("ai_allocatable")
    if raw is True:
        return True
    if isinstance(raw, str) and raw.strip().lower() == "true":
        return True
    return False


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _meta_truthy_used(meta: Mapping[str, Any]) -> bool:
    raw = meta.get("used")
    if raw is True:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def native_usage_limit(meta: Mapping[str, Any]) -> Optional[int]:
    raw = meta.get("usage_limit")
    if raw in (None, ""):
        raw = meta.get("limit")
    return _safe_int(raw)


def resolved_allocation_channel(coupon: Any, meta: Mapping[str, Any]) -> str:
    raw = (
        getattr(coupon, "allocation_channel", None)
        or meta.get("allocation_channel")
        or ""
    )
    return str(raw).strip().lower()


def resolved_coupon_level(coupon: Any, meta: Mapping[str, Any]) -> str:
    raw = getattr(coupon, "coupon_level", None) or meta.get("coupon_level") or ""
    return str(raw).strip().lower()


def has_customer_binding(meta: Mapping[str, Any]) -> bool:
    owner = meta.get("customer_id")
    return owner not in (None, "", "null")


def native_one_customer_allocation_safe(meta: Mapping[str, Any]) -> bool:
    """Fail closed unless usage is exactly one and not already consumed."""
    if _meta_truthy_used(meta):
        return False
    if has_customer_binding(meta):
        return False
    if native_usage_limit(meta) != NATIVE_AI_USAGE_LIMIT:
        return False
    usage_count = _safe_int(meta.get("usage_count") or meta.get("usages")) or 0
    if usage_count >= NATIVE_AI_USAGE_LIMIT:
        return False
    if meta.get("redeemed") in (True, "true", "True") or meta.get("consumed") in (
        True,
        "true",
        "True",
    ):
        return False
    return True


def native_coupon_is_active(meta: Mapping[str, Any]) -> bool:
    active = meta.get("active")
    if active is False:
        return False
    if isinstance(active, str) and active.strip().lower() in {"false", "0", "no"}:
        return False
    return True


def remaining_hours_ok(
    expires_at: Optional[datetime],
    *,
    min_remaining_hours: int,
    now: Optional[datetime] = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    exp = _as_aware(expires_at)
    if exp is None:
        return True
    if exp <= now:
        return False
    if min_remaining_hours > 0 and (exp - now) < timedelta(hours=min_remaining_hours):
        return False
    return True


def is_native_ai_allocatable_coupon(
    coupon: Any,
    *,
    tenant_id: int,
    resolved_level: str,
    for_channel: str = "ai",
    min_remaining_hours: int = 0,
    now: Optional[datetime] = None,
) -> bool:
    """Python-side fail-closed contract for one-customer native AI allocation."""
    now = now or datetime.now(timezone.utc)
    if int(getattr(coupon, "tenant_id", 0) or 0) != int(tenant_id):
        return False
    if str(getattr(coupon, "source_type", "") or "").strip().lower() != NATIVE_AI_SOURCE_TYPE:
        return False
    meta = dict(getattr(coupon, "extra_metadata", None) or {})
    if not explicit_ai_allocatable(meta):
        return False
    level = str(resolved_level or "").strip().lower()
    if level not in NATIVE_AI_LEVELS:
        return False
    if resolved_coupon_level(coupon, meta) != level:
        return False
    channel = str(for_channel or "ai").strip().lower()
    alloc = resolved_allocation_channel(coupon, meta)
    if alloc not in NATIVE_AI_CHANNELS:
        return False
    if alloc not in {channel, "shared"}:
        return False
    if not native_coupon_is_active(meta):
        return False
    if not native_one_customer_allocation_safe(meta):
        return False
    if not remaining_hours_ok(
        getattr(coupon, "expires_at", None),
        min_remaining_hours=min_remaining_hours,
        now=now,
    ):
        return False
    return True


def validate_native_ai_opt_in(
    *,
    ai_allocatable: bool,
    coupon_level: Optional[str],
    allocation_channel: Optional[str],
    usage_limit: Optional[int],
) -> Optional[str]:
    """Return an Arabic merchant error, or None when the contract is valid.

    General promotions (ai_allocatable false) need no extra fields.
    """
    if not ai_allocatable:
        return None
    level = str(coupon_level or "").strip().lower()
    if level not in NATIVE_AI_LEVELS:
        return "لتخصيص الذكاء الاصطناعي يجب اختيار مستوى صالح (برونزي، فضي، ذهبي، استثنائي)."
    channel = str(allocation_channel or "").strip().lower()
    if channel not in NATIVE_AI_CHANNELS:
        return "لتخصيص الذكاء الاصطناعي يجب اختيار قناة متوافقة (ذكاء أو مشتركة)."
    if usage_limit != NATIVE_AI_USAGE_LIMIT:
        return "كوبون تخصيص الذكاء يجب أن يكون لعميل واحد (حد الاستخدام = 1)."
    return None


__all__ = [
    "NATIVE_AI_CHANNELS",
    "NATIVE_AI_LEVELS",
    "NATIVE_AI_SOURCE_TYPE",
    "NATIVE_AI_USAGE_LIMIT",
    "explicit_ai_allocatable",
    "is_native_ai_allocatable_coupon",
    "validate_native_ai_opt_in",
]
