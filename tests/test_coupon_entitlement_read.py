"""Which coupon levels a customer has earned — the read-only entitlement view.

A valid coupon is not an entitled coupon. What is proved here is that the
merchant's own ladder decides who reached which rung, that the platform's
existing contract stays the authority on the resolution itself, that a rule the
merchant left off is never turned on, and — the part that matters most — that
*not knowing* is never read as an entitlement. An unidentified conversation, a
customer record that cannot be found and a history that cannot be read are three
different failures, each named, and none of them earns a rung; a known customer
with no purchases is a different thing again, and it is a determination.

The issuance half of the coupon service is not reachable from here, and a
separate case proves it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services import coupon_entitlement_read as cer  # noqa: E402

TENANT = 7
CUSTOMER = 41


class Count:
    """What ``count_customer_orders`` returns: a countable-order total."""

    def __init__(self, countable: int, customer_id: int = CUSTOMER) -> None:
        self.customer_id = customer_id
        self.countable_orders = countable
        self.raw_orders = countable
        self.excluded_orders = 0


def ladder(**overrides: Any) -> List[Dict[str, Any]]:
    """The merchant's saved levels, canonical thresholds, all enabled."""
    levels = [{"id": "bronze", "enabled": True}, {"id": "silver", "enabled": True},
              {"id": "gold", "enabled": True}, {"id": "vip", "enabled": True}]
    for entry in levels:
        if entry["id"] in overrides:
            entry.update(overrides[entry["id"]])
    return levels


def resolve(monkeypatch: pytest.MonkeyPatch, *, count: Any = 0, block: Any = None,
            customer_id: Optional[int] = CUSTOMER, tenant_id: int = TENANT,
            count_raises: Optional[BaseException] = None,
            block_raises: Optional[BaseException] = None) -> cer.LevelEntitlement:
    """Run the read with the order count and the merchant's block as doubles.

    ``count`` may be an int (countable orders), ``None`` (no such customer) or
    a :class:`Count`.
    """
    import services.coupon_generator as generator
    import services.customer_request_coupon_service as crcs

    def read_count(db: Any, tid: int, cid: int) -> Any:
        if count_raises is not None:
            raise count_raises
        return Count(count) if isinstance(count, int) else count

    def read_block(db: Any, tid: int) -> Dict[str, Any]:
        if block_raises is not None:
            raise block_raises
        return dict(block if block is not None else {"levels": ladder()})

    monkeypatch.setattr(crcs, "count_customer_orders", read_count)
    monkeypatch.setattr(generator, "_get_coupon_dashboard_block", read_block)
    return cer.resolve_level_entitlement(object(), tenant_id, customer_id)


# ── The ladder the merchant saved ────────────────────────────────────────────


def test_a_customer_stands_on_one_rung_and_it_is_the_contract_s(monkeypatch) -> None:
    """Eight orders have met bronze's one, silver's three and gold's seven, and
    not vip's fifteen — so the contract resolves **gold**, and gold is the
    answer. Restated from a first draft that returned all three passed rungs on
    the reasoning that ``min_orders`` is a minimum: that handed a gold
    customer's bronze and silver codes to the model as well, which is the thing
    this module exists to stop. A customer has a standing, not a range, and the
    issuance half acts on this same single answer."""
    standing = resolve(monkeypatch, count=8)
    assert standing.entitled_levels == ("gold",)
    assert standing.resolved_level == "gold"
    assert standing.reason == cer.REASON_ENTITLED and standing.determined is True
    assert standing.countable_orders == 8
    assert standing.entitles("gold")
    assert not standing.entitles("bronze"), "a rung they have outgrown is not theirs to be offered"
    assert not standing.entitles("vip")


def test_a_rung_the_merchant_switched_off_is_not_reached_by_anyone(monkeypatch) -> None:
    """The merchant's ``enabled`` flag is policy, not decoration. Turning
    silver off does not promote a silver customer or demote a gold one."""
    standing = resolve(monkeypatch, count=8, block={"levels": ladder(silver={"enabled": False})})
    assert standing.entitled_levels == ("gold",)
    assert standing.resolved_level == "gold"


