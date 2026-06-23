#!/usr/bin/env python3
"""Reconcile meta_catalog_published_at against Meta Graph catalog membership.

Dry-run by default — pass ``--apply`` to write stamps.

Usage (Railway shell / local with DATABASE_URL):

    python backend/scripts/reconcile_meta_catalog_stamps.py --tenant 33
    python backend/scripts/reconcile_meta_catalog_stamps.py --tenant 33 --apply
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services.meta_catalog_reconcile import reconcile_meta_catalog_publish_stamps  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Meta catalog publish stamps for a tenant.",
    )
    parser.add_argument("--tenant", type=int, required=True, help="Tenant id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write stamp/clear changes (default: dry-run only)",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        report = reconcile_meta_catalog_publish_stamps(
            db,
            int(args.tenant),
            apply=bool(args.apply),
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not report.error else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
