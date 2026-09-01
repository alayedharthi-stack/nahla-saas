"""Partial unique indexes for catalog membership identity.

One active membership per (catalog, retailer_id) already exists.
This module adds fail-closed uniqueness for:

- (tenant_id, catalog_id, product_id, salla_variant_id)
- (tenant_id, catalog_id, meta_item_id)

Upgrade fails without delete/merge when duplicates exist.
"""
from __future__ import annotations

UQ_MEMBERSHIP_VARIANT_KEY = "uq_mcm_tenant_catalog_product_salla_vid"
UQ_MEMBERSHIP_META_ITEM = "uq_mcm_tenant_catalog_meta_item"
ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED = "DUPLICATE_CATALOG_IDENTITY_BLOCKED"

CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL = (
    f"CREATE UNIQUE INDEX {UQ_MEMBERSHIP_VARIANT_KEY} "
    "ON meta_catalog_memberships (tenant_id, catalog_id, product_id, salla_variant_id) "
    "WHERE salla_variant_id IS NOT NULL AND btrim(salla_variant_id) <> ''"
)

CREATE_UQ_MEMBERSHIP_META_ITEM_SQL = (
    f"CREATE UNIQUE INDEX {UQ_MEMBERSHIP_META_ITEM} "
    "ON meta_catalog_memberships (tenant_id, catalog_id, meta_item_id) "
    "WHERE meta_item_id IS NOT NULL AND btrim(meta_item_id) <> ''"
)

DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL = f"DROP INDEX IF EXISTS {UQ_MEMBERSHIP_VARIANT_KEY}"
DROP_UQ_MEMBERSHIP_META_ITEM_SQL = f"DROP INDEX IF EXISTS {UQ_MEMBERSHIP_META_ITEM}"

AUDIT_DUP_VARIANT_KEY_SQL = """
SELECT tenant_id, catalog_id, product_id, salla_variant_id, COUNT(*) AS n,
       array_agg(id ORDER BY id) AS ids
FROM meta_catalog_memberships
WHERE salla_variant_id IS NOT NULL AND btrim(salla_variant_id) <> ''
GROUP BY tenant_id, catalog_id, product_id, salla_variant_id
HAVING COUNT(*) > 1
"""

AUDIT_DUP_META_ITEM_SQL = """
SELECT tenant_id, catalog_id, meta_item_id, COUNT(*) AS n,
       array_agg(id ORDER BY id) AS ids
FROM meta_catalog_memberships
WHERE meta_item_id IS NOT NULL AND btrim(meta_item_id) <> ''
GROUP BY tenant_id, catalog_id, meta_item_id
HAVING COUNT(*) > 1
"""

AUDIT_DUP_RETAILER_SQL = """
SELECT tenant_id, catalog_id, retailer_id, COUNT(*) AS n,
       array_agg(id ORDER BY id) AS ids
FROM meta_catalog_memberships
WHERE retailer_id IS NOT NULL AND btrim(retailer_id) <> ''
GROUP BY tenant_id, catalog_id, retailer_id
HAVING COUNT(*) > 1
"""


def format_duplicate_catalog_identity_report(kind: str, rows: list) -> str:
    lines = [
        ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED,
        f"Duplicate {kind} rows exist.",
        "No delete, merge, or winner selection was performed.",
    ]
    for row in rows:
        mapping = dict(row) if not isinstance(row, dict) else row
        lines.append(str(mapping))
    return "\n".join(lines)


def raise_if_duplicate_catalog_identities(bind) -> None:
    from sqlalchemy import text as _text

    def _fetch(conn, sql: str) -> list:
        result = conn.execute(_text(sql))
        if hasattr(result, "mappings"):
            return list(result.mappings())
        return list(result)

    queries = (
        ("salla_variant_id", AUDIT_DUP_VARIANT_KEY_SQL),
        ("meta_item_id", AUDIT_DUP_META_ITEM_SQL),
        ("retailer_id", AUDIT_DUP_RETAILER_SQL),
    )
    if hasattr(bind, "pool") and hasattr(bind, "connect"):
        with bind.connect() as conn:
            for kind, sql in queries:
                rows = _fetch(conn, sql)
                if rows:
                    raise RuntimeError(format_duplicate_catalog_identity_report(kind, rows))
        return
    for kind, sql in queries:
        rows = _fetch(bind, sql)
        if rows:
            raise RuntimeError(format_duplicate_catalog_identity_report(kind, rows))
