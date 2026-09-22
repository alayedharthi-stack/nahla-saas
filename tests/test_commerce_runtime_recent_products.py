"""What an earlier reply showed is carried into the next turn — bounded.

A follow-up question about something already shown needs an identity, not a
phrase. The platform already persists the evidence each reply rested on; this
reads it back, re-checks it against the merchant's catalogue, and stops
carrying a product once the reply that last showed **it** is old enough —
however busy the conversation has been about other things since.

Offline: the stored rows and the catalogue are supplied directly, so the
bounds, the lapse and the parsing are under test without a database. The
PostgreSQL proofs in ``tests/commerce_reliability`` run the same code against
real rows. Generic merchants throughout.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.commerce_runtime import recent_products as rp  # noqa: E402

NOW = datetime(2026, 9, 22, 10, 0, 0)


class Row:
    def __init__(self, refs: Any, *, hours_ago: float = 1.0) -> None:
        self.created_at = NOW - timedelta(hours=hours_ago)
        self.extra_metadata = {"evidence_refs": refs} if refs is not None else None


CATALOGUE = {
    11: {"id": 11, "title": "قميص قطني أزرق", "price": "129.0", "sale_price": "",
         "currency": "SAR", "in_stock": True},
    12: {"id": 12, "title": "عطر ورد 100ml", "price": "240.0", "sale_price": "199.0",
         "currency": "SAR", "in_stock": False},
    13: {"id": 13, "title": "حذاء رياضي أبيض", "price": "310.0", "sale_price": None,
         "currency": "SAR", "in_stock": None},
}


def shown(monkeypatch: Any, rows: list, *, catalogue: dict | None = None,
          now: datetime = NOW, lapse: int = rp.BROWSING_CONTEXT_LAPSE_SECONDS) -> Any:
    monkeypatch.setattr(rp, "_recent_outbound_rows",
                        lambda db, **kwargs: list(rows)[:rp.MAX_REPLIES_READ])
    table = CATALOGUE if catalogue is None else catalogue

    def catalog(db: Any, *, tenant_id: int, product_ids: Any) -> list:
        out = []
        for pid in product_ids:
            row = table.get(int(pid))
            if row is None:
                continue
            out.append(rp.ShownProduct(
                product_id=int(pid), title=str(row["title"]),
                price=row.get("price") or None, sale_price=row.get("sale_price") or None,
                currency=row.get("currency") or None, in_stock=row.get("in_stock")))
        return out

    monkeypatch.setattr(rp, "_still_in_catalog", catalog)
    return rp.products_shown_earlier(object(), tenant_id=7, conversation_id=3,
                                     now=now, lapse_seconds=lapse)


def test_the_products_an_earlier_reply_cited_are_carried_with_the_merchants_values(monkeypatch) -> None:
    result = shown(monkeypatch, [Row(["catalog:product:11", "catalog:product:12"])])
    assert result.reason == rp.CARRIED
    assert result.product_ids == [11, 12]
    assert result.as_facts() == [
        {"product_id": 11, "title": "قميص قطني أزرق", "price": "129.0",
         "currency": "SAR", "in_stock": True},
        {"product_id": 12, "title": "عطر ورد 100ml", "price": "240.0",
         "sale_price": "199.0", "currency": "SAR", "in_stock": False},
    ]


def test_a_product_the_merchant_no_longer_lists_is_not_an_identity_this_turn_may_read(monkeypatch) -> None:
    result = shown(monkeypatch, [Row(["catalog:product:11", "catalog:product:999"])])
    assert result.product_ids == [11]


def test_references_that_are_not_catalogue_products_are_left_alone(monkeypatch) -> None:
    result = shown(monkeypatch, [Row(["order:summary:4", "promotion:coupon:9",
                                      "catalog:product:", "catalog:product:abc",
                                      "catalog:product:-3", "catalog:product:13"])])
    assert result.product_ids == [13]


def test_a_reply_that_grounded_on_nothing_says_so_rather_than_guessing(monkeypatch) -> None:
    for refs in ([], None, "", ["order:summary:4"]):
        result = shown(monkeypatch, [Row(refs)])
        assert result.reason == rp.NO_PRODUCTS_CITED and result.products == ()


def test_a_comma_joined_reference_string_is_read_the_same_way(monkeypatch) -> None:
    """Some rows store the references joined rather than as a list."""
    result = shown(monkeypatch, [Row("catalog:product:11,catalog:product:13")])
    assert result.product_ids == [11, 13]


def test_the_newest_reply_comes_first_and_a_repeat_is_not_carried_twice(monkeypatch) -> None:
    result = shown(monkeypatch, [
        Row(["catalog:product:13"], hours_ago=1),
        Row(["catalog:product:11", "catalog:product:13"], hours_ago=2),
    ])
    assert result.product_ids == [13, 11]


def test_only_a_bounded_number_of_products_is_carried(monkeypatch) -> None:
    table = {i: {"id": i, "title": f"منتج {i}", "price": "10", "sale_price": "",
                 "currency": "SAR", "in_stock": True} for i in range(1, 40)}
    rows = [Row([f"catalog:product:{i}" for i in range(n * 4 + 1, n * 4 + 5)], hours_ago=n + 1)
            for n in range(8)]
    result = shown(monkeypatch, rows, catalogue=table)
    assert len(result.product_ids) == rp.MAX_PRODUCTS
    assert result.product_ids[0] == 1


def test_a_product_shown_longer_ago_than_the_lapse_is_not_carried(monkeypatch) -> None:
    hours = rp.BROWSING_CONTEXT_LAPSE_SECONDS / 3600
    lapsed = shown(monkeypatch, [Row(["catalog:product:11"], hours_ago=hours + 1)])
    assert lapsed.reason == rp.LAPSED and lapsed.products == ()
    assert lapsed.seconds_since_last_product_shown == int((hours + 1) * 3600)

    just_inside = shown(monkeypatch, [Row(["catalog:product:11"], hours_ago=hours - 0.5)])
    assert just_inside.reason == rp.CARRIED


def test_talking_every_day_about_other_things_does_not_keep_an_old_product_alive(monkeypatch) -> None:
    """The clock belongs to the product, not to the conversation.

    The customer was shown a shirt a week ago and has chatted daily since —
    about delivery, about a coupon, about nothing in particular. None of those
    replies showed the shirt, so none of them makes it current again.
    """
    rows = [Row(["promotion:coupon:9"], hours_ago=1),
            Row([], hours_ago=25),
            Row(["order:summary:4"], hours_ago=49),
            Row(["catalog:product:11"], hours_ago=24 * 7)]
    result = shown(monkeypatch, rows)
    assert result.reason == rp.LAPSED and result.products == ()
    assert result.seconds_since_last_product_shown == 24 * 7 * 3600


def test_a_product_still_being_discussed_stays_current_on_its_own(monkeypatch) -> None:
    """No intent detection: the reply that discusses it cites it, and that
    citation is what refreshes it."""
    rows = [Row(["catalog:product:11"], hours_ago=2),
            Row(["catalog:product:11", "catalog:product:12"], hours_ago=24 * 7)]
    result = shown(monkeypatch, rows)
    assert result.reason == rp.CARRIED
    assert result.product_ids == [11]  # 12 was only ever shown a week ago


def test_each_product_is_judged_on_its_own_age_not_the_newest_reply(monkeypatch) -> None:
    rows = [Row(["catalog:product:13"], hours_ago=1),
            Row(["catalog:product:11"], hours_ago=24 * 7)]
    result = shown(monkeypatch, rows)
    assert result.product_ids == [13]
    assert result.seconds_since_last_product_shown == 3600


def test_the_lapse_is_a_parameter_so_the_policy_is_one_number(monkeypatch) -> None:
    rows = [Row(["catalog:product:11"], hours_ago=30)]
    assert shown(monkeypatch, rows, lapse=24 * 3600).reason == rp.LAPSED
    assert shown(monkeypatch, rows, lapse=72 * 3600).reason == rp.CARRIED


def test_a_conversation_with_no_reply_yet_is_not_a_lapse(monkeypatch) -> None:
    result = shown(monkeypatch, [])
    assert result.reason == rp.NO_EARLIER_REPLY and result.products == ()


def test_a_history_that_cannot_be_read_leaves_the_turn_running_without_the_aid(monkeypatch) -> None:
    def boom(db: Any, **kwargs: Any) -> Any:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(rp, "_recent_outbound_rows", boom)
    result = rp.products_shown_earlier(object(), tenant_id=7, conversation_id=3, now=NOW)
    assert result.reason == rp.UNAVAILABLE and result.products == ()
    assert not result


def test_an_unreadable_reply_timestamp_does_not_silently_lapse_the_context(monkeypatch) -> None:
    row = Row(["catalog:product:11"])
    row.created_at = None  # type: ignore[assignment]
    result = shown(monkeypatch, [row])
    assert result.reason == rp.CARRIED and result.seconds_since_last_product_shown is None