def test_a_merchant_who_raised_a_threshold_is_obeyed(monkeypatch) -> None:
    """Every number comes from the merchant's saved row; this module sets none
    of its own. Raising gold to twelve puts it out of an eight-order reach."""
    standing = resolve(monkeypatch, count=8, block={"levels": ladder(gold={"min_orders": 12})})
    assert standing.entitled_levels == ("silver",)
    assert standing.resolved_level == "silver"


def test_one_countable_order_reaches_the_first_rung_only(monkeypatch) -> None:
    standing = resolve(monkeypatch, count=1)
    assert standing.entitled_levels == ("bronze",) and standing.resolved_level == "bronze"
    assert standing.determined is True


# ── A customer with no purchases, and the merchant's own welcome ─────────────


def test_a_known_customer_with_no_purchases_earns_nothing_unless_the_merchant_said_so(monkeypatch) -> None:
    """This is a determination, not a failure: the history was read and it is
    empty. With no first-purchase rule saved, no rung is reached — and that is
    the honest answer for a store that did not ask for one."""
    standing = resolve(monkeypatch, count=0)
    assert standing.entitled_levels == () and standing.resolved_level is None
    assert standing.reason == cer.REASON_NO_ENTITLED_LEVEL
    assert standing.determined is True, "the platform looked; there was nothing there"
    assert standing.countable_orders == 0
    assert standing.first_purchase_applied is False


def test_the_merchants_first_purchase_rule_is_applied_when_the_merchant_enabled_it(monkeypatch) -> None:
    """«عدم وجود طلبات سابقة لا يعني أنه غير مؤهل تلقائيًا». When the merchant
    turned the welcome on, a customer with no purchases reaches the rung the
    contract names — and only that one. A welcome is not a promotion to the top
    of the ladder."""
    block = {"levels": ladder(), "rules": {"first_purchase": {"enabled": True}}}
    standing = resolve(monkeypatch, count=0, block=block)
    assert standing.entitled_levels == ("bronze",) and standing.resolved_level == "bronze"
    assert standing.reason == cer.REASON_FIRST_PURCHASE
    assert standing.first_purchase_applied is True and standing.determined is True
    assert not standing.entitles("silver"), "the welcome opens one rung, not the ladder"


def test_a_first_purchase_rule_the_merchant_left_off_is_never_turned_on(monkeypatch) -> None:
    """Read, not enabled. The rule is present and disabled, and stays that
    way: this module changes no merchant policy to make an answer nicer."""
    block = {"levels": ladder(), "rules": {"first_purchase": {"enabled": False}}}
    standing = resolve(monkeypatch, count=0, block=block)
    assert standing.entitled_levels == () and standing.first_purchase_applied is False
    assert standing.reason == cer.REASON_NO_ENTITLED_LEVEL


def test_a_first_purchase_welcome_on_a_rung_the_merchant_disabled_is_not_granted(monkeypatch) -> None:
    block = {"levels": ladder(bronze={"enabled": False}), "rules": {"first_purchase": {"enabled": True}}}
    standing = resolve(monkeypatch, count=0, block=block)
    assert standing.entitled_levels == () and standing.reason == cer.REASON_NO_ENTITLED_LEVEL


# ── Not knowing is never an entitlement ──────────────────────────────────────


def test_an_unidentified_conversation_earns_no_rung_and_says_which_failure_it_is(monkeypatch) -> None:
    standing = resolve(monkeypatch, count=9, customer_id=None)
    assert standing.entitled_levels == () and standing.resolved_level is None
    assert standing.reason == cer.REASON_IDENTITY_NOT_ESTABLISHED
    assert standing.determined is False, "nothing was read, so nothing was determined"
    assert standing.countable_orders is None
    assert standing.customer_id is None


