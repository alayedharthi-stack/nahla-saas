"""PostgreSQL repository tests for product_sale_offer COUNT + sample."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.product_sale_offer_repository import (  # noqa: E402
    ProductSaleOfferRepositoryError,
    ProductSaleSampleRow,
    StoreWideSaleSnapshot,
    fetch_store_wide_sale_snapshot,
    store_wide_sale_sql_text,
)


def test_store_wide_sql_uses_postgres_jsonb_typeof_and_metadata_column() -> None:
    sql = store_wide_sale_sql_text()
    assert "jsonb_typeof" in sql
    assert "p.metadata AS extra_metadata" in sql
    assert "merchant_hidden_at IS NULL" in sql
    assert "catalog_status = 'active'" in sql
    assert "in_stock IS TRUE" in sql
    assert "ORDER BY ss.id ASC" in sql
    assert "LIMIT 5" in sql
    assert "LEFT JOIN samples s ON TRUE" in sql


def test_store_wide_sql_compiles_on_postgresql_dialect() -> None:
    from sqlalchemy import text  # noqa: PLC0415

    stmt = text(store_wide_sale_sql_text())
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "tenant_id" in compiled


def test_fetch_store_wide_rejects_non_postgresql() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"
    with pytest.raises(ProductSaleOfferRepositoryError, match="postgresql_required"):
        fetch_store_wide_sale_snapshot(db, tenant_id=1)


def test_fetch_store_wide_none_verified_single_row() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    result = MagicMock()
    result.fetchall.return_value = [(0, None, None, None, None)]
    db.execute.return_value = result

    snapshot = fetch_store_wide_sale_snapshot(db, tenant_id=1)
    assert snapshot == StoreWideSaleSnapshot(verified_count=0, sample_rows=[])


def test_fetch_store_wide_count_with_deterministic_sample() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    result = MagicMock()
    result.fetchall.return_value = [
        (3, 1, "حذاء رياضي أبيض", "59", "79"),
        (3, 2, "قميص قطني أزرق", "90", "120"),
        (3, 3, "عطر ورد 100ml", "199", "249"),
    ]
    db.execute.return_value = result

    snapshot = fetch_store_wide_sale_snapshot(db, tenant_id=1)
    assert snapshot.verified_count == 3
    assert len(snapshot.sample_rows) == 3
    assert snapshot.sample_rows[0] == ProductSaleSampleRow(
        product_id=1,
        title="حذاء رياضي أبيض",
        sale_price="59",
        regular_price="79",
    )
    assert [row.product_id for row in snapshot.sample_rows] == [1, 2, 3]


def test_fetch_store_wide_db_error_maps_to_repository_error() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.side_effect = RuntimeError("db_down")
    with pytest.raises(ProductSaleOfferRepositoryError):
        fetch_store_wide_sale_snapshot(db, tenant_id=1)
