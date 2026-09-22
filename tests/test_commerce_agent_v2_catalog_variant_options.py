"""The product view carries the colours, sizes and other options a customer can
buy now, read from the catalog's own variant rows.

Tenant 1, September 2026: the model called a white/fuchsia dress "black"
because the search view carried no variant at all. The platform now hands the
model the options of the variants in stock, bounded; the wording stays the
model's. Offline, generic merchant rows.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.commerce_agent_v2.tools import catalog  # noqa: E402


def variant(**fields: Any) -> dict:
    row: dict = {"id": 1, "in_stock": True, "stock_quantity": 2, "options": {}, "is_default": False}
    row.update(fields)
    return row


def test_only_variants_the_customer_can_buy_now_contribute_their_options() -> None:
    options, in_stock, total = catalog._variant_options([
        variant(id=1, is_default=True, options={"اللون": "افتراضي"}),
        variant(id=2, options={"اللون": "أبيض", "المقاس": "38 - S"}),
        variant(id=3, stock_quantity=0, options={"اللون": "أسود", "المقاس": "42 - L"}),
        variant(id=4, in_stock=False, options={"اللون": "بيج"}),
        variant(id=5, stock_quantity=None, options={"اللون": "فوشي", "المقاس": "40 - M"}),
        variant(id=6, stock_quantity="many", options={"اللون": "أبيض"}),
    ])
    assert options == {"اللون": ["أبيض", "فوشي"], "المقاس": ["38 - S", "40 - M"]}
    assert in_stock == 3 and total == 5


def test_a_product_without_variants_has_no_options_and_no_counts() -> None:
    assert catalog._variant_options(None) == ({}, None, None)
    assert catalog._variant_options([]) == ({}, None, None)
    assert catalog._variant_options([variant(is_default=True)]) == ({}, None, None)
    assert catalog._variant_options("not a list") == ({}, None, None)


def test_names_and_values_are_bounded() -> None:
    rows = [variant(id=i, options={f"خيار{i}": f"قيمة{i}"}) for i in range(10)]
    options, _, _ = catalog._variant_options(rows)
    assert len(options) == catalog.MAX_VARIANT_OPTION_NAMES
    rows = [variant(id=i, options={"المقاس": str(30 + i)}) for i in range(20)]
    options, in_stock, total = catalog._variant_options(rows)
    assert len(options["المقاس"]) == catalog.MAX_VARIANT_OPTION_VALUES
    assert in_stock == 20 and total == 20
    long_name = "x" * 80
    options, _, _ = catalog._variant_options([variant(options={long_name: "y" * 80})])
    assert list(options) == [long_name[:40]] and options[long_name[:40]] == ["y" * 40]


def test_a_product_row_projects_its_variant_options_into_snapshot_and_evidence() -> None:
    row = {
        "id": 21, "external_id": "P21", "title": "فستان", "description": "فستان صيفي",
        "price": "289.0", "sale_price": "", "regular_price": "289.0", "currency": "SAR",
        "in_stock": True, "stock_qty": 6, "image_url": "", "product_url": "", "orderable": True,
        "variants": [
            variant(id=1, is_default=True),
            variant(id=2, options={"اللون": "أبيض", "المقاس": "38 - S"}),
            variant(id=3, stock_quantity=0, options={"اللون": "أسود"}),
        ],
    }
    snapshot, evidence = catalog._product_evidence(row)
    assert snapshot.variant_options == {"اللون": ["أبيض"], "المقاس": ["38 - S"]}
    assert snapshot.variants_in_stock == 1 and snapshot.variants_total == 2
    assert evidence.fields["variant_options"] == snapshot.variant_options
    assert evidence.fields["variants_in_stock"] == 1 and evidence.fields["variants_total"] == 2
    assert snapshot.evidence_ref == "catalog:product:21"


def test_a_row_without_variants_projects_nothing_extra() -> None:
    row = {"id": 22, "title": "فستان", "price": "149.0", "in_stock": False, "stock_qty": None}
    snapshot, evidence = catalog._product_evidence(row)
    assert snapshot.variant_options == {} and snapshot.variants_in_stock is None
    assert evidence.fields["variants_total"] is None


def test_a_providers_internal_option_ids_are_not_an_option_the_customer_picks() -> None:
    """Tenant 1 product 37 carried Salla's own id array beside the real size.

    The list reached the model as an option named ``option_value_ids`` whose
    values read ``"['1064266980', '1837256091']"`` — something no customer
    says and the model could repeat. The shape decides, not the name.
    """
    options, in_stock, total = catalog._variant_options([
        variant(id=1, options={"المقاس": "40 - M",
                               "option_value_ids": ["1064266980", "1837256091"]}),
        variant(id=2, options={"المقاس": "42 - L",
                               "option_value_ids": ["1527950054", "1837256091"]}),
    ])
    assert options == {"المقاس": ["40 - M", "42 - L"]}
    assert in_stock == 2 and total == 2


def test_only_a_scalar_value_is_carried_whatever_the_provider_nests() -> None:
    options, _, _ = catalog._variant_options([
        variant(options={
            "Colour": "White",          # a generic merchant, non-Arabic option name
            "Size": 42,                 # a number is still something a customer says
            "internal_map": {"id": 7},  # a mapping is bookkeeping
            "internal_list": [1, 2],    # so is a list
            "flag": True,               # and a boolean is not a value anyone picks
            "blank": "   ",
        }),
    ])
    assert options == {"Colour": ["White"], "Size": ["42"]}


def test_a_variant_whose_options_are_all_internal_contributes_no_option_name() -> None:
    options, in_stock, total = catalog._variant_options([
        variant(id=1, options={"option_value_ids": ["9", "8"]}),
        variant(id=2, options={"اللون": "أزرق"}),
    ])
    assert options == {"اللون": ["أزرق"]}
    assert in_stock == 2 and total == 2
