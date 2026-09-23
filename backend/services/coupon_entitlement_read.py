"""Which coupon levels a customer has actually earned — read only.

A valid coupon is not an entitled coupon. The merchant's records say a code
exists, is live and may be shared on this channel; they do not say that *this*
customer may have it. Level-conditioned codes are the merchant's loyalty
ladder, and handing every rung to every conversation turns a reward into a
public discount.

This module answers one question — *which of the merchant's coupon levels has
this customer reached?* — from the platform's own authorities, and answers
nothing else:

* :func:`services.customer_request_coupon_service.count_customer_orders` for
  the countable-order count (the Customer Intelligence phone index),
* :func:`services.coupon_level_contract.resolve_coupon_level_for_order_count`
  for the level that count resolves to, including the merchant's
  first-purchase rule,
* the merchant's own ``coupons_dashboard`` levels for which rungs are enabled
  and what each one requires.

It **reads**. It never issues, assigns, generates, reserves or redeems
anything, and it imports none of the issuance half. It sets no policy of its
own: every threshold, every ``enabled`` flag and the first-purchase rule come
from the merchant's saved configuration, and a rule the merchant did not turn
on is never turned on here.

**One level, and it is the contract's.** The contract resolves exactly one —
the highest enabled rung whose ``min_orders`` the customer has reached — and
that one is the answer here too. An earlier draft returned every rung the
customer had passed, on the reasoning that ``min_orders`` is a minimum; that
put a gold customer's bronze, silver and gold codes in front of the model at
once and so recreated the problem this module exists to solve, only smaller.
A customer has one standing with this merchant, the contract already says
which, and the issuance half acts on that same answer. Reading and issuing
must not disagree about who a customer is.

What stays with the agent is everything the contract does not decide: whether
to mention a coupon at all, when, and in what words.

**Not knowing is its own answer.** A customer whose identity was never
established in this conversation, one whose record cannot be found for this
tenant, one whose record exists but carries nothing the order index can be
searched by, and a known customer who simply has not bought yet are four
different states, and the reason code says which. Only the last is a
determination; the rest are failures to determine, and they entitle nothing —
while leaving untouched every coupon that was not conditioned on a level in the
first place.

The third is the one that hides. The order count comes back as a plain zero
whether the history was searched and found empty or never searched at all, and
a zero read as "new customer" is how a first-purchase welcome reaches someone
with years of orders. ``CustomerOrderCount.history_established`` is what tells
them apart, and this module refuses to resolve a level without it.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, Optional, Tuple

from services.coupon_level_contract import (
    CANONICAL_COUPON_LEVEL_IDS,
    REASON_FIRST_PURCHASE_AUTHORIZED,
    resolve_coupon_level_for_order_count,
)

logger = logging.getLogger(__name__)

# Closed set of reasons. The first three are determinations — the platform
# looked and this is what it found. The last three are failures to determine,
# and a failure to determine is never read as an entitlement.
REASON_ENTITLED = "entitled_by_order_count"
REASON_FIRST_PURCHASE = "first_purchase_rule_authorized"
REASON_NO_ENTITLED_LEVEL = "no_entitled_level"
REASON_IDENTITY_NOT_ESTABLISHED = "identity_not_established"
REASON_CUSTOMER_RECORD_UNAVAILABLE = "customer_record_unavailable"
REASON_ORDER_HISTORY_UNREADABLE = "order_history_unreadable"
# The record exists but carries nothing the order index can be searched by, so
# the history was never read. The platform's own counters would say "zero", and
# zero here would be a claim the platform cannot support.
REASON_ORDER_HISTORY_NOT_SEARCHABLE = "order_history_not_searchable"
REASON_LEVEL_POLICY_UNREADABLE = "level_policy_unreadable"

DETERMINED_REASONS: Tuple[str, ...] = (REASON_ENTITLED, REASON_FIRST_PURCHASE, REASON_NO_ENTITLED_LEVEL)


@dataclasses.dataclass(frozen=True)
class LevelEntitlement:
    """What this customer has earned, and how firmly it is known.

    ``determined`` is the honest bit: ``True`` only when the order history was
    actually read and the merchant's ladder actually applied. ``False`` means
    the question could not be answered, never that the answer was "nothing".
    """

    customer_id: Optional[int]
    countable_orders: Optional[int]          # None when it could not be established
    resolved_level: Optional[str]            # the contract's single answer
    entitled_levels: Tuple[str, ...]         # that answer, as a list; empty when there is none
    reason: str
    first_purchase_applied: bool = False
    # Everything the index returned for this customer, and how many of those
    # the countability policy set aside (cancelled, refunded, abandoned, …).
    # "Six orders, none of them countable" and "no orders at all" both resolve
    # to no rung, and a merchant asked why will want the difference.
    raw_orders: Optional[int] = None
    excluded_orders: Optional[int] = None

    @property
    def determined(self) -> bool:
        return self.reason in DETERMINED_REASONS

    def entitles(self, level: str) -> bool:
        """Whether a coupon requiring ``level`` may be put in front of this
        customer — true only for the one rung they actually stand on. An
        unreadable or unknown level entitles nothing."""
        wanted = str(level or "").strip().lower()
        return bool(wanted) and wanted in self.entitled_levels

    def as_dict(self) -> Dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "countable_orders": self.countable_orders,
            "resolved_level": self.resolved_level or "",
            "entitled_levels": list(self.entitled_levels),
            "reason": self.reason,
            "determined": self.determined,
            "first_purchase_applied": self.first_purchase_applied,
            "raw_orders": self.raw_orders,
            "excluded_orders": self.excluded_orders,
        }


def _opt_count(value: Any) -> Optional[int]:
    """A counter the source actually reported, or ``None`` when it did not."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _undetermined(customer_id: Optional[int], reason: str) -> LevelEntitlement:
    return LevelEntitlement(customer_id=customer_id, countable_orders=None, resolved_level=None,
                            entitled_levels=(), reason=reason)


