"""Order-count coupon level resolver — numeric min_orders only."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from backend.routers.coupons import DEFAULT_COUPON_LEVELS, _normalise_levels
from backend.services.coupon_level_contract import (
    CANONICAL_LEVEL_MIN_ORDERS,
    min_orders_for_level,
    resolve_coupon_level_for_order_count,
)
from backend.services.coupon_generator import _segment_to_level


def _levels(**overrides):
    payload = _normalise_levels(None)
    by_id = {row["id"]: dict(row) for row in payload}
    for lid, patch in overrides.items():
        by_id[lid].update(patch)
    return list(by_id.values())


def test_canonical_min_orders_matrix() -> None:
    assert CANONICAL_LEVEL_MIN_ORDERS == {
        "bronze": 1,
        "silver": 3,
        "gold": 7,
        "vip": 15,
    }


def test_saved_config_without_min_orders_backfills() -> None:
    assert min_orders_for_level("gold", {"id": "gold", "threshold": "+7 طلبات"}) == 7
    assert min_orders_for_level("silver", {}) == 3


def test_threshold_arabic_text_is_not_parsed() -> None:
    # Presentation text must not become the runtime threshold.
    raw = {"id": "silver", "threshold": "+99 طلبات"}
    assert min_orders_for_level("silver", raw) == 3
    resolved = resolve_coupon_level_for_order_count([raw], 3)
    assert resolved.level_id == "silver"
    assert resolved.min_orders == 3


def test_default_order_count_matrix() -> None:
    levels = DEFAULT_COUPON_LEVELS
    expected = {
        0: None,
        1: "bronze",
        2: "bronze",
        3: "silver",
        6: "silver",
        7: "gold",
        14: "gold",
        15: "vip",
        16: "vip",
    }
    for count, level_id in expected.items():
        got = resolve_coupon_level_for_order_count(levels, count, first_purchase_rule=False)
        assert got.level_id == level_id, (count, got.level_id, level_id)


def test_custom_merchant_thresholds() -> None:
    levels = _levels(
        bronze={"min_orders": 2},
        silver={"min_orders": 5},
        gold={"min_orders": 10},
        vip={"min_orders": 20},
    )
    assert resolve_coupon_level_for_order_count(levels, 1).level_id is None
    assert resolve_coupon_level_for_order_count(levels, 2).level_id == "bronze"
    assert resolve_coupon_level_for_order_count(levels, 4).level_id == "bronze"
    assert resolve_coupon_level_for_order_count(levels, 5).level_id == "silver"
    assert resolve_coupon_level_for_order_count(levels, 9).level_id == "silver"
    assert resolve_coupon_level_for_order_count(levels, 10).level_id == "gold"
    assert resolve_coupon_level_for_order_count(levels, 19).level_id == "gold"
    assert resolve_coupon_level_for_order_count(levels, 20).level_id == "vip"


def test_disabled_gold_skips_to_highest_enabled() -> None:
    levels = _levels(gold={"enabled": False})
    got = resolve_coupon_level_for_order_count(levels, 7)
    assert got.level_id == "silver"


def test_zero_orders_without_first_purchase_is_no_level() -> None:
    got = resolve_coupon_level_for_order_count(
        DEFAULT_COUPON_LEVELS, 0, first_purchase_rule={"enabled": False}
    )
    assert got.level_id is None
    assert got.resolution_reason == "no_level"


def test_zero_orders_with_first_purchase_authorizes_bronze() -> None:
    got = resolve_coupon_level_for_order_count(
        DEFAULT_COUPON_LEVELS, 0, first_purchase_rule={"enabled": True, "id": "first_purchase"}
    )
    assert got.level_id == "bronze"
    assert got.resolution_reason == "first_purchase_authorized"


def test_crm_status_ladder_is_not_the_coupon_level() -> None:
    """7 countable orders resolve gold; CRM active still maps to silver."""
    assert _segment_to_level("active") == "silver"
    assert resolve_coupon_level_for_order_count(DEFAULT_COUPON_LEVELS, 7).level_id == "gold"
    assert _segment_to_level("vip") == "gold"
    assert resolve_coupon_level_for_order_count(DEFAULT_COUPON_LEVELS, 15).level_id == "vip"
