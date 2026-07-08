#!/usr/bin/env python3
"""Guarded batch Meta Catalog push for ready create items (dry-run by default).

Usage::

    python backend/scripts/push_ready_meta_catalog_items.py --tenant-id 1
    python backend/scripts/push_ready_meta_catalog_items.py --tenant-id 1 --product-id 26 --limit 3
    python backend/scripts/push_ready_meta_catalog_items.py --tenant-id 1 --confirm
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

from services.meta_catalog_push import push_ready_meta_catalog_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch push ready Meta catalog items (dry-run unless --confirm).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Push filtered items one-by-one (default is dry-run only).",
    )
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--include-updates",
        action="store_true",
        help="Also push ready in-stock items with action_needed=update.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue batch after a failed item (default stops on first error).",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        batch = push_ready_meta_catalog_batch(
            db,
            int(args.tenant_id),
            confirm=bool(args.confirm),
            product_id=args.product_id,
            limit=args.limit,
            include_updates=bool(args.include_updates),
            stop_on_first_error=not bool(args.continue_on_error),
        )
        indent = 2 if args.pretty else None
        print(json.dumps(batch, ensure_ascii=False, indent=indent))

        summary = batch.get("summary") or {}
        print(
            "batch "
            f"tenant={args.tenant_id} "
            f"dry_run={batch.get('dry_run')} "
            f"candidates={summary.get('candidate_count', 0)} "
            f"attempted={summary.get('attempted', 0)} "
            f"succeeded={summary.get('succeeded', 0)} "
            f"failed={summary.get('failed', 0)}",
            file=sys.stderr,
        )

        if batch.get("error"):
            return 2
        if not summary.get("candidate_count"):
            return 0
        if args.confirm and summary.get("failed", 0) > 0:
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
