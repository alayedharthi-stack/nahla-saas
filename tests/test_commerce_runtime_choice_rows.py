"""A selector's rows are told apart by the merchant's own facts, or not shown.

Tenant 1's five dresses are all titled «فستان». WhatsApp refuses an interactive
payload whose visible titles repeat, so a selector built from titles alone
would collapse to one row. The platform composes the label from what the
merchant's records carry — the price it sells at, the option values it is in
stock in — and drops a product that no fact tells apart rather than inventing
one.

Offline, and merchant-agnostic: the same rules run on identical dress titles,
on a generic mixed catalogue and on a merchant with no variants at all.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.commerce_runtime import choice_rows as cr  # noqa: E402


def product(product_id: int, title: str, **fields: Any) -> dict:
    row: dict = {"product_id": product_id, "title": title, "currency": "SAR"}
    row.update(fields)
    return row


def titles(result: cr.ChoiceRows) -> list:
    return [row.title for row in result.rows]


def test_distinct_titles_are_left_exactly_as_the_merchant_wrote_them() -> None:
    result = cr.choice_rows([
        product(11, "قميص قطني أزرق", price="129.0"),
        product(12, "عطر ورد 100ml", price="240.0"),
        product(13, "حذاء رياضي أبيض", price="310.0"),
    ])
    assert titles(result) == ["قميص قطني أزرق", "عطر ورد 100ml", "حذاء رياضي أبيض"]
    assert result.indistinguishable == ()


def test_identical_titles_are_separated_by_the_price_the_merchant_sells_at() -> None:
    """Tenant 1's own shape: five rows, one word between them."""
    result = cr.choice_rows([
        product(23, "فستان", price="289.0", sale_price="144.0"),
        product(37, "فستان", price="229.0", sale_price="114.0"),
        product(38, "فستان", price="279.0", sale_price="83.0"),
    ])
    assert titles(result) == ["فستان · 144.0 SAR", "فستان · 114.0 SAR", "فستان · 83.0 SAR"]
    assert result.indistinguishable == ()
    assert [row.product_id for row in result.rows] == [23, 37, 38]


def test_an_option_value_only_this_product_has_separates_it_when_prices_match() -> None:
    result = cr.choice_rows([
        product(1, "فستان", price="199.0", variant_options={"اللون": ["أسود"]}),
        product(2, "فستان", price="199.0", variant_options={"اللون": ["بيج"]}),
    ])
    assert titles(result) == ["فستان · أسود", "فستان · بيج"]


def test_products_no_fact_tells_apart_are_numbered_rather_than_dropped() -> None:
    """Nothing real disappears because of a display limit.

    Two of these three are identical in every stored fact. The whole group is
    numbered instead — a position is not a claim about the product, it is a
    true statement about the list the customer is looking at, in the words
    they already use for it.
    """
    result = cr.choice_rows([
        product(1, "فستان", price="199.0"),
        product(2, "فستان", price="199.0"),
        product(3, "فستان", price="250.0"),
    ])
    assert titles(result) == ["فستان · 1", "فستان · 2", "فستان · 3"]
    assert result.indistinguishable == () and result.complete
    assert [row.description for row in result.rows] == ["199.0 SAR", "199.0 SAR", "250.0 SAR"]


def test_the_whole_group_is_numbered_so_a_number_is_never_read_as_a_price() -> None:
    """«فستان · 1» beside «فستان · 250 SAR» invites the 1 to be read as money."""
    result = cr.choice_rows([
        product(1, "فستان", price="199.0"),
        product(2, "فستان", price="199.0"),
    ])
    assert titles(result) == ["فستان · 1", "فستان · 2"]


def test_a_group_the_merchants_facts_can_separate_is_still_labelled_by_them() -> None:
    result = cr.choice_rows([
        product(1, "فستان", price="199.0"),
        product(2, "فستان", price="250.0"),
    ])
    assert titles(result) == ["فستان · 199.0 SAR", "فستان · 250.0 SAR"]


def test_the_number_is_the_row_position_the_customer_sees() -> None:
    result = cr.choice_rows([
        product(9, "عطر ورد 100ml", price="240.0"),
        product(1, "فستان", price="199.0"),
        product(2, "فستان", price="199.0"),
    ])
    assert titles(result) == ["عطر ورد 100ml", "فستان · 2", "فستان · 3"]


