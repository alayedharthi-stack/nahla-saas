#!/usr/bin/env python3
"""Read-only Production audit: duplicate active (tenant_id, meta_item_id).

Does NOT modify rows. Does NOT create the unique index.

Exit codes
──────────
  0 — zero active duplicate groups (index is data-safe)
  1 — active duplicates exist (DUPLICATE_ACTIVE_META_BINDING_BLOCKED)
  2 — configuration / connectivity error

Usage
─────
  railway run --environment production --service nahla-saas \\
    python scripts/audit_active_meta_item_bindings_readonly.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import psycopg2
import psycopg2.extras

from catalog_meta_item_uniqueness import (  # noqa: E402
    AUDIT_ACTIVE_DUPLICATE_GROUPS_SQL,
    AUDIT_HISTORICAL_OVERLAP_SQL,
    AUDIT_STATUS_COUNTS_SQL,
    ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED,
    UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM,
    UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_WHERE,
)

SAMPLE_LIMIT = 40


def _db_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    return url


def main() -> int:
    conn = psycopg2.connect(_db_url())
    try:
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT current_database() AS db, current_user AS usr")
        ident = cur.fetchone() or {}
        print("Active Meta item binding audit (read-only)")
        print(f"database={ident.get('db')} user={ident.get('usr')}")
        print(f"proposed_index={UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM}")
        print(f"proposed_where={UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_WHERE}")
        print()

        cur.execute(AUDIT_STATUS_COUNTS_SQL)
        status_rows = list(cur.fetchall() or [])
        print("catalog_status totals (bound = non-empty meta_item_id):")
        for row in status_rows:
            print(
                f"  status={row['catalog_status']}"
                f" bound={int(row['bound_rows'])}"
                f" total={int(row['total_rows'])}"
            )
        print()

        cur.execute(AUDIT_ACTIVE_DUPLICATE_GROUPS_SQL)
        dupes = list(cur.fetchall() or [])
        print(f"active_duplicate_groups={len(dupes)}")
        if dupes:
            print(f"code={ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED}")
            print("Do not migrate. Do not delete, merge, or pick a winner.")
            for row in dupes[:SAMPLE_LIMIT]:
                print(
                    f"  tenant_id={row['tenant_id']}"
                    f" meta_item_id={row['meta_item_id']}"
                    f" active_row_count={int(row['active_row_count'])}"
                    f" product_ids={list(row['product_ids'] or [])}"
                )
            if len(dupes) > SAMPLE_LIMIT:
                print(f"  … {len(dupes) - SAMPLE_LIMIT} more groups")
        else:
            print("active_duplicate_groups=0 — partial unique index is data-safe")
        print()

        cur.execute(AUDIT_HISTORICAL_OVERLAP_SQL)
        overlaps = list(cur.fetchall() or [])
        print(
            "historical_overlap_with_active="
            f"{len(overlaps)} (allowed; index does not cover history)"
        )
        for row in overlaps[:SAMPLE_LIMIT]:
            print(
                f"  tenant_id={row['tenant_id']}"
                f" meta_item_id={row['meta_item_id']}"
                f" active_product_id={row['active_product_id']}"
                f" historical_product_id={row['historical_product_id']}"
                f" historical_status={row['historical_status']}"
            )
        if len(overlaps) > SAMPLE_LIMIT:
            print(f"  … {len(overlaps) - SAMPLE_LIMIT} more overlaps")

        cur.execute("ROLLBACK")
        return 1 if dupes else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
