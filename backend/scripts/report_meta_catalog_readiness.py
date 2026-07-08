#!/usr/bin/env python3
"""Read-only Meta Catalog readiness report (variant-level).

Usage::

    python backend/scripts/report_meta_catalog_readiness.py --tenant-id 1
    python backend/scripts/report_meta_catalog_readiness.py --tenant-id 1 --only-ready
    python backend/scripts/report_meta_catalog_readiness.py --tenant-id 1 --include-meta-live-read
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

from services.meta_catalog_readiness import build_meta_catalog_readiness_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report Meta Catalog readiness per variant (read-only).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--only-ready", action="store_true")
    parser.add_argument("--only-blocked", action="store_true")
    parser.add_argument(
        "--include-meta-live-read",
        action="store_true",
        help="Include Meta Graph GET comparison (no POST).",
    )
    parser.add_argument(
        "--exclude-out-of-stock",
        action="store_true",
        help="Omit out-of-stock variants from the output.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    if args.only_ready and args.only_blocked:
        parser.error("--only-ready and --only-blocked are mutually exclusive")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        report = build_meta_catalog_readiness_report(
            db,
            int(args.tenant_id),
            product_id=args.product_id,
            exclude_out_of_stock=bool(args.exclude_out_of_stock),
            include_meta_live_read=bool(args.include_meta_live_read),
            only_ready=bool(args.only_ready),
            only_blocked=bool(args.only_blocked),
        )
        indent = 2 if args.pretty else None
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=indent))
        print(report.summary_line(), file=sys.stderr)
        if report.error:
            return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
