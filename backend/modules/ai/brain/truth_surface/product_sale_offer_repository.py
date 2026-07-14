"""
product_sale_offer_repository.py
────────────────────────────────
Official PostgreSQL COUNT + bounded sample for catalog product sale offers.

Single CTE pipeline reads the matching set once:
  active catalog predicates → price extract → normalize → strict sale filter
  → COUNT (official) + sample (ORDER BY id ASC LIMIT 5)

Sample price strings are canonicalized via product_sale_offer_price_parse after SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .product_sale_offer_price_parse import canonical_price_string, normalize_extracted_price_raw

_STORE_WIDE_SALE_SQL = """
WITH active_products AS (
    SELECT
        p.id,
        p.title,
        p.metadata AS extra_metadata
    FROM products p
    WHERE p.tenant_id = :tenant_id
      AND p.merchant_hidden_at IS NULL
      AND p.catalog_status = 'active'
      AND p.in_stock IS TRUE
),
priced AS (
    SELECT
        ap.id,
        ap.title,
        CASE jsonb_typeof(ap.extra_metadata->'sale_price')
            WHEN 'object' THEN NULLIF(TRIM(ap.extra_metadata->'sale_price'->>'amount'), '')
            WHEN 'number' THEN (ap.extra_metadata->'sale_price')::text
            WHEN 'string' THEN NULLIF(TRIM(ap.extra_metadata->>'sale_price'), '')
            ELSE NULL
        END AS sale_raw,
        CASE jsonb_typeof(ap.extra_metadata->'regular_price')
            WHEN 'object' THEN NULLIF(TRIM(ap.extra_metadata->'regular_price'->>'amount'), '')
            WHEN 'number' THEN (ap.extra_metadata->'regular_price')::text
            WHEN 'string' THEN NULLIF(TRIM(ap.extra_metadata->>'regular_price'), '')
            ELSE NULL
        END AS regular_raw
    FROM active_products ap
),
normalized AS (
    SELECT
        pr.id,
        pr.title,
        REPLACE(TRIM(pr.sale_raw), ',', '') AS sale_norm,
        REPLACE(TRIM(pr.regular_raw), ',', '') AS regular_norm
    FROM priced pr
    WHERE pr.sale_raw IS NOT NULL
      AND pr.regular_raw IS NOT NULL
),
strict_sale AS (
    SELECT
        n.id,
        n.title,
        n.sale_norm,
        n.regular_norm
    FROM normalized n
    WHERE n.sale_norm ~ '^[0-9]+(\\.[0-9]+)?$'
      AND n.regular_norm ~ '^[0-9]+(\\.[0-9]+)?$'
      AND n.sale_norm::numeric > 0
      AND n.regular_norm::numeric > 0
      AND n.sale_norm::numeric < n.regular_norm::numeric
),
agg AS (
    SELECT COUNT(*)::int AS verified_count
    FROM strict_sale
),
samples AS (
    SELECT
        ss.id,
        ss.title,
        ss.sale_norm AS sale_price,
        ss.regular_norm AS regular_price
    FROM strict_sale ss
    ORDER BY ss.id ASC
    LIMIT 5
)
SELECT
    agg.verified_count,
    s.id AS product_id,
    s.title,
    s.sale_price,
    s.regular_price
FROM agg
LEFT JOIN samples s ON TRUE
"""


@dataclass(frozen=True)
class ProductScopedCatalogRow:
    id: int
    title: str
    extra_metadata: dict[str, Any]
    catalog_status: str
    in_stock: bool
    merchant_hidden_at: Any


_PRODUCT_SCOPED_SQL = """
SELECT
    p.id,
    p.title,
    p.metadata AS extra_metadata,
    p.catalog_status,
    p.in_stock,
    p.merchant_hidden_at
FROM products p
WHERE p.tenant_id = :tenant_id
  AND p.id = :product_id
