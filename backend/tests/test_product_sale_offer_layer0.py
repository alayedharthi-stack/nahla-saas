"""Product sale offer loader, projection, and consumption gate tests."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.catalog import CATALOG_STATUS_ACTIVE  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.product_sale_offer_compose_projection import (  # noqa: E402
    ProductSaleOfferProjectionError,
    project_general_offer_discovery_compose_facts,
    project_product_sale_offer_compose_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_consumption_gate import (  # noqa: E402
    maybe_general_offer_discovery_compose_facts,
    maybe_product_sale_offer_compose_facts,
    safe_product_sale_loader_telemetry,
)
from modules.ai.brain.truth_surface.product_sale_offer_loader import (  # noqa: E402
    _store_wide_from_snapshot,
    classify_product_sale_question_kind,
    is_strict_product_sale,
    load_product_sale_offer_facts,
    should_load_product_sale_offer_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_repository import (  # noqa: E402
    ProductSaleOfferRepositoryError,
    ProductSaleSampleRow,
    ProductScopedCatalogRow,
    StoreWideSaleSnapshot,
)


def _product(
    pid: int,
    *,
    title: str = "حذاء رياضي أبيض",
    sale: str | None = None,
    regular: str | None = None,
    in_stock: bool = True,
    tenant_id: int = 1,
) -> SimpleNamespace:
    meta = {}
    if sale is not None:
        meta["sale_price"] = sale
    if regular is not None:
        meta["regular_price"] = regular
    return SimpleNamespace(
        id=pid,
        tenant_id=tenant_id,
        title=title,
        in_stock=in_stock,
        catalog_status=CATALOG_STATUS_ACTIVE,
        merchant_hidden_at=None,
        extra_metadata=meta,
    )


def _product_row(product: SimpleNamespace) -> ProductScopedCatalogRow:
    return ProductScopedCatalogRow(
        id=int(product.id),
        title=product.title,
        extra_metadata=dict(product.extra_metadata),
        catalog_status=product.catalog_status,
        in_stock=product.in_stock,
        merchant_hidden_at=product.merchant_hidden_at,
    )


def _load_product_scoped_facts(
    product: SimpleNamespace | None,
    *,
    message: str,
    brain_state: Any = None,
) -> tuple[list[TrustedFact], dict[str, Any]]:
    row = _product_row(product) if product is not None else None
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_product_scoped_catalog_row",
        return_value=row,
    ):
        return load_product_sale_offer_facts(
            db=MagicMock(),
            tenant_id=1,
            message=message,
            brain_state=brain_state,
        )



def _snapshot(count: int, *, availability: str = "active_sale_present") -> StoreWideSaleSnapshot:
    rows = [
        ProductSaleSampleRow(
            product_id=1,
            title="حذاء رياضي أبيض",
            sale_price="59",
            regular_price="79",
        ),
        ProductSaleSampleRow(
            product_id=2,
            title="قميص قطني أزرق",
            sale_price="90",
            regular_price="120",
        ),
    ]
    return StoreWideSaleSnapshot(verified_count=count, sample_rows=rows[: min(count, 5)])


def test_store_wide_count_and_sample_generic_merchant() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=_snapshot(3),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(),
            tenant_id=1,
            message="عندكم عروض؟",
        )
    assert len(facts) == 1
    record = facts[0].value
    assert record["product_sale_availability"] == "active_sale_present"
    assert record["verified_on_sale_product_count"] == 3
    assert record["allow_price_mention"] is True
    assert len(record["sample_products"]) <= 5
    assert obs["question_kind"] == "store_wide"
    assert obs["sample_product_ids"] == [1, 2]


def test_store_wide_none_verified_from_official_count() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=StoreWideSaleSnapshot(verified_count=0, sample_rows=[]),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(),
            tenant_id=1,
            message="في تخفيضات؟",
        )
    record = facts[0].value
    assert record["product_sale_availability"] == "none_verified"
    assert record["verified_on_sale_product_count"] == 0
    assert record["allow_price_mention"] is False
    assert "sample_products" not in record
    assert obs["verified_on_sale_product_count"] == 0
    assert "sample_product_ids" not in obs


def test_store_wide_db_error_unavailable_without_count() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        side_effect=ProductSaleOfferRepositoryError("db_down"),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(),
            tenant_id=1,
            message="عندكم عروض؟",
        )
    assert facts == []
    assert obs["product_sale_availability"] == "unavailable"
    assert "verified_on_sale_product_count" not in obs


def test_product_scoped_requires_resolved_focus() -> None:
    facts, obs = load_product_sale_offer_facts(
        db=MagicMock(),
        tenant_id=1,
        message="هل فيه عرض على المنتج؟",
        brain_state=SimpleNamespace(current_product_focus=None),
    )
    record = facts[0].value
    assert record["product_sale_availability"] == "requires_product_context"
    assert record["allow_price_mention"] is False
    assert "verified_on_sale_product_count" not in record
    assert "verified_on_sale_product_count" not in obs
    assert obs["question_kind"] == "product_scoped"


def test_product_scoped_with_focus_strict_sale() -> None:
    product = _product(9, title="عطر ورد 100ml", sale="199", regular="249")
    brain = SimpleNamespace(current_product_focus={"product_id": 9})
    facts, _obs = _load_product_scoped_facts(
        product,
        message="هل المنتج مخفض؟",
        brain_state=brain,
    )
    record = facts[0].value
    assert record["target_product"]["is_on_sale"] is True
    assert record["allow_price_mention"] is True
    assert record["verified_on_sale_product_count"] == 1


def test_product_scoped_inactive_product_unavailable_without_prices() -> None:
    product = _product(9, sale="199", regular="249", in_stock=False)
    brain = SimpleNamespace(current_product_focus={"product_id": 9})
    facts, obs = _load_product_scoped_facts(
        product,
        message="هل المنتج مخفض؟",
        brain_state=brain,
    )
    record = facts[0].value
    assert record["product_sale_availability"] == "unavailable"
    assert record.get("target_product") is None
    assert record["allow_price_mention"] is False
    assert "verified_on_sale_product_count" not in record
    assert "verified_on_sale_product_count" not in obs


def _snapshot_with_sale_record(record: dict) -> TrustedContextSnapshot:
    return TrustedContextSnapshot(
        tenant_id=1,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value=record,
                source=TruthSource.PRODUCTS_TABLE,
                path="test",
            )
        ],
    )


def test_project_product_sale_offer_compose_facts_no_ids() -> None:
    snap = _snapshot_with_sale_record(
        {
            "question_kind": "store_wide",
            "product_sale_availability": "active_sale_present",
            "verified_on_sale_product_count": 2,
            "sample_products": [
                {
                    "title": "حذاء رياضي أبيض",
                    "sale_price": "80",
                    "regular_price": "100",
                    "product_id": 99,
                },
            ],
            "allow_price_mention": True,
        }
    )
    payload = project_product_sale_offer_compose_facts(snapshot=snap)
    assert payload["surface"] == "product_sale_offer_answer"
    assert "product_id" not in str(payload)
    assert payload["sample_products"][0].keys() == {"title", "sale_price", "regular_price"}


def test_telemetry_excludes_titles_and_prices() -> None:
    obs = {
        "product_sale_availability": "active_sale_present",
        "verified_on_sale_product_count": 2,
        "question_kind": "store_wide",
        "loader_duration_ms": 12,
        "sample_product_ids": [1, 2],
        "sample_products": [{"title": "x", "sale_price": "1", "regular_price": "2"}],
    }
    telemetry = safe_product_sale_loader_telemetry(obs)
    assert "sample_product_ids" in telemetry
    assert "title" not in str(telemetry)
    assert "sale_price" not in telemetry


def test_general_offer_discovery_namespaced_bundles() -> None:
    snap = _snapshot_with_sale_record(
        {
            "question_kind": "store_wide",
            "product_sale_availability": "active_sale_present",
            "verified_on_sale_product_count": 1,
            "sample_products": [],
            "allow_price_mention": True,
        }
    )
    coupon_facts = {"coupon_availability": "active_eligible_present"}
    payload = project_general_offer_discovery_compose_facts(
        snapshot=snap,
        trusted_coupon_offer_facts=coupon_facts,
    )
    assert payload["surface"] == "general_offer_discovery_answer"
    assert payload["product_sale_offer_facts"] is not None
    assert payload["trusted_coupon_offer_facts"] == coupon_facts


def test_consumption_gates_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snapshot_with_sale_record(
        {
            "question_kind": "store_wide",
            "product_sale_availability": "active_sale_present",
            "verified_on_sale_product_count": 1,
            "sample_products": [],
            "allow_price_mention": True,
        }
    )
    assert maybe_product_sale_offer_compose_facts(message="عرض", snapshot=snap) is None
    assert (
        maybe_general_offer_discovery_compose_facts(
            message="عندكم عروض؟",
            snapshot=snap,
        )
        is None
    )


def test_should_load_independent_from_coupon() -> None:
    assert should_load_product_sale_offer_facts(message="عندكم عروض؟")
    assert classify_product_sale_question_kind("عندكم عروض؟") == "store_wide"


def test_projection_missing_record_raises() -> None:
    snap = TrustedContextSnapshot(tenant_id=1, facts=[])
    with pytest.raises(ProductSaleOfferProjectionError):
        project_product_sale_offer_compose_facts(snapshot=snap)


def test_store_wide_from_snapshot_deterministic_order() -> None:
    snapshot = StoreWideSaleSnapshot(
        verified_count=2,
        sample_rows=[
            ProductSaleSampleRow(2, "B", "10", "20"),
            ProductSaleSampleRow(1, "A", "5", "10"),
        ],
    )
    facts, obs = _store_wide_from_snapshot(
        snapshot=snapshot,
        question_kind="store_wide",
        started=__import__("time").perf_counter(),
    )
    assert facts[0].value["verified_on_sale_product_count"] == 2
    assert obs["sample_product_ids"] == [2, 1]
