#!/usr/bin/env python3
"""Read-only A1 order-customer identity reconciliation operator report.

Usage::

    python backend/scripts/report_order_customer_identity_reconciliation.py --tenant-id 42
    python backend/scripts/report_order_customer_identity_reconciliation.py --tenant-id 42 --pretty

Requires DATABASE_URL. Always tenant-scoped — no all-tenant scan.
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

from services.order_customer_identity_reconciliation_report import (  # noqa: E402
    DEFAULT_MAX_SUBJECTS_PER_KIND,
    MAX_SUBJECTS_PER_KIND,
    build_order_customer_identity_reconciliation_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tenant-scoped A1 reconciliation report (read-only JSON).",
    )
    parser.add_argument(
        "--tenant-id",
        type=int,
        required=True,
        help="Required tenant scope. Reports never scan all tenants.",
    )
    parser.add_argument(
        "--max-subjects-per-kind",
        type=int,
        default=DEFAULT_MAX_SUBJECTS_PER_KIND,
        help="Safety cap per subject kind; truncation fails ready_for_validate closed.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    if int(args.tenant_id) <= 0:
        parser.error("--tenant-id must be a positive integer")
    if not 1 <= int(args.max_subjects_per_kind) <= MAX_SUBJECTS_PER_KIND:
        parser.error(
            f"--max-subjects-per-kind must be between 1 and {MAX_SUBJECTS_PER_KIND}"
        )

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        report = build_order_customer_identity_reconciliation_report(
            db,
            int(args.tenant_id),
            max_subjects_per_kind=int(args.max_subjects_per_kind),
        )
        indent = 2 if args.pretty else None
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=indent))
        print(report.summary_line(), file=sys.stderr)
        if report.access_status != "ok":
            return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
