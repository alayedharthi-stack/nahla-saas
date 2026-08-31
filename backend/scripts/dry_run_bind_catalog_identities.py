#!/usr/bin/env python3
"""Read-only dry-run: bind Graph catalog items by literal retailer_id only.

Never DELETE / POST / PATCH Graph. Never writes memberships.
--confirm is ignored. Name matching is forbidden.

Usage:
  python backend/scripts/dry_run_bind_catalog_identities.py \\
    --tenant 1 --catalog 871742015873294 --graph-json items.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO / "database"))
sys.path.insert(0, str(_REPO))


def _load_graph_items(path: str) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        rows = raw.get("items") or raw.get("data") or raw.get("products") or []
    else:
        rows = raw
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("retailer_id") or row.get("product_retailer_id") or "").strip()
        if not rid:
            continue
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", type=int, required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--graph-json", required=True, help="Graph catalog products dump (read-only)")
    parser.add_argument("--confirm", action="store_true", help="unused; this script never writes")
    args = parser.parse_args()
    if args.confirm:
        print("confirm is ignored: this dry-run never writes", file=sys.stderr)

    from sqlalchemy import text
    from core.database import SessionLocal
    from database.models import Product, ProductVariant, MetaCatalogMembership
    from services.salla_variant_catalog_identity import literal_bind_plan

    graph_items = _load_graph_items(args.graph_json)
    db = SessionLocal()
    report = {
        "tenant_id": args.tenant,
        "catalog_id": args.catalog,
        "writes": 0,
        "would_bind": [],
        "quarantine": [],
        "exact": [],
        "items": [],
    }
    try:
        try:
            db.execute(text("SET TRANSACTION READ ONLY"))
        except Exception as exc:
            print(f"read_only_unavailable:{type(exc).__name__}", file=sys.stderr)
        products = {
            p.id: p
            for p in db.query(Product).filter(Product.tenant_id == int(args.tenant)).all()
        }
        variants = db.query(ProductVariant).filter(ProductVariant.tenant_id == int(args.tenant)).all()
        by_ext: dict[str, Product] = {}
        by_product: dict[int, list] = {}
        for product in products.values():
            ext = str(product.external_id or "").strip()
            if ext and ext not in by_ext:
                by_ext[ext] = product
            native = str(getattr(product, "meta_retailer_id", None) or "").strip()
            if native.startswith("nahla_p_") and native not in by_ext:
                by_ext[native] = product
        for row in variants:
            by_product.setdefault(int(row.product_id), []).append(row)
        mems = {
            str(m.retailer_id): m
            for m in db.query(MetaCatalogMembership).filter(
                MetaCatalogMembership.tenant_id == int(args.tenant),
                MetaCatalogMembership.catalog_id == str(args.catalog),
            ).all()
        }
        for item in graph_items:
            rid = str(item.get("retailer_id") or "").strip()
            ext = rid.split("-", 1)[0] if rid else ""
            product = by_ext.get(rid) or by_ext.get(ext)
            mem = mems.get(rid)
            if product is None:
                plan = {
                    "retailer_id": rid,
                    "meta_item_id": str(item.get("id") or ""),
                    "class": "AMBIGUOUS",
                    "would_bind": False,
                    "quarantine": True,
                    "reason": "no_local_product_for_literal_retailer_id",
                    "name_match_used": False,
                }
            else:
                plan = literal_bind_plan(
                    graph_item=item,
                    product=product,
                    variants=by_product.get(int(product.id), []),
                    membership_meta_item_id=str(getattr(mem, "meta_item_id", "") or "") if mem else "",
                )
                plan["product_id"] = product.id
            report["items"].append(plan)
            if plan.get("would_bind"):
                report["would_bind"].append(plan)
            elif plan.get("class") == "EXACT_LOCAL_IDENTITY":
                report["exact"].append(plan)
            else:
                report["quarantine"].append(plan)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