def test_a_customer_record_this_tenant_does_not_carry_is_not_a_customer_with_no_orders(monkeypatch) -> None:
    """The owner's distinction, made executable: «التمييز بين عميل معروف بلا
    مشتريات وعميل تعذّر التحقق من هويته أو سجلّه». An identifier with no record
    behind it establishes nothing about this person's history — it is not an
    empty history."""
    standing = resolve(monkeypatch, count=None)
    assert standing.reason == cer.REASON_CUSTOMER_RECORD_UNAVAILABLE
    assert standing.determined is False and standing.countable_orders is None
    assert standing.customer_id == CUSTOMER

    known_and_empty = resolve(monkeypatch, count=0)
    assert known_and_empty.reason == cer.REASON_NO_ENTITLED_LEVEL
    assert known_and_empty.determined is True and known_and_empty.countable_orders == 0
    assert known_and_empty.reason != standing.reason, "two different states, two different answers"


def test_an_unreadable_history_is_reported_not_guessed_as_zero(monkeypatch) -> None:
    """A gate that cannot be read is a closed gate, and the failure never
    escapes: the caller gets an answer, not an exception."""
    standing = resolve(monkeypatch, count=5, count_raises=RuntimeError("index down"))
    assert standing.reason == cer.REASON_ORDER_HISTORY_UNREADABLE
    assert standing.determined is False and standing.entitled_levels == ()


def test_an_unreadable_ladder_entitles_nothing(monkeypatch) -> None:
    standing = resolve(monkeypatch, count=9, block_raises=RuntimeError("settings unreadable"))
    assert standing.reason == cer.REASON_LEVEL_POLICY_UNREADABLE
    assert standing.determined is False and standing.entitled_levels == ()


def test_a_tenant_that_is_not_a_tenant_reads_as_no_identity(monkeypatch) -> None:
    for bad in (0, -3, "not-a-number"):
        standing = resolve(monkeypatch, count=9, tenant_id=bad)
        assert standing.reason == cer.REASON_IDENTITY_NOT_ESTABLISHED
        assert standing.determined is False


def test_a_rung_nobody_named_is_never_entitled(monkeypatch) -> None:
    """``entitles`` answers about a level the caller read off a record. An
    empty or unknown one is not a rung, and is never granted by default."""
    standing = resolve(monkeypatch, count=20)
    assert standing.entitled_levels == ("vip",)
    assert not standing.entitles("") and not standing.entitles(None)
    assert not standing.entitles("platinum")
    assert standing.entitles("VIP"), "the record's casing is not the customer's problem"


def test_the_view_hands_out_a_plain_readable_answer(monkeypatch) -> None:
    """``as_dict`` is what travels to the model's observation: no objects, no
    surprises, and ``determined`` said out loud."""
    view = resolve(monkeypatch, count=4).as_dict()
    assert view == {"customer_id": CUSTOMER, "countable_orders": 4, "resolved_level": "silver",
                    "entitled_levels": ["silver"], "reason": cer.REASON_ENTITLED,
                    "determined": True, "first_purchase_applied": False}


# ── Read only, and provably so ───────────────────────────────────────────────


def _identifiers_of(path: Path) -> set:
    """Every name the module actually calls or imports — prose excluded.

    A docstring that says the word "redeem" is documentation; a call to
    something named redeem is the thing itself. This reads the tree, not the
    text, so the two are never confused.
    """
    import ast

    tree = ast.parse(path.read_text())
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
                names.add(alias.asname or "")
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.asname or "")
    return {name for name in names if name}


def test_the_module_reaches_no_issuance_path() -> None:
    """The owner authorized reuse «بالقراءة فقط». Nothing here creates,
    assigns, generates, reserves or redeems a coupon, and this reads what the
    module's code actually names rather than trusting the review that wrote it.
    """
    names = _identifiers_of(REPO_ROOT / "backend" / "services" / "coupon_entitlement_read.py")
    forbidden = {"CouponGeneratorService", "pick_coupon_for_level", "find_reusable_assigned_coupon",
                 "issue_customer_coupon", "generate_coupon", "create_coupon", "add", "commit",
                 "flush", "delete", "merge", "execute", "save"}
    assert not names & forbidden, f"issuance, not reading: {sorted(names & forbidden)}"
    # It reads through the platform's own authorities and nothing else.
    assert {"count_customer_orders", "resolve_coupon_level_for_order_count",
            "_get_coupon_dashboard_block", "_first_purchase_rule_from_dashboard"} <= names