def test_a_long_title_gives_room_to_the_number_too() -> None:
    long_title = "قميص قطني أزرق بأكمام طويلة"
    result = cr.choice_rows([
        product(1, long_title, price="199.0"),
        product(2, long_title, price="199.0"),
    ])
    assert len(result.rows) == 2 and result.complete
    for row in result.rows:
        assert len(row.title) <= cr.MAX_ROW_TITLE
    assert result.rows[0].title.endswith(" · 1")
    assert result.rows[1].title.endswith(" · 2")


def test_a_long_title_gives_room_to_the_fact_rather_than_cutting_it() -> None:
    """A price truncated halfway states a number the merchant does not."""
    long_title = "قميص قطني أزرق بأكمام طويلة"
    result = cr.choice_rows([
        product(1, long_title, price="129.0"),
        product(2, long_title, price="149.0"),
    ])
    assert len(result.rows) == 2
    for row in result.rows:
        assert len(row.title) <= cr.MAX_ROW_TITLE
    assert result.rows[0].title.endswith("129.0 SAR")
    assert result.rows[1].title.endswith("149.0 SAR")


def test_every_title_is_unique_under_the_providers_own_comparison() -> None:
    """Arabic orthographic variants are the same title to Meta."""
    result = cr.choice_rows([
        product(1, "عباية", price="300.0"),
        product(2, "عبايه", price="350.0"),
    ])
    # Spelled differently, the same title to the provider — so both are labelled.
    assert titles(result) == ["عباية · 300.0 SAR", "عبايه · 350.0 SAR"]
    keys = [cr._title_key(row.title) for row in result.rows]
    assert len(keys) == len(set(keys)) == 2


def test_the_description_carries_price_and_the_options_in_stock() -> None:
    result = cr.choice_rows([
        product(1, "فستان", price="289.0", sale_price="144.0",
                variant_options={"اللون": ["أبيض", "فوشي"], "المقاس": ["38 - S", "40 - M"]}),
    ])
    assert result.rows[0].description == "144.0 SAR · أبيض/فوشي · 38 - S/40 - M"


def test_a_description_that_would_not_fit_drops_whole_fields_not_half_a_value() -> None:
    result = cr.choice_rows([
        product(1, "فستان", price="289.0",
                variant_options={"اللون": ["أبيض جداً جداً طويل الاسم", "فوشي غامق جدا"],
                                 "المقاس": ["38 - S", "40 - M", "42 - L"]}),
    ])
    description = result.rows[0].description
    assert len(description) <= cr.MAX_ROW_DESCRIPTION
    assert description.startswith("289.0 SAR")


def test_a_merchant_with_no_variants_and_no_currency_still_gets_usable_rows() -> None:
    result = cr.choice_rows([
        {"product_id": 5, "title": "قميص قطني أزرق", "price": "129"},
        {"product_id": 6, "title": "قميص قطني أزرق", "price": "149"},
    ])
    assert titles(result) == ["قميص قطني أزرق · 129", "قميص قطني أزرق · 149"]
    assert result.rows[0].description == "129"


def test_a_row_without_a_usable_identity_or_title_is_never_offered() -> None:
    result = cr.choice_rows([
        {"title": "بلا معرّف", "price": "10"},
        {"product_id": 0, "title": "معرّف غير صالح", "price": "10"},
        product(7, "   ", price="10"),
        product(8, "عطر ورد 100ml", price="10"),
    ])
    assert [row.product_id for row in result.rows] == [8]
    assert result.indistinguishable == (7,)


def test_an_empty_set_is_empty_rather_than_an_error() -> None:
    result = cr.choice_rows([])
    assert result.rows == () and result.indistinguishable == ()
    assert not result


def test_a_set_every_product_could_join_is_complete() -> None:
    result = cr.choice_rows([
        product(11, "قميص قطني أزرق", price="129.0"),
        product(12, "عطر ورد 100ml", price="240.0"),
    ])
    assert result.complete and len(result.rows) == 2


def test_looking_alike_no_longer_makes_a_set_incomplete() -> None:
    result = cr.choice_rows([
        product(1, "فستان", price="199.0"),
        product(2, "فستان", price="199.0"),
        product(3, "فستان", price="250.0"),
    ])
    assert result.complete and result.offered == 3 and len(result.rows) == 3


def test_a_product_with_no_usable_identity_also_makes_the_set_incomplete() -> None:
    result = cr.choice_rows([
        {"title": "بلا معرّف", "price": "10"},
        product(8, "عطر ورد 100ml", price="10"),
    ])
    assert not result.complete
    assert result.offered == 2 and [row.product_id for row in result.rows] == [8]


def test_an_empty_set_loses_nothing() -> None:
    result = cr.choice_rows([])
    assert result.complete and not result
