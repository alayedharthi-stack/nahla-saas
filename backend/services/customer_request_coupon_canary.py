"""Tenant-scoped canary allowlist for customer-request coupon live routing.

Generic env allowlist. Default empty/off. No tenant-id hardcoded in business
logic. Owner enables tenants after merge/deploy by setting the env var.
"""
from __future__ import annotations

import os
from typing import FrozenSet, Optional, Tuple

ENV_CANARY_TENANTS = "NAHLA_CUSTOMER_COUPON_CANARY_TENANTS"
MAX_ALLOWLIST_TENANTS = 64
REASON_ALLOWLIST_MALFORMED = "allowlist_config_malformed"
REASON_TENANT_NOT_ALLOWLISTED = "tenant_not_allowlisted"
REASON_TENANT_MISSING = "tenant_missing"
REASON_ALLOWED = "allowed"

# (raw_env, parsed_ids, error)
_PARSE_CACHE: Optional[Tuple[str, Optional[FrozenSet[int]], Optional[str]]] = None


def parse_customer_coupon_canary_tenants(raw: Optional[str]) -> Tuple[Optional[FrozenSet[int]], Optional[str]]:
    """Parse comma-separated positive tenant IDs. Empty → empty set. Malformed → error."""
    text = str(raw or "").strip()
    if not text:
        return frozenset(), None
    tokens = [part.strip() for part in text.split(",") if part.strip()]
    if len(tokens) > MAX_ALLOWLIST_TENANTS:
        return None, REASON_ALLOWLIST_MALFORMED
    out: set[int] = set()
    for token in tokens:
        try:
            value = int(token)
        except (TypeError, ValueError):
            return None, REASON_ALLOWLIST_MALFORMED
        if value <= 0:
            return None, REASON_ALLOWLIST_MALFORMED
        out.add(value)
    return frozenset(out), None


def clear_customer_coupon_canary_cache() -> None:
    """Test helper — reset env parse cache."""
    global _PARSE_CACHE
    _PARSE_CACHE = None


def customer_coupon_canary_tenant_ids() -> Tuple[Optional[FrozenSet[int]], Optional[str]]:
    global _PARSE_CACHE
    raw = os.getenv(ENV_CANARY_TENANTS, "")
    if _PARSE_CACHE is not None:
        cached_raw, parsed, error = _PARSE_CACHE
        if cached_raw == raw:
            return parsed, error
    parsed, error = parse_customer_coupon_canary_tenants(raw)
    _PARSE_CACHE = (raw, parsed, error)
    return parsed, error


def is_customer_coupon_canary_tenant(tenant_id: Optional[int]) -> bool:
    """True only when tenant is on the env allowlist. Default false."""
    try:
        tid = int(tenant_id or 0)
    except (TypeError, ValueError):
        return False
    if tid <= 0:
        return False
    allowed, error = customer_coupon_canary_tenant_ids()
    if error or allowed is None:
        return False
    return tid in allowed


__all__ = [
    "ENV_CANARY_TENANTS",
    "REASON_ALLOWED",
    "REASON_ALLOWLIST_MALFORMED",
    "REASON_TENANT_MISSING",
    "REASON_TENANT_NOT_ALLOWLISTED",
    "clear_customer_coupon_canary_cache",
    "customer_coupon_canary_tenant_ids",
    "is_customer_coupon_canary_tenant",
    "parse_customer_coupon_canary_tenants",
]
