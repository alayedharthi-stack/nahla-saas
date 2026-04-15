#!/usr/bin/env python
"""
scripts/verify_salla_no_duplicates.py
───────────────────────────────────────
Step 2 (pre-migration): verify that no duplicate Salla integrations remain.

Run this AFTER cleanup_salla_duplicates.py --execute and BEFORE running
alembic upgrade.  Migration 0017 contains the same pre-check and will raise
a RuntimeError if duplicates are present; this script lets you catch that
condition before touching the database schema.

Exit codes
──────────
  0 — database is clean; safe to apply migration 0017
  1 — duplicates still exist; run cleanup first

Usage
─────
  python scripts/verify_salla_no_duplicates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402
from database.session import SessionLocal  # noqa: E402


def _has_external_store_id_column(db) -> bool:
    try:
        db.execute(text("SELECT external_store_id FROM integrations LIMIT 0"))
        return True
    except (OperationalError, ProgrammingError):
        # PostgreSQL aborts the transaction on error — rollback so the session stays usable.
        try:
            db.rollback()
        except Exception:
            pass
        return False


def verify() -> bool:
    """
    Query for duplicate (provider, external_store_id) pairs.
    Prints a report and returns True when the database is clean.
    """
    db = SessionLocal()
    try:
        has_ext_col = _has_external_store_id_column(db)

        # ── Check 1: duplicate external_store_id (only after migration 0017) ───
        dup_rows: list = []
        if has_ext_col:
            dup_rows = list(
                db.execute(
                    text("""
                        SELECT   provider,
                                 external_store_id,
                                 COUNT(*) AS cnt
                        FROM     integrations
                        WHERE    provider = 'salla'
                          AND    external_store_id IS NOT NULL
                        GROUP BY provider, external_store_id
                        HAVING   COUNT(*) > 1
                    """)
                )
            )

        # ── Check 2: pre-migration — duplicate config store_id for enabled rows ─
        if not has_ext_col:
            dup_rows = list(
                db.execute(
                    text("""
                        SELECT config->>'store_id' AS store_id, COUNT(*) AS cnt
                        FROM integrations
                        WHERE provider = 'salla'
                          AND config->>'store_id' IS NOT NULL
                          AND config->>'store_id' != ''
                        GROUP BY config->>'store_id'
                        HAVING COUNT(*) > 1
                    """)
                )
            )

        # ── Check 3: rows where external_store_id is NULL but config has store_id
        unset_rows: list = []
        if has_ext_col:
            unset_rows = list(
                db.execute(
                    text("""
                        SELECT id, tenant_id, config->>'store_id' AS config_store_id
                        FROM   integrations
                        WHERE  provider = 'salla'
                          AND  external_store_id IS NULL
                          AND  config->>'store_id' IS NOT NULL
                    """)
                )
            )

        clean = True

        if dup_rows:
            clean = False
            if has_ext_col:
                print(f"❌  Found {len(dup_rows)} duplicate (provider, external_store_id) group(s):\n")
                for row in dup_rows:
                    print(
                        f"    provider={row.provider}  "
                        f"external_store_id={row.external_store_id}  "
                        f"count={row.cnt}"
                    )
            else:
                print(f"❌  Found {len(dup_rows)} duplicate config store_id group(s) (pre-migration):\n")
                for row in dup_rows:
                    print(f"    store_id={row.store_id}  count={row.cnt}")
            print(
                "\n  ➜  Run:  python scripts/cleanup_salla_duplicates.py --execute\n"
                "     Then re-run this script to confirm clean state.\n"
            )
        else:
            if has_ext_col:
                print("✅  No duplicate (provider, external_store_id) pairs found.")
            else:
                print("✅  No duplicate config store_id groups found (pre-migration).")

        if unset_rows:
            print(
                f"\n⚠️   {len(unset_rows)} Salla integration(s) have external_store_id=NULL "
                f"but store_id in config (will be backfilled by migration 0017):"
            )
            for row in unset_rows[:10]:
                print(
                    f"    integration_id={row.id}  "
                    f"tenant_id={row.tenant_id}  "
                    f"config_store_id={row.config_store_id}"
                )
            if len(unset_rows) > 10:
                print(f"    … and {len(unset_rows) - 10} more")
            print("    These are safe — the migration will fix them automatically.\n")

        if clean:
            print("\n✅  Safe to apply migration 0017.")
        else:
            print("\n❌  NOT safe to apply migration 0017 — resolve duplicates first.")

        return clean

    finally:
        db.close()


if __name__ == "__main__":
    ok = verify()
    sys.exit(0 if ok else 1)
