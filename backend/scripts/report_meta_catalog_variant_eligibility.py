#!/usr/bin/env python3
"""Read-only Meta Catalog variant eligibility report.

Usage::

    python backend/scripts/report_meta_catalog_variant_eligibility.py --tenant-id 1
    python backend/scripts/report_meta_catalog_variant_eligibility.py --tenant-id 1 --product-id 32
    python backend/scripts/report_meta_catalog_variant_eligibility.py --tenant-id 1 --limit 50
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

from services.meta_catalog_eligibility import build_meta_catalog_eligibility_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report Meta Catalog variant push eligibility (read-only).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--include-out-of-stock",
        action="store_true",
        default=True,
        help="Include out-of-stock variants in the report (default: true).",
    )
    parser.add_argument(
        "--exclude-out-of-stock",
        action="store_true",
        help="Omit out-of-stock variants from the report.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        report = build_meta_catalog_eligibility_report(
            db,
            int(args.tenant_id),
            product_id=args.product_id,
            limit=args.limit,
            include_out_of_stock=not bool(args.exclude_out_of_stock),
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
