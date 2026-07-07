#!/usr/bin/env python3
"""
backend/scripts/push_one_meta_catalog_item.py
─────────────────────────────────────────────
Guarded one-item Meta Catalog push (dry-run by default).

Default (no --confirm): builds preview payload only — no Graph calls.
With --confirm: GET by retailer_id, then update or create one item.

No DB writes. No full export. No production use without explicit review.
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

from services.meta_catalog_push import (  # noqa: E402
    MetaCatalogPushError,
    push_one_meta_catalog_item,
)


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push one Meta Catalog item (dry-run unless --confirm).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--retailer-id", type=str, required=True)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Perform Graph lookup + create/update (default is dry-run only).",
    )
    args = parser.parse_args()

    retailer_id = (args.retailer_id or "").strip()
    if not retailer_id:
        parser.error("--retailer-id is required and must be non-empty")

    _require_db_url()
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        result = push_one_meta_catalog_item(
            db,
            args.tenant_id,
            retailer_id,
            confirm=bool(args.confirm),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("error") == "preview_fatal":
            return 1
        if args.confirm and not result.get("ok"):
            return 1
        return 0
    except MetaCatalogPushError as exc:
        print(
            json.dumps(
                {"ok": False, "error": exc.code, "message": str(exc), "detail": exc.detail},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
