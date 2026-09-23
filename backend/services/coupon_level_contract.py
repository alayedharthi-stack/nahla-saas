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


def highest_allowed_at_or_below(level_id: str, allowed: Sequence[str]) -> Optional[str]:
    """The best rung a store will let the assistant hand out for this customer.

    A customer who earned gold in a store whose assistant may only offer bronze
    and silver is not a customer with nothing: they are a silver customer as far
    as this channel is concerned. This walks **down** the canonical ladder from
    what they earned and returns the first rung the store allows.

    It never walks up. A bronze customer in a store that allows only gold still
    gets nothing here — serving above what the order history earned would be
    inventing an entitlement, which is the opposite of the problem this solves.
    """
    key = str(level_id or "").strip().lower()
    if key not in CANONICAL_COUPON_LEVEL_IDS:
        return None
    permitted = {str(x).strip().lower() for x in (allowed or ())}
    ladder = CANONICAL_COUPON_LEVEL_IDS[:CANONICAL_COUPON_LEVEL_IDS.index(key) + 1]
    for candidate in reversed(ladder):
        if candidate in permitted:
            return candidate
    return None


def _canonical_level(level_id: Any) -> Optional[str]:
    key = str(level_id or "").strip().lower()
    return key if key in CANONICAL_COUPON_LEVEL_IDS else None


def level_entry(levels: Any, level_id: str) -> Dict[str, Any]:
    """This rung's saved row, or a bare ``{"id": ...}`` when the merchant never
    configured it. One reader for both ladder shapes — the list the dashboard
    writes and the mapping some callers hold — so the two halves of the
    platform cannot disagree about what a rung says.
    """
    key = _canonical_level(level_id)
    if key is None:
        return {}
    for entry in _as_level_list(levels):
        if str(entry.get("id") or "").strip().lower() == key:
            return dict(entry)
    return {"id": key}


def _readable_channels(raw: Any) -> Optional[List[str]]:
    """A rung's ``allowed_channels`` as channel names, or ``None`` when what
    was saved is not a list of names at all.

    ``None`` says the restriction could not be read, which is not the same as
    there being none. Reading an unreadable restriction as an absent one would
    let malformed data authorise a rung the merchant may have closed, and every
    gate here exists so that nothing is granted by default. Empty stays empty:
    a rung with no channels saved is the dashboard's own way of placing no
    restriction, and that reading is unchanged.
    """
    if not raw:
        return []
    if isinstance(raw, (str, bytes)) or isinstance(raw, Mapping):
        return None
    if not isinstance(raw, Sequence):
        return None
    names: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        name = item.strip().lower()
        if name:
            names.append(name)
    return names


def _switch_is_on(entry: Mapping[str, Any]) -> Optional[bool]:
    """A rung's ``enabled`` switch, or ``None`` when what was saved is not one.

    Absent is on: the dashboard writes a real boolean for every rung it saves,
    and the callers' own accessors omit it only on a row they synthesised. A
    value that is neither absent nor a boolean is a switch nobody can read, and
    an unreadable switch is never read as an open one.
    """
    if "enabled" not in entry:
        return True
    value = entry.get("enabled")
    return value if isinstance(value, bool) else None


def _is_configured(entry: Mapping[str, Any]) -> bool:
    """Whether this row says anything beyond naming its own rung.

    ``level_entry`` and ``_as_level_list`` both answer with a bare ``{"id": …}``
    for a rung the merchant never configured. That is an absence, not a rung
    carrying default permissions.
    """
    return bool(set(entry) - {"id"})


def _channel_permits(entry: Mapping[str, Any], channel: str) -> bool:
    """Whether this rung's own channel list lets ``channel`` have it. An
    unreadable list permits nothing."""
    channels = _readable_channels(entry.get("allowed_channels"))
    if channels is None:
        return False
    return not channels or channel in channels


BLOCK_LEVEL_DISABLED = "level_disabled"
BLOCK_CHANNEL_NOT_ALLOWED = "channel_not_allowed"
BLOCK_NOT_IN_AI_POLICY = "level_not_allowed_for_ai"
# The saved ladder is not a ladder at all, so no gate on it can be read. Not a
# gate the merchant set — a fact about the settings — and kept apart from the
# three because it is the one that says "we do not know" rather than "no".
BLOCK_LADDER_UNREADABLE = "level_policy_unreadable"


def ladder_is_readable(levels: Any) -> bool:
    """Whether ``levels`` is a ladder at all.

    The list the dashboard saves, the mapping some callers hold, and nothing
    saved yet are all readable: a merchant who never opened the coupon settings
    has configured no rung, which is a complete answer. A scalar or a string is
    a settings row nobody can read, and unreadable is not unconfigured — the
    first leaves the merchant's gates unknown, the second says they set none.
    """
    return levels is None or isinstance(levels, Mapping) or (
        isinstance(levels, Sequence) and not isinstance(levels, (str, bytes)))


