#!/usr/bin/env python3
"""
backend/scripts/preview_meta_catalog_item_payload.py
────────────────────────────────────────────────────
Read-only preview of the Meta Catalog item payload for one variant.

Does NOT call Meta Graph API. Does NOT write to the database.

Exit codes
──────────
  0 — payload reviewable; no fatal warnings
  1 — fatal warnings (missing retailer_id / price / image / url)

Usage
─────
  python backend/scripts/preview_meta_catalog_item_payload.py \\
    --tenant-id 1 --retailer-id 722682388-591539870

  python backend/scripts/preview_meta_catalog_item_payload.py \\
    --tenant-id 1 --product-id 32 --variant-id 207
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from services.meta_catalog_export import preview_meta_variant_payload  # noqa: E402


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def _load_variant(db, tenant_id: int, *, retailer_id: str | None,
                  product_id: int | None, variant_id: int | None):
    from models import Product, ProductVariant  # noqa: PLC0415

    variant_q = db.query(ProductVariant).filter(ProductVariant.tenant_id == tenant_id)
    if retailer_id:
        variant_q = variant_q.filter(ProductVariant.retailer_id == retailer_id)
    if variant_id is not None:
        variant_q = variant_q.filter(ProductVariant.id == variant_id)
    if product_id is not None:
        variant_q = variant_q.filter(ProductVariant.product_id == product_id)

    variant = variant_q.first()
    if variant is None:
        print("ERROR: variant not found for given filters", file=sys.stderr)
        sys.exit(1)

    parent = (
        db.query(Product)
        .filter(Product.id == variant.product_id, Product.tenant_id == tenant_id)
        .first()
    )
    if parent is None:
        print("ERROR: parent product not found", file=sys.stderr)
        sys.exit(1)
    return parent, variant


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview Meta Catalog payload for one Nahla variant (read-only).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--retailer-id", type=str, default=None)
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--variant-id", type=int, default=None)
    args = parser.parse_args()

    if not args.retailer_id and args.variant_id is None:
        parser.error("Provide --retailer-id or --variant-id (with optional --product-id)")

    _require_db_url()
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        parent, variant = _load_variant(
            db,
            args.tenant_id,
            retailer_id=(args.retailer_id or "").strip() or None,
            product_id=args.product_id,
            variant_id=args.variant_id,
        )
        report = preview_meta_variant_payload(parent, variant)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("fatal") else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
