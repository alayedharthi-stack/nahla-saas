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
    BLOCK_CHANNEL_NOT_ALLOWED,
    BLOCK_LADDER_UNREADABLE,
    BLOCK_LEVEL_DISABLED,
    BLOCK_NOT_IN_AI_POLICY,
    CANONICAL_COUPON_LEVEL_IDS,
    CANONICAL_LEVEL_MIN_ORDERS,
    earned_level_block,
    highest_allowed_at_or_below,
    ladder_is_readable,
    level_entry,
    min_orders_for_level,
    policy_served_level,
    resolve_coupon_level_for_order_count,
    servable_levels,
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



def test_highest_allowed_walks_down_the_ladder_only() -> None:
    """Down to the best permitted rung, never up past what was earned."""
    assert highest_allowed_at_or_below("gold", ["bronze", "silver"]) == "silver"
    assert highest_allowed_at_or_below("gold", ["bronze"]) == "bronze"
    assert highest_allowed_at_or_below("vip", ["bronze", "gold"]) == "gold"
    # Permitted itself: nothing to walk.
    assert highest_allowed_at_or_below("gold", ["gold", "silver"]) == "gold"
    # A rung above the one earned is not an entitlement to hand out.
    assert highest_allowed_at_or_below("bronze", ["gold", "vip"]) is None
    assert highest_allowed_at_or_below("silver", ["gold"]) is None


def test_highest_allowed_refuses_what_is_not_a_rung() -> None:
    """Unknown, empty and absent inputs resolve nothing rather than guessing."""
    assert highest_allowed_at_or_below("", ["bronze"]) is None
    assert highest_allowed_at_or_below("platinum", ["bronze", "silver"]) is None
    assert highest_allowed_at_or_below("gold", []) is None
    assert highest_allowed_at_or_below("gold", ["platinum"]) is None
    # Case and padding come from stored merchant config, not from a caller.
    assert highest_allowed_at_or_below(" Gold ", [" Silver ", "BRONZE"]) == "silver"


def test_highest_allowed_covers_every_canonical_rung() -> None:
    """Each rung permitted on its own resolves to itself, top to bottom."""
    for rung in CANONICAL_COUPON_LEVEL_IDS:
        assert highest_allowed_at_or_below(rung, [rung]) == rung


# ── Which rung a store may serve ─────────────────────────────────────────────
#
# Two questions with deliberately different answers. Keeping the rung a
# customer earned needs no further act from the merchant, so an unconfigured
# rung is not a closed one. Handing out a *different* rung in its place does
# need one, so an unconfigured rung is not servable as a substitute. Every case
# below is about one of those two, or about what neither of them may assume.


def _open(level_id: str, **extra):
    """A rung the merchant genuinely configured and left open to the assistant."""
    return {"id": level_id, "enabled": True, "allowed_channels": ["ai", "campaign"],
            "discount_default": 10, **extra}


def test_a_rung_carrying_only_its_own_name_is_an_absence_not_a_permission() -> None:
    """The bare-entry rule, in both shapes a ladder is ever stored in.

    ``{"id": "silver"}`` is what a lookup answers for a rung nobody configured,
    and an empty mapping row is the same thing written differently. Reading
    either as "enabled, no channel restriction" would let a rung the merchant
    never set up stand in for one they closed.
    """
    closed_gold = {"id": "gold", "enabled": True, "allowed_channels": ["campaign"]}
    assert servable_levels([{"id": "silver"}, closed_gold],
                           channel="ai", policy_levels=["silver", "gold"]) == []
    assert policy_served_level([{"id": "silver"}, closed_gold], "gold",
                               channel="ai", policy_levels=["silver", "gold"]) is None
    # The mapping form of the same ladder, with an empty silver row.
    mapping = {"silver": {}, "gold": {"enabled": True, "allowed_channels": ["campaign"]}}
    assert servable_levels(mapping, channel="ai", policy_levels=["silver", "gold"]) == []
    assert policy_served_level(mapping, "gold", channel="ai",
                               policy_levels=["silver", "gold"]) is None