def resolve_level_entitlement(db: Any, tenant_id: int, customer_id: Optional[int]) -> LevelEntitlement:
    """Read this customer's earned coupon levels for one tenant.

    Never raises: an unreadable history or an unreadable ladder is reported as
    the failure it is, entitling nothing, because a capability gate that cannot
    be read is a closed gate.
    """
    try:
        tid = int(tenant_id)
    except (TypeError, ValueError):
        return _undetermined(None, REASON_IDENTITY_NOT_ESTABLISHED)
    if tid <= 0:
        return _undetermined(None, REASON_IDENTITY_NOT_ESTABLISHED)
    if customer_id in (None, "", 0):
        # No customer was identified in this conversation. Nothing was looked
        # up and nothing is claimed: an anonymous conversation earns no rung.
        return _undetermined(None, REASON_IDENTITY_NOT_ESTABLISHED)
    try:
        cid = int(customer_id)
    except (TypeError, ValueError):
        return _undetermined(None, REASON_IDENTITY_NOT_ESTABLISHED)

    from services.customer_request_coupon_service import (  # noqa: PLC0415
        _first_purchase_rule_from_dashboard,
        count_customer_orders,
    )

    try:
        count = count_customer_orders(db, tid, cid)
    except Exception as exc:  # noqa: BLE001 - an unreadable history is said, never guessed as zero
        logger.info("[COUPON_ENTITLEMENT] tenant=%s outcome=%s error=%s",
                    tid, REASON_ORDER_HISTORY_UNREADABLE, type(exc).__name__)
        return _undetermined(cid, REASON_ORDER_HISTORY_UNREADABLE)
    if count is None:
        # The identifier is present but no customer of this tenant carries it.
        # That is not "a customer with no orders": nothing about this person's
        # history was established, so nothing about it may be acted on.
        return _undetermined(cid, REASON_CUSTOMER_RECORD_UNAVAILABLE)
    if not bool(getattr(count, "history_established", True)):
        # The record was found and its history was not read — no phone the
        # order index could be searched by. Its zero counters describe the
        # search, not the customer, and reading them as "no purchases" is how
        # a customer with years of orders is welcomed as a new one.
        logger.info("[COUPON_ENTITLEMENT] tenant=%s outcome=%s source=%s",
                    tid, REASON_ORDER_HISTORY_NOT_SEARCHABLE,
                    str(getattr(count, "count_source", "") or ""))
        return _undetermined(cid, REASON_ORDER_HISTORY_NOT_SEARCHABLE)

    try:
        from services.coupon_generator import _get_coupon_dashboard_block  # noqa: PLC0415

        block = _get_coupon_dashboard_block(db, tid)
    except Exception as exc:  # noqa: BLE001 - an unreadable ladder is a closed ladder
        logger.info("[COUPON_ENTITLEMENT] tenant=%s outcome=%s error=%s",
                    tid, REASON_LEVEL_POLICY_UNREADABLE, type(exc).__name__)
        return _undetermined(cid, REASON_LEVEL_POLICY_UNREADABLE)

    countable = int(getattr(count, "countable_orders", 0) or 0)
    seen = _opt_count(getattr(count, "raw_orders", None))
    set_aside = _opt_count(getattr(count, "excluded_orders", None))
    levels = block.get("levels")
    # The merchant's own first-purchase rule, exactly as saved. It is read, not
    # enabled: a merchant who left it off gets a customer with no purchases
    # resolving to no level, which is the honest answer for that store.
    first_purchase = _first_purchase_rule_from_dashboard(block)
    resolution = resolve_coupon_level_for_order_count(levels, countable, first_purchase_rule=first_purchase)

    if resolution.level_id is None:
        # The contract is the authority on whether this count reaches any rung
        # at all, first-purchase rule included. When it says none, none is the
        # answer here too — the set below is never allowed to disagree with it.
        return LevelEntitlement(customer_id=cid, countable_orders=countable, resolved_level=None,
                                entitled_levels=(), reason=REASON_NO_ENTITLED_LEVEL,
                                raw_orders=seen, excluded_orders=set_aside)

    if resolution.resolution_reason == REASON_FIRST_PURCHASE_AUTHORIZED:
        # The merchant chose to welcome a first purchase. That authorizes the
        # rung the contract names and no other, whatever the thresholds above
        # it: a welcome is not a promotion to the top of the ladder.
        return LevelEntitlement(customer_id=cid, countable_orders=countable,
                                resolved_level=resolution.level_id,
                                entitled_levels=(resolution.level_id,),
                                reason=REASON_FIRST_PURCHASE, first_purchase_applied=True,
                                raw_orders=seen, excluded_orders=set_aside)

    # The contract's one answer, and nothing beside it. A rung the customer has
    # passed but outgrown is not theirs to be offered: they have a standing,
    # not a range.
    return LevelEntitlement(customer_id=cid, countable_orders=countable,
                            resolved_level=resolution.level_id,
                            entitled_levels=(resolution.level_id,),
                            reason=REASON_ENTITLED, raw_orders=seen, excluded_orders=set_aside)


__all__ = [
    "CANONICAL_COUPON_LEVEL_IDS", "DETERMINED_REASONS", "LevelEntitlement", "REASON_CUSTOMER_RECORD_UNAVAILABLE", "REASON_ENTITLED",
    "REASON_FIRST_PURCHASE", "REASON_IDENTITY_NOT_ESTABLISHED", "REASON_LEVEL_POLICY_UNREADABLE",
    "REASON_NO_ENTITLED_LEVEL", "REASON_ORDER_HISTORY_NOT_SEARCHABLE",
    "REASON_ORDER_HISTORY_UNREADABLE", "resolve_level_entitlement",
]