def unreadable_rungs(levels: Any) -> List[str]:
    """Rungs saved on this ladder whose permission fields cannot be read.

    ``servable_levels`` already refuses them, which is the safe answer. This
    names them so a caller can say the list it is returning was shortened by
    settings nobody could read, rather than report the result as the store's
    own answer. A rung the merchant never configured is not here: an absence is
    readable, and it says no.
    """
    if not ladder_is_readable(levels):
        return list(CANONICAL_COUPON_LEVEL_IDS)
    broken: List[str] = []
    for entry in _as_level_list(levels):
        candidate = _canonical_level(entry.get("id"))
        if candidate is None or not _is_configured(entry):
            continue
        if _switch_is_on(entry) is None or _readable_channels(
                entry.get("allowed_channels")) is None:
            broken.append(candidate)
    return [lid for lid in CANONICAL_COUPON_LEVEL_IDS if lid in set(broken)]


def earned_level_block(levels: Any, earned_level: Any, *, channel: str,
                       policy_levels: Sequence[str]) -> Optional[str]:
    """Why the rung the order history earned cannot be served on ``channel``,
    or ``None`` when it can.

    The rung a customer stands on is theirs unless the merchant closed it, so
    it is read on the settings the merchant actually saved and an unconfigured
    rung is not a closed one. That is the opposite of the rule for a
    *substitute* rung in ``servable_levels``, and deliberately so: keeping what
    was earned needs no further act from the merchant, while handing out a
    different rung in its place does.

    A ladder that cannot be read at all is the one case where neither reading
    applies, and it closes the rung: settings nobody can read are not settings
    that left it open.
    """
    key = _canonical_level(earned_level)
    if key is None:
        return None
    if not ladder_is_readable(levels):
        return BLOCK_LADDER_UNREADABLE
    wanted_channel = str(channel or "").strip().lower()
    entry = level_entry(levels, key)
    if _switch_is_on(entry) is not True:
        return BLOCK_LEVEL_DISABLED
    if not _channel_permits(entry, wanted_channel):
        return (BLOCK_CHANNEL_NOT_ALLOWED if wanted_channel != "ai"
                else BLOCK_NOT_IN_AI_POLICY)
    permitted = {str(x).strip().lower() for x in (policy_levels or ())}
    if wanted_channel == "ai" and permitted and key not in permitted:
        return BLOCK_NOT_IN_AI_POLICY
    return None


def servable_levels(levels: Any, *, channel: str, policy_levels: Sequence[str]) -> List[str]:
    """Every rung this merchant lets this channel hand out **in place of
    another**, in ladder order.

    Each candidate is read on its own terms — its own ``enabled`` switch, its
    own ``allowed_channels``, and for the assistant the store's AI policy list.
    Nothing is inherited from the rung a customer's order history resolved,
    because a rung the merchant closed must not lend its permissions to the one
    served in its place.

    A rung the merchant never configured is an absence, not a rung with default
    permissions, so it is not servable here. Permission data that cannot be
    read is not absent permission data either, and never widens this list. An
    empty policy list is no restriction rather than a restriction to nothing:
    that is what the dashboard means by leaving it unset, and it matches the
    gate the callers already apply.
    """
    permitted = {str(x).strip().lower() for x in (policy_levels or ())}
    wanted_channel = str(channel or "").strip().lower()
    out: List[str] = []
    for entry in _as_level_list(levels):
        candidate = _canonical_level(entry.get("id"))
        if candidate is None or not _is_configured(entry):
            continue
        if _switch_is_on(entry) is not True:
            continue
        if not _channel_permits(entry, wanted_channel):
            continue
        if wanted_channel == "ai" and permitted and candidate not in permitted:
            continue
        out.append(candidate)
    return [lid for lid in CANONICAL_COUPON_LEVEL_IDS if lid in set(out)]


def policy_served_level(levels: Any, earned_level: Any, *, channel: str,
                        policy_levels: Sequence[str]) -> Optional[str]:
    """The one rung to serve a customer standing on ``earned_level``.

    The single selection both halves of the platform use, so reading and
    issuing can never disagree about which rung a customer is served: what they
    earned when the merchant left it open, and otherwise — for the assistant
    only — the highest rung the merchant configured and permits **below** it.

    Never above it. A standing that could not be determined is not a standing,
    and serves nothing — a failure to determine must never become a lower tier
    handed out by default. Total by construction: malformed settings narrow
    what is servable and never raise, so a caller cannot be handed a rung by an
    error path.
    """
    key = _canonical_level(earned_level)
    if key is None:
        return None
    if earned_level_block(levels, key, channel=channel,
                          policy_levels=policy_levels) is None:
        return key
    # Only the assistant walks the ladder down. A campaign or autopilot run is
    # the merchant addressing one rung deliberately, so a rung they closed
    # there stays closed rather than quietly becoming a lower one.
    if str(channel or "").strip().lower() != "ai":
        return None
    # Strictly below: the earned rung was just refused, and a candidate read
    # that disagreed with that refusal would serve the very rung the merchant
    # closed.
    below = [lid for lid in servable_levels(levels, channel=channel,
                                            policy_levels=policy_levels)
             if lid != key]
    return highest_allowed_at_or_below(key, below)


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