LIMIT 1
"""


@dataclass(frozen=True)
class ProductSaleSampleRow:
    product_id: int
    title: str
    sale_price: str
    regular_price: str


@dataclass(frozen=True)
class StoreWideSaleSnapshot:
    verified_count: int
    sample_rows: List[ProductSaleSampleRow]


class ProductSaleOfferRepositoryError(RuntimeError):
    """Raised when the DB dialect or query execution is unsupported."""


def _canonical_from_sql_norm(norm: Optional[str]) -> str:
    if norm in (None, ""):
        return ""
    return normalize_extracted_price_raw(str(norm)) or ""


def _require_postgresql(db: Any) -> None:
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        raise ProductSaleOfferRepositoryError("postgresql_required")


def fetch_store_wide_sale_snapshot(
    db: Any,
    *,
    tenant_id: int,
) -> Optional[StoreWideSaleSnapshot]:
    """
    Official store-wide COUNT + sample from PostgreSQL.

    Returns snapshot on success. Raises ProductSaleOfferRepositoryError on failure.

    ``db`` must be a Session/Connection bound to the same PostgreSQL connection
    that owns any TEMP ``products`` table used in integration tests.
    """
    _require_postgresql(db)
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    if bind is None:
        raise ProductSaleOfferRepositoryError("missing_db_bind")
    try:
        if hasattr(db, "execute"):
            result = db.execute(
                text(_STORE_WIDE_SALE_SQL),
                {"tenant_id": int(tenant_id)},
            )
        else:
            with bind.connect() as conn:
                result = conn.execute(
                    text(_STORE_WIDE_SALE_SQL),
                    {"tenant_id": int(tenant_id)},
                )
                rows = result.fetchall()
                return _snapshot_from_rows(rows)
        rows = result.fetchall()
    except Exception as exc:
        raise ProductSaleOfferRepositoryError(str(exc)) from exc

    return _snapshot_from_rows(rows)


def fetch_product_scoped_catalog_row(
    db: Any,
    *,
    tenant_id: int,
    product_id: int,
) -> Optional[ProductScopedCatalogRow]:
    """Load one tenant-bound product row for product-scoped sale checks."""
    _require_postgresql(db)
    try:
        result = db.execute(
            text(_PRODUCT_SCOPED_SQL),
            {"tenant_id": int(tenant_id), "product_id": int(product_id)},
        )
        row = result.first()
    except Exception as exc:
        raise ProductSaleOfferRepositoryError(str(exc)) from exc
    if row is None:
        return None
    meta = row[2]
    if not isinstance(meta, dict):
        meta = {}
    return ProductScopedCatalogRow(
        id=int(row[0]),
        title=str(row[1] or "").strip(),
        extra_metadata=dict(meta),
        catalog_status=str(row[3] or "active"),
        in_stock=bool(row[4]),
        merchant_hidden_at=row[5],
    )


def _snapshot_from_rows(rows: Any) -> StoreWideSaleSnapshot:
    if not rows:
        return StoreWideSaleSnapshot(verified_count=0, sample_rows=[])

    verified_count = int(rows[0][0] or 0)
    sample_rows: List[ProductSaleSampleRow] = []
    seen_ids: set[int] = set()
    for row in rows:
        product_id = row[1]
        if product_id is None:
            continue
        pid = int(product_id)
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        sample_rows.append(
            ProductSaleSampleRow(
                product_id=pid,
                title=str(row[2] or "").strip(),
                sale_price=_canonical_from_sql_norm(row[3]),
                regular_price=_canonical_from_sql_norm(row[4]),
            )
        )
    sample_rows.sort(key=lambda r: r.product_id)
    return StoreWideSaleSnapshot(
        verified_count=verified_count,
        sample_rows=sample_rows[:5],
    )


def canonical_prices_from_metadata(metadata: dict[str, Any]) -> tuple[str, str]:
    """Parity helper: canonical sale/regular strings from metadata dict."""
    sale = canonical_price_string(metadata.get("sale_price")) or ""
    regular = canonical_price_string(metadata.get("regular_price")) or ""
    return sale, regular


def store_wide_sale_sql_text() -> str:
    """Expose SQL for golden compilation tests."""
    return _STORE_WIDE_SALE_SQL


__all__ = [
    "ProductScopedCatalogRow",
    "ProductSaleOfferRepositoryError",
    "ProductSaleSampleRow",
    "StoreWideSaleSnapshot",
    "canonical_prices_from_metadata",
    "fetch_product_scoped_catalog_row",
    "fetch_store_wide_sale_snapshot",
    "store_wide_sale_sql_text",
]