def test_a_genuinely_configured_lower_rung_is_served_in_both_shapes() -> None:
    """The same ladders with silver actually set up: the walk finds it."""
    closed_gold = {"id": "gold", "enabled": True, "allowed_channels": ["campaign"]}
    as_list = [_open("silver"), closed_gold]
    assert servable_levels(as_list, channel="ai", policy_levels=["silver", "gold"]) == ["silver"]
    assert policy_served_level(as_list, "gold", channel="ai",
                               policy_levels=["silver", "gold"]) == "silver"
    as_mapping = {"silver": {"enabled": True, "allowed_channels": ["ai"], "discount_default": 10},
                  "gold": {"enabled": True, "allowed_channels": ["campaign"]}}
    assert policy_served_level(as_mapping, "gold", channel="ai",
                               policy_levels=["silver", "gold"]) == "silver"


def test_permission_data_that_cannot_be_read_never_authorises_a_rung() -> None:
    """Malformed settings narrow what is servable. They never widen it, and
    they never raise — a caller must not be handed a rung by an error path."""
    closed_gold = {"id": "gold", "enabled": True, "allowed_channels": ["campaign"]}
    for broken in (7, "ai", {"ai": True}, ["ai", 7], [b"ai"]):
        ladder = [{"id": "silver", "enabled": True, "allowed_channels": broken}, closed_gold]
        assert servable_levels(ladder, channel="ai", policy_levels=["silver", "gold"]) == [], broken
        assert policy_served_level(ladder, "gold", channel="ai",
                                   policy_levels=["silver", "gold"]) is None, broken
    # A switch that is not a switch is not an open one either.
    for broken in ("yes", "false", 1, 0, {}, []):
        ladder = [{"id": "silver", "enabled": broken, "allowed_channels": ["ai"]}, closed_gold]
        assert servable_levels(ladder, channel="ai", policy_levels=["silver", "gold"]) == [], broken
    # And a ladder that is not a ladder resolves nothing rather than everything.
    for broken in (7, "levels", b"levels"):
        assert servable_levels(broken, channel="ai", policy_levels=[]) == [], broken
        assert policy_served_level(broken, "gold", channel="ai", policy_levels=[]) is None, broken


def test_an_empty_channel_list_stays_the_absence_of_a_restriction() -> None:
    """What the dashboard means by choosing no channel is unchanged: nothing
    saved is no restriction, which is not the same as nothing readable."""
    for empty in (None, [], (), ""):
        ladder = [{"id": "silver", "enabled": True, "allowed_channels": empty,
                   "discount_default": 10}]
        assert servable_levels(ladder, channel="ai", policy_levels=[]) == ["silver"], empty


def test_each_rungs_own_switch_and_channels_are_read_on_their_own_terms() -> None:
    """A closed rung lends nothing to the one served in its place."""
    ladder = [_open("bronze"), {**_open("silver"), "enabled": False},
              {**_open("gold"), "allowed_channels": ["campaign"]}, _open("vip")]
    # Silver disabled, gold closed to this channel: the walk steps over both.
    assert servable_levels(ladder, channel="ai", policy_levels=[]) == ["bronze", "vip"]
    assert policy_served_level(ladder, "gold", channel="ai", policy_levels=[]) == "bronze"
    # On the merchant's own channel gold is open, and nothing walks anywhere.
    assert policy_served_level(ladder, "gold", channel="campaign", policy_levels=[]) == "gold"


def test_only_the_assistant_walks_the_ladder_down() -> None:
    """A campaign or autopilot run addresses one rung deliberately. A rung the
    merchant closed there stays closed rather than becoming a lower one."""
    ladder = [_open("bronze"), _open("silver"),
              {**_open("gold"), "allowed_channels": ["ai"]}]
    assert policy_served_level(ladder, "gold", channel="campaign", policy_levels=[]) is None
    assert policy_served_level(ladder, "gold", channel="autopilot", policy_levels=[]) is None
    assert policy_served_level(ladder, "gold", channel="ai", policy_levels=[]) == "gold"


