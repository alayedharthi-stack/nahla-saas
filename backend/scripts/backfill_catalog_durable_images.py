#!/usr/bin/env python3
"""Ingest remote catalog display images onto durable R2 URLs.

Active meta_readonly rows only. Never Graph writes. Never create/update/
delete Meta items. Never process ``removed_from_meta``.

Dry-run (default) classifies URLs. Pass --confirm to download + upload +
rewrite extra_metadata.image_url via catalog_media_storage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from core.catalog import (  # noqa: E402
    CATALOG_STATUS_REMOVED_FROM_META,
    catalog_status_of,
)
from core.catalog_image import coerce_image_url, resolve_product_image_url  # noqa: E402
from services.catalog_durable_images import (  # noqa: E402
    _is_active_meta_readonly,
    backfill_active_meta_readonly_images,
)
from services.catalog_media_storage import is_managed_catalog_image_url  # noqa: E402


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def _host_class(url: str) -> str:
    if not url:
        return "missing"
    if is_managed_catalog_image_url(url):
        return "durable_r2"
    host = (urlparse(url).hostname or "").lower()
    if "fbcdn" in host or "scontent" in host or host.endswith("facebook.com"):
        return "meta_cdn"
    return "other_http"


def _preview(db, tenant_id: int, product_id: int | None, limit: int) -> dict:
    from models import Product  # noqa: PLC0415

    q = db.query(Product).filter(Product.tenant_id == int(tenant_id)).order_by(Product.id.asc())
    if product_id is not None:
        q = q.filter(Product.id == int(product_id))
    report = {
        "dry_run": True,
        "tenant_id": int(tenant_id),
        "scanned": 0,
        "durable": 0,
        "meta_cdn": 0,
        "missing": 0,
        "other": 0,
        "ignored_removed": 0,
        "rows": [],
    }
    budget = max(1, int(limit))
    for product in q.all():
        if catalog_status_of(product) == CATALOG_STATUS_REMOVED_FROM_META:
            report["ignored_removed"] += 1
            continue
        if not _is_active_meta_readonly(product):
            continue
        report["scanned"] += 1
        meta = dict(getattr(product, "extra_metadata", None) or {})
        url = resolve_product_image_url(meta=meta, variants=getattr(product, "variants", None) or [])
        url = coerce_image_url(url) or ""
        kind = _host_class(url)
        if kind == "durable_r2":
            report["durable"] += 1
        elif kind == "meta_cdn":
            report["meta_cdn"] += 1
        elif kind == "missing":
            report["missing"] += 1
        else:
            report["other"] += 1
        report["rows"].append({
            "product_id": int(product.id),
            "kind": kind,
            "host": (urlparse(url).hostname or "") if url else "",
        })
        if report["scanned"] >= budget:
            break
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill durable catalog images (dry-run unless --confirm).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=28)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Download + R2 ingest + rewrite DB URLs (default is classify-only).",
    )
    args = parser.parse_args()

    _require_db_url()
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        if args.confirm:
            report = backfill_active_meta_readonly_images(
                db,
                args.tenant_id,
                product_id=args.product_id,
                limit=args.limit,
            )
            report["dry_run"] = False
        else:
            report = _preview(db, args.tenant_id, args.product_id, args.limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report.get("failed") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
