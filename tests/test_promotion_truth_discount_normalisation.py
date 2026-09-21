"""One reading of a coupon's discount: the type decides, the value is a number.

Tenant 1, September 2026: a coupon issued as 5% and reconciled from Salla
carried ``discount_type="percentage"`` beside ``discount_value="{'amount': 5,
'currency': 'SAR'}"``; handed both, the model told the customer "5 SAR". The
resolver's facts now carry ``discount_value`` as the number the record
supports and ``discount`` as its one reading ("5%" / "20 SAR"), or nothing
when nothing is readable. Offline, with the same fake session the resolver's
other tests use.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce import promotion_truth as pt  # noqa: E402

MONEY_OBJECT_STRING = "{'amount': 5, 'currency': 'SAR'}"


class Row:
    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", 1)
        self.tenant_id = kwargs.get("tenant_id", 1)
        self.code = kwargs.get("code", "SAVE10")
        self.description = kwargs.get("description", "خصم")
        self.discount_type = kwargs.get("discount_type", "percentage")
        self.discount_value = kwargs.get("discount_value", "10")
        self.expires_at = kwargs.get("expires_at", datetime.now(timezone.utc) + timedelta(days=7))
        self.source_type = kwargs.get("source_type", "manual")
        self.allocation_channel = kwargs.get("allocation_channel", "")
        self.coupon_level = kwargs.get("coupon_level")
        self.extra_metadata = kwargs.get("extra_metadata") or {}
        self.rules: List[Any] = []
        self.starts_at = None


def session(coupons: List[Row]) -> MagicMock:
    db = MagicMock()

    def query(model: Any) -> MagicMock:
        q = MagicMock()
        for chain in ("filter", "order_by", "limit", "join"):
            getattr(q, chain).return_value = q
        name = str(getattr(model, "__name__", "") or model)
        q.all.return_value = [] if ("CouponRule" in name or "Promotion" in name) else list(coupons)
        q.first.return_value = None
        return q

    db.query.side_effect = query
    return db


def fact_of(row: Row) -> dict:
    return pt.resolve_shareable_promotions(session([row]), 1).shareable[0]


# ── The observed record ──────────────────────────────────────────────────────


def test_a_percentage_reconciled_as_a_money_object_string_reads_as_a_percentage() -> None:
    fact = fact_of(Row(code="NHABCD", discount_type="percentage", discount_value=MONEY_OBJECT_STRING,
                       extra_metadata={"discount_pct": 5}))
    assert fact["discount"] == "5%"
    assert fact["discount_value"] == "5"
    assert fact["discount_type"] == "percentage"


def test_an_unreadable_percentage_falls_back_to_the_generator_s_discount_pct() -> None:
    fact = fact_of(Row(discount_type="percentage", discount_value="n/a", extra_metadata={"discount_pct": 15}))
    assert fact["discount"] == "15%" and fact["discount_value"] == "15"


# ── Generic merchants ────────────────────────────────────────────────────────


def test_a_fixed_amount_keeps_its_currency() -> None:
    fact = fact_of(Row(discount_type="fixed", discount_value="{'amount': 20, 'currency': 'SAR'}"))
    assert fact["discount"] == "20 SAR" and fact["discount_value"] == "20"
    plain = fact_of(Row(discount_type="fixed", discount_value="15"))
    assert plain["discount"] == "15" and plain["discount_value"] == "15"


def test_a_plain_percentage_reads_as_before() -> None:
    fact = fact_of(Row(discount_type="percentage", discount_value="10"))
    assert fact["discount"] == "10%" and fact["discount_value"] == "10"


def test_nothing_readable_is_an_empty_reading_never_a_guess() -> None:
    fact = fact_of(Row(discount_type="percentage", discount_value="", extra_metadata={}))
    assert fact["discount"] == "" and fact["discount_value"] == ""
    fact = fact_of(Row(discount_type="percentage", discount_value="soon", extra_metadata={"discount_pct": "x"}))
    assert fact["discount"] == "" and fact["discount_value"] == ""


def test_an_offer_s_terms_read_the_same_way() -> None:
    offer = MagicMock(id=3, name="خصم الموسم", description="", promotion_type="percentage",
                      discount_value="{'amount': 10, 'currency': 'SAR'}", ends_at=None, conditions={},
                      extra_metadata={})
    fact = pt._offer_to_fact(offer)
    assert fact["discount"] == "10%" and fact["discount_value"] == "10"


def test_numbers_are_rendered_plainly() -> None:
    assert pt._plain_number(5.0) == "5"
    assert pt._plain_number("5.00") == "5"
    assert pt._plain_number("12.50") == "12.5"
    assert pt._plain_number("1,250") == "1250"
    assert pt._plain_number("abc") == "" and pt._plain_number(None) == ""
    assert pt._discount_number({"amount": "7", "currency": "sar"}) == ("7", "SAR")
    assert pt._discount_number(True) == ("", "")
