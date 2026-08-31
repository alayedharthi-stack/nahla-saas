"""Partial unique index contract: one active local product ↔ one Meta item.

Shared by Alembic 0101, the claim helper, and PostgreSQL tests.
Does not write catalog rows.

Writers:
- LINK / orchestrator stamps assign via ``claim_active_meta_item_binding``.
- Import upsert may set ``meta_item_id`` directly; this index is the
  fail-closed backstop (no delete/merge/winner).
- Advisory ``pg_advisory_xact_lock`` remains an extra defense.
"""
from __future__ import annotations

UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM = "uq_products_active_tenant_meta_item"
ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED = "DUPLICATE_ACTIVE_META_BINDING_BLOCKED"

# Active rows only. Historical catalog_status values may keep the same
# meta_item_id; empty strings are treated as unbound.
UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_WHERE = (
    "catalog_status = 'active' "
    "AND meta_item_id IS NOT NULL "
    "AND btrim(meta_item_id) <> ''"
)

CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL = (
    f"CREATE UNIQUE INDEX {UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM} "
    f"ON products (tenant_id, meta_item_id) "
    f"WHERE {UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_WHERE}"
)

DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL = (
    f"DROP INDEX IF EXISTS {UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM}"
)

AUDIT_ACTIVE_DUPLICATE_GROUPS_SQL = """
SELECT
    tenant_id,
    meta_item_id,
    COUNT(*) AS active_row_count,
    array_agg(id ORDER BY id) AS product_ids
FROM products
WHERE catalog_status = 'active'
  AND meta_item_id IS NOT NULL
  AND btrim(meta_item_id) <> ''
GROUP BY tenant_id, meta_item_id
HAVING COUNT(*) > 1
ORDER BY tenant_id, meta_item_id
"""

AUDIT_HISTORICAL_OVERLAP_SQL = """
SELECT
    a.tenant_id,
    a.meta_item_id,
    a.id AS active_product_id,
    h.id AS historical_product_id,
    h.catalog_status AS historical_status
FROM products a
JOIN products h
  ON h.tenant_id = a.tenant_id
 AND h.meta_item_id = a.meta_item_id
 AND h.id <> a.id
WHERE a.catalog_status = 'active'
  AND a.meta_item_id IS NOT NULL
  AND btrim(a.meta_item_id) <> ''
  AND h.catalog_status IS DISTINCT FROM 'active'
ORDER BY a.tenant_id, a.meta_item_id, a.id, h.id
"""

AUDIT_STATUS_COUNTS_SQL = """
SELECT
    COALESCE(NULLIF(btrim(catalog_status), ''), 'active') AS catalog_status,
    COUNT(*) FILTER (
        WHERE meta_item_id IS NOT NULL AND btrim(meta_item_id) <> ''
    ) AS bound_rows,
    COUNT(*) AS total_rows
FROM products
GROUP BY 1
ORDER BY 1
"""


def format_duplicate_active_meta_binding_report(rows: list) -> str:
    lines = [
        ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED,
        "Active (tenant_id, meta_item_id) duplicates exist.",
        "No delete, merge, or winner selection was performed.",
    ]
    for row in rows:
        mapping = dict(row) if not isinstance(row, dict) else row
        lines.append(
            "tenant_id={tenant_id} meta_item_id={meta_item_id} "
            "active_row_count={active_row_count} product_ids={product_ids}".format(
                tenant_id=mapping.get("tenant_id"),
                meta_item_id=mapping.get("meta_item_id"),
                active_row_count=mapping.get("active_row_count") or mapping.get("n"),
                product_ids=mapping.get("product_ids") or mapping.get("ids"),
            )
        )
    return "\n".join(lines)


def raise_if_duplicate_active_meta_bindings(bind) -> None:
    """Fail closed. Never deletes, merges, or picks a winner."""
    from sqlalchemy import text as _text

    def _fetch(conn) -> list:
        result = conn.execute(_text(AUDIT_ACTIVE_DUPLICATE_GROUPS_SQL))
        if hasattr(result, "mappings"):
            return list(result.mappings())
        return list(result)

    # Alembic passes a Connection; tests may pass an Engine.
    if hasattr(bind, "pool") and hasattr(bind, "connect"):
        with bind.connect() as conn:
            rows = _fetch(conn)
    else:
        rows = _fetch(bind)
    if not rows:
        return
    raise RuntimeError(format_duplicate_active_meta_binding_report(rows))