def test_the_rung_a_customer_earned_is_theirs_unless_the_merchant_closed_it() -> None:
    """A merchant who never opened the coupons dashboard has closed nothing.

    The earned rung is read on what was actually saved, so an absent ladder and
    an absent row leave it open. This is the half that must not be confused
    with the substitute rule above, and the issuance service reads it the same
    way — a customer is not refused their own standing by a row nobody wrote.
    """
    for absent in (None, [], {}):
        assert earned_level_block(absent, "gold", channel="ai", policy_levels=[]) is None
        assert policy_served_level(absent, "gold", channel="ai", policy_levels=[]) == "gold"
    # A ladder where only silver was configured leaves gold untouched.
    partial = [_open("silver")]
    assert earned_level_block(partial, "gold", channel="ai", policy_levels=[]) is None
    assert policy_served_level(partial, "gold", channel="ai", policy_levels=[]) == "gold"


def test_the_earned_rung_names_the_gate_that_closed_it() -> None:
    """Three gates, three answers, so a caller can say which one refused."""
    ladder = [{**_open("gold"), "enabled": False}]
    assert earned_level_block(ladder, "gold", channel="ai",
                              policy_levels=[]) == BLOCK_LEVEL_DISABLED
    ladder = [{**_open("gold"), "allowed_channels": ["campaign"]}]
    assert earned_level_block(ladder, "gold", channel="ai",
                              policy_levels=[]) == BLOCK_NOT_IN_AI_POLICY
    assert earned_level_block([{**_open("gold"), "allowed_channels": ["ai"]}], "gold",
                              channel="autopilot",
                              policy_levels=[]) == BLOCK_CHANNEL_NOT_ALLOWED
    assert earned_level_block([_open("gold")], "gold", channel="ai",
                              policy_levels=["bronze", "silver"]) == BLOCK_NOT_IN_AI_POLICY
    # An empty policy list is no restriction, not a restriction to nothing.
    assert earned_level_block([_open("gold")], "gold", channel="ai", policy_levels=[]) is None


def test_the_store_policy_list_only_ever_applies_to_the_assistant() -> None:
    """``allowed_levels`` is the AI policy. Campaign and autopilot are the
    merchant's own surfaces and are not filtered by it."""
    ladder = [_open("gold", allowed_channels=["ai", "campaign", "autopilot"])]
    assert earned_level_block(ladder, "gold", channel="campaign",
                              policy_levels=["bronze"]) is None
    assert earned_level_block(ladder, "gold", channel="ai",
                              policy_levels=["bronze"]) == BLOCK_NOT_IN_AI_POLICY


def test_level_entry_reads_both_ladder_shapes_and_invents_nothing() -> None:
    ladder = [_open("silver")]
    assert level_entry(ladder, "silver")["discount_default"] == 10
    assert level_entry(ladder, "gold") == {"id": "gold"}
    assert level_entry({"silver": {"discount_default": 12}}, "silver")["discount_default"] == 12
    assert level_entry(ladder, "platinum") == {}


def test_the_shipped_defaults_serve_a_gold_customer_the_best_rung_below_gold() -> None:
    """The incident, at the contract: gold and vip are campaign-only out of the
    box, so a gold customer is a silver customer as far as the assistant is
    concerned — not a customer with nothing."""
    shipped = _normalise_levels(DEFAULT_COUPON_LEVELS)
    assert policy_served_level(shipped, "gold", channel="ai", policy_levels=[]) == "silver"
    assert policy_served_level(shipped, "vip", channel="ai", policy_levels=[]) == "silver"
    assert policy_served_level(shipped, "silver", channel="ai", policy_levels=[]) == "silver"
    # And never above what was earned.
    assert policy_served_level(shipped, "bronze", channel="ai", policy_levels=[]) == "bronze"


def test_a_ladder_nobody_can_read_closes_the_earned_rung_too() -> None:
    """Unreadable is not unconfigured.

    An absent ladder says the merchant set no gate, and the rung a customer
    earned stays theirs. A settings row holding something that is not a ladder
    says nothing at all, and a rung is never served on the strength of a gate
    nobody could read.
    """
    for unreadable in (7, "levels", b"levels", 3.5, True):
        assert ladder_is_readable(unreadable) is False, unreadable
        assert earned_level_block(unreadable, "gold", channel="ai",
                                  policy_levels=[]) == BLOCK_LADDER_UNREADABLE, unreadable
        assert policy_served_level(unreadable, "gold", channel="ai",
                                   policy_levels=[]) is None, unreadable
    for readable in (None, [], {}, [_open("gold")], {"gold": {"enabled": True}}):
        assert ladder_is_readable(readable) is True, readable
