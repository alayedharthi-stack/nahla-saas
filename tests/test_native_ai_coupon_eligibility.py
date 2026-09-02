"""Explicit native AI coupon eligibility — no Salla, no inference from text."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, p)

from services.native_ai_coupon_eligibility import (
    explicit_ai_allocatable,
    is_native_ai_allocatable_coupon,
    validate_native_ai_opt_in,
)


def _coupon(**kwargs):
    meta = dict(kwargs.pop("meta", {}) or {})
    defaults = {
        "tenant_id": 33,
        "source_type": "manual",
        "coupon_level": "bronze",
        "allocation_channel": "ai",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=2),
        "extra_metadata": {
            "ai_allocatable": True,
            "active": True,
            "used": "false",
            "usage_limit": 1,
            "usage_count": 0,
        },
    }
    defaults.update(kwargs)
    extra = dict(defaults["extra_metadata"])
    extra.update(meta)
    defaults["extra_metadata"] = extra
    return SimpleNamespace(**defaults)


def test_missing_marker_is_not_allocatable():
    assert explicit_ai_allocatable({}) is False
    assert explicit_ai_allocatable({"ai_allocatable": False}) is False
    assert explicit_ai_allocatable({"ai_allocatable": "yes"}) is False
    coupon = _coupon(meta={"ai_allocatable": False})
    assert is_native_ai_allocatable_coupon(
        coupon, tenant_id=33, resolved_level="bronze"
    ) is False


def test_explicit_true_with_safe_contract_is_allocatable():
    coupon = _coupon()
    assert is_native_ai_allocatable_coupon(
        coupon, tenant_id=33, resolved_level="bronze"
    ) is True


def test_wrong_level_channel_tenant_and_expiry_fail_closed():
    assert is_native_ai_allocatable_coupon(
        _coupon(), tenant_id=33, resolved_level="silver"
    ) is False
    assert is_native_ai_allocatable_coupon(
        _coupon(allocation_channel="campaign"),
        tenant_id=33,
        resolved_level="bronze",
    ) is False
    assert is_native_ai_allocatable_coupon(
        _coupon(), tenant_id=1, resolved_level="bronze"
    ) is False
    expired = _coupon(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert is_native_ai_allocatable_coupon(
        expired, tenant_id=33, resolved_level="bronze"
    ) is False


def test_bound_or_unlimited_is_not_one_customer_safe():
    bound = _coupon(meta={"customer_id": 22})
    assert is_native_ai_allocatable_coupon(
        bound, tenant_id=33, resolved_level="bronze"
    ) is False
    unlimited = _coupon(meta={"usage_limit": 0})
    assert is_native_ai_allocatable_coupon(
        unlimited, tenant_id=33, resolved_level="bronze"
    ) is False


def test_opt_in_validation_defaults_general_promo():
    assert validate_native_ai_opt_in(
        ai_allocatable=False,
        coupon_level=None,
        allocation_channel=None,
        usage_limit=0,
    ) is None
    assert validate_native_ai_opt_in(
        ai_allocatable=True,
        coupon_level=None,
        allocation_channel="ai",
        usage_limit=1,
    )
    assert validate_native_ai_opt_in(
        ai_allocatable=True,
        coupon_level="bronze",
        allocation_channel="ai",
        usage_limit=1,
    ) is None
