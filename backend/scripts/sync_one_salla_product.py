#!/usr/bin/env python3
"""
backend/scripts/sync_one_salla_product.py
─────────────────────────────────────────
Guarded one-product Salla sync (dry-run by default).

Default (no --confirm): reads DB + Salla live, prints diff JSON — no DB writes.
With --confirm: upserts exactly one product (+ variants) via StoreSyncService.

No full sync. No get_products loop. No --all.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from models import Product  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def _resolve_external_id(db, tenant_id: int, external_id: str, nahla_product_id: int | None) -> str:
    ext = (external_id or "").strip()
    if nahla_product_id is not None:
        product = (
            db.query(Product)
            .filter(Product.tenant_id == tenant_id, Product.id == nahla_product_id)
            .first()
        )
        if product is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "nahla_product_not_found",
                        "message": f"Product id={nahla_product_id} not found for tenant={tenant_id}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        if not (product.external_id or "").strip():
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "missing_external_id",
                        "message": f"Product id={nahla_product_id} has no external_id",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        return str(product.external_id).strip()
    return ext


async def _run(args: argparse.Namespace) -> int:
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        external_id = _resolve_external_id(
            db,
            args.tenant_id,
            args.external_id or "",
            args.nahla_product_id,
        )
        service = StoreSyncService(db, tenant_id=args.tenant_id)
        result = await service.sync_one_product_by_external_id(
            external_id,
            dry_run=not args.confirm,
            nahla_product_id=args.nahla_product_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result.get("ok"):
            return 1
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync one Salla product into Nahla (dry-run unless --confirm).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--external-id", type=str)
    id_group.add_argument("--nahla-product-id", type=int)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Write one product to DB (default is dry-run only).",
    )
    args = parser.parse_args()

    if args.tenant_id <= 0:
        parser.error("--tenant-id must be a positive integer")

    _require_db_url()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
