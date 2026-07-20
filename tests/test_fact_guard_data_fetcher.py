from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH_DIR = REPO_ROOT / "services" / "ai-orchestrator"
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))


def _product(
    product_id: int,
    *,
    extra_metadata: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=product_id, extra_metadata=extra_metadata)


def _session_factory(
    *,
    tenant: object | None,
    products: list[object],
    coupons: list[object] | None = None,
    orders: list[object] | None = None,
) -> MagicMock:
    db = MagicMock()

    def _query(model):
        chain = MagicMock()
        if model.__name__ == "Tenant":
            chain.filter.return_value.first.return_value = tenant
        elif model.__name__ == "Product":
            chain.filter.return_value.all.return_value = products
        elif model.__name__ == "Coupon":
            chain.filter.return_value.all.return_value = coupons or []
        elif model.__name__ == "Order":
            order_chain = chain.filter.return_value
            order_chain.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
                orders or []
            )
        else:
            chain.filter.return_value.first.return_value = None
        return chain

    db.query.side_effect = _query
    return db


def _tenant() -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        same_day_delivery_enabled=False,
        pickup_enabled=True,
    )


@pytest.mark.parametrize(
    ("extra_metadata", "expected_in_stock", "expected_low_stock", "expected_discounted"),
    [
        (
            {"in_stock": True, "stock_count": 3, "sale_price": "89"},
            True,
            True,
            True,
        ),
        (
            {"in_stock": True, "stock_count": 12, "discount_pct": 15},
            True,
            False,
            True,
        ),
        (
            {"in_stock": False, "stock_count": 2},
            False,
            True,
            False,
        ),
        (
            None,
            False,
            False,
            False,
        ),
        (
            "not-a-dict",
            False,
            False,
            False,
        ),
        (
            ["in_stock", True],
            False,
            False,
            False,
        ),
    ],
    ids=[
        "in_stock_low_stock_sale_price",
        "in_stock_discount_pct",
        "out_of_stock_low_stock_only",
        "null_metadata",
        "string_metadata",
        "list_metadata",
    ],
)
def test_fetch_grounding_data_reads_product_extra_metadata(
    extra_metadata: object,
    expected_in_stock: bool,
    expected_low_stock: bool,
    expected_discounted: bool,
) -> None:
    from fact_guard.data_fetcher import fetch_grounding_data

    product = _product(501, extra_metadata=extra_metadata)
    db = _session_factory(tenant=_tenant(), products=[product])

    with patch("fact_guard.data_fetcher.SessionLocal", return_value=db):
        data = fetch_grounding_data(tenant_id=101, customer_phone="+966500000001")

    assert 501 in data.known_product_ids
    assert (501 in data.explicitly_in_stock_ids) is expected_in_stock
    assert (501 in data.low_stock_product_ids) is expected_low_stock
    assert (501 in data.discounted_product_ids) is expected_discounted


def test_fetch_grounding_data_malformed_metadata_does_not_invent_facts() -> None:
    from fact_guard.data_fetcher import fetch_grounding_data

    products = [
        _product(601, extra_metadata={"in_stock": "yes"}),
        _product(602, extra_metadata={"stock_count": "3"}),
        _product(603, extra_metadata={"sale_price": 0, "discount_pct": 0}),
    ]
    db = _session_factory(tenant=_tenant(), products=products)

    with patch("fact_guard.data_fetcher.SessionLocal", return_value=db):
        data = fetch_grounding_data(tenant_id=101, customer_phone="+966500000001")

    assert data.known_product_ids == {601, 602, 603}
    assert data.explicitly_in_stock_ids == set()
    assert data.low_stock_product_ids == set()
    assert data.discounted_product_ids == set()


def test_fetch_grounding_data_generic_catalog_products() -> None:
    from fact_guard.data_fetcher import fetch_grounding_data

    products = [
        _product(
            701,
            extra_metadata={
                "title": "حذاء رياضي أبيض",
                "in_stock": True,
                "stock_count": 4,
            },
        ),
        _product(
            702,
            extra_metadata={
                "title": "قميص قطني أزرق",
                "in_stock": True,
                "stock_count": 20,
                "discount_pct": 10,
            },
        ),
        _product(
            703,
            extra_metadata={
                "title": "عطر ورد 100ml",
                "in_stock": False,
                "stock_count": 0,
            },
        ),
    ]
    valid_coupon = SimpleNamespace(
        code="WELCOME10",
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db = _session_factory(
        tenant=_tenant(),
        products=products,
        coupons=[valid_coupon],
    )

    with patch("fact_guard.data_fetcher.SessionLocal", return_value=db):
        data = fetch_grounding_data(tenant_id=101, customer_phone="+966500000001")

    assert data.known_product_ids == {701, 702, 703}
    assert data.explicitly_in_stock_ids == {701, 702}
    assert data.low_stock_product_ids == {701}
    assert data.discounted_product_ids == {702}
    assert data.valid_coupon_codes == {"WELCOME10"}
