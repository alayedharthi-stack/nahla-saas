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


# ── The welcome is the merchant's, not the rung's ────────────────────────────


def _welcome(**overrides):
    """A first-purchase rule as the dashboard persists it: all five fields."""
    rule = {"id": "first_purchase", "enabled": True, "discount_type": "percentage",
            "discount_value": 15, "validity_days": 1, "min_order_amount": 100, "max_uses": 1}
    rule.update(overrides)
    return rule


def _bronze_unlike_the_welcome():
    return _levels(bronze={"enabled": True, "discount_default": 5,
                           "validity_hours": 720, "max_uses": 50})


def test_the_welcome_runs_on_the_settings_the_merchant_saved() -> None:
    """«يجب أن تُحترم إعداداته الفعلية». A merchant who configures a 15% welcome
    valid for one day, usable once, over 100 — gets exactly that. Before this,
    all four were silently replaced by whatever the bronze rung said, so the
    merchant could switch the welcome on and could not say what it was."""
    got = resolve_coupon_level_for_order_count(
        _bronze_unlike_the_welcome(), 0, first_purchase_rule=_welcome())
    assert got.level_id == "bronze"
    assert got.resolution_reason == "first_purchase_authorized"
    assert got.discount_default == 15.0          # not the rung's 5
    assert got.validity_hours == 24              # not the rung's 720
    assert got.max_uses == 1                     # not the rung's 50
    assert got.min_order_amount == 100.0
    assert got.discount_type == "percentage"
    assert got.economics_source == "first_purchase_rule"


def test_a_field_the_merchant_left_unset_keeps_the_rungs_value() -> None:
    """Overlay, not replacement: the rule decides what it carries and nothing
    else. A welcome with only a discount does not thereby expire like bronze
    never would, or lose the rung's usage cap."""
    got = resolve_coupon_level_for_order_count(
        _bronze_unlike_the_welcome(), 0,
        first_purchase_rule={"id": "first_purchase", "enabled": True, "discount_value": 15})
    assert got.discount_default == 15.0
    assert got.validity_hours == 720 and got.max_uses == 50
    assert got.economics_source == "first_purchase_rule"


def test_a_welcome_switched_on_and_never_configured_behaves_as_before() -> None:
    """An older store whose rule is a bare flag configured nothing, so there is
    nothing to honour and the rung stands. No store changes behaviour for
    having been saved before the rule carried economics."""
    for bare in (True, {"enabled": True}, {"id": "first_purchase", "enabled": True}):
        got = resolve_coupon_level_for_order_count(_bronze_unlike_the_welcome(), 0,
                                                   first_purchase_rule=bare)
        assert got.level_id == "bronze"
        assert (got.discount_default, got.validity_hours, got.max_uses) == (5.0, 720, 50)
        assert got.economics_source == "level"
        assert got.min_order_amount is None


def test_the_welcome_never_reshapes_a_rung_the_customer_earned() -> None:
    """The rule is a welcome for a first purchase. A customer with four orders
    stands on silver by their history, and the welcome's numbers have no
    business there — whatever the merchant configured on it."""
    for count in (1, 4, 8, 20):
        got = resolve_coupon_level_for_order_count(
            _bronze_unlike_the_welcome(), count, first_purchase_rule=_welcome())
        assert got.resolution_reason == "highest_enabled_min_orders"
        assert got.economics_source == "level"
        assert got.min_order_amount is None and got.discount_type is None


def test_a_disabled_welcome_configures_nothing_at_all() -> None:
    got = resolve_coupon_level_for_order_count(
        _bronze_unlike_the_welcome(), 0, first_purchase_rule=_welcome(enabled=False))
    assert got.level_id is None and got.resolution_reason == "no_level"
    assert got.economics_source == "level"


def test_a_welcome_saved_with_no_validity_still_expires() -> None:
    """Zero days is a saved value, not a coupon that lives forever. The floor is
    one day, because a welcome nobody can use is not what the merchant meant."""
    got = resolve_coupon_level_for_order_count(
        _bronze_unlike_the_welcome(), 0, first_purchase_rule=_welcome(validity_days=0))
    assert got.validity_hours == 24


def test_the_resolution_says_out_loud_whose_numbers_it_carries() -> None:
    """A reader must never have to guess whether it is looking at the welcome or
    at bronze — `as_dict` is what travels to the issuance half and the logs."""
    welcome = resolve_coupon_level_for_order_count(
        _bronze_unlike_the_welcome(), 0, first_purchase_rule=_welcome()).as_dict()
    earned = resolve_coupon_level_for_order_count(_bronze_unlike_the_welcome(), 4).as_dict()
    assert welcome["economics_source"] == "first_purchase_rule"
    assert earned["economics_source"] == "level"
    assert welcome["min_order_amount"] == 100.0 and earned["min_order_amount"] is None


def test_a_generic_store_gets_its_own_welcome_whatever_it_sells() -> None:
    """Platform-wide: the welcome a clothing store configures is the clothing
    store's, and a perfume store's is its own. Nothing here is per-merchant."""
    for value, days in ((25, 3), (10, 7), (5, 1)):
        got = resolve_coupon_level_for_order_count(
            _bronze_unlike_the_welcome(), 0,
            first_purchase_rule=_welcome(discount_value=value, validity_days=days))
        assert got.discount_default == float(value)
        assert got.validity_hours == days * 24
