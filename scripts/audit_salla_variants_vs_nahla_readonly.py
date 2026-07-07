#!/usr/bin/env python3
"""
scripts/audit_salla_variants_vs_nahla_readonly.py
──────────────────────────────────────────────────
Read-only audit: compare Salla product variants with Nahla ``product_variants``.

Does NOT write to the database. Does NOT run sync or Meta push.

Exit codes
──────────
  0 — no FAIL findings (WARN allowed)
  1 — at least one FAIL

Usage
─────
  python scripts/audit_salla_variants_vs_nahla_readonly.py --tenant-id 1 --external-id 722682388
  python scripts/audit_salla_variants_vs_nahla_readonly.py --tenant-id 1 --product-id 32
  python scripts/audit_salla_variants_vs_nahla_readonly.py --tenant-id 1 --limit 5

Requires DATABASE_URL and a valid Salla integration for the tenant.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SAMPLE_LIMIT = 10


@dataclass
class Finding:
    status: str
    code: str
    message: str
    salla_variant_id: Optional[str] = None


@dataclass
class AuditReport:
    findings: List[Finding] = field(default_factory=list)

    def add(self, status: str, code: str, message: str,
            salla_variant_id: Optional[str] = None) -> None:
        self.findings.append(Finding(status, code, message, salla_variant_id))

    @property
    def has_fail(self) -> bool:
        return any(f.status == FAIL for f in self.findings)

    @property
    def counts(self) -> Dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0}
        for f in self.findings:
            out[f.status] = out.get(f.status, 0) + 1
        return out


def coerce_price(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        f = float(value)
        if f == int(f):
            return str(int(f))
        return str(f)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None


def extract_salla_variant_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one raw Salla variant dict for comparison (read-only)."""
    vid = str(raw.get("id") or "").strip()
    price = coerce_price(raw.get("price"))
    sale_price = coerce_price(raw.get("sale_price"))
    regular_price = coerce_price(raw.get("regular_price"))
    qty = raw.get("quantity")
    if qty is None:
        qty = raw.get("stock_quantity")
    try:
        stock_quantity = int(qty) if qty is not None else None
    except (TypeError, ValueError):
        stock_quantity = None
    available = raw.get("available")
    if available is None and stock_quantity is not None:
        available = stock_quantity > 0
    options = raw.get("options")
    if not isinstance(options, dict):
        rov = raw.get("related_option_values") or raw.get("related_options")
        if isinstance(rov, list) and rov:
            options = rov
    image = (
        raw.get("image_url")
        or raw.get("image")
        or raw.get("main_image")
        or raw.get("thumbnail")
    )
    if isinstance(image, dict):
        image = image.get("url")
    return {
        "salla_variant_id": vid,
        "sku": (raw.get("sku") or "").strip() or None,
        "price": price,
        "sale_price": sale_price,
        "regular_price": regular_price,
        "stock_quantity": stock_quantity,
        "in_stock": bool(available) if available is not None else None,
        "options": options,
        "image_url": str(image).strip() if image else None,
    }


def extract_nahla_variant_row(row: Any) -> Dict[str, Any]:
    meta = getattr(row, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "nahla_variant_id": getattr(row, "id", None),
        "salla_variant_id": (getattr(row, "salla_variant_id", None) or "").strip() or None,
        "sku": (getattr(row, "sku", None) or "").strip() or None,
        "price": coerce_price(getattr(row, "price", None)),
        "sale_price": coerce_price(meta.get("sale_price")),
        "regular_price": coerce_price(meta.get("regular_price")),
        "stock_quantity": getattr(row, "stock_quantity", None),
        "in_stock": bool(getattr(row, "in_stock", True)),
        "options": getattr(row, "options", None) or meta.get("options"),
        "option_summary": (getattr(row, "option_summary", None) or "").strip() or None,
        "image_url": (getattr(row, "image_url", None) or "").strip() or None,
        "retailer_id": (getattr(row, "retailer_id", None) or "").strip() or None,
        "is_default": bool(getattr(row, "is_default", False)),
    }


def _options_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return len(value) > 0
    return bool(str(value).strip())


def _prices_differ(a: Optional[str], b: Optional[str]) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    try:
        return float(a) != float(b)
    except (TypeError, ValueError):
        return str(a) != str(b)


def compare_product_variants(
    salla_rows: List[Dict[str, Any]],
    nahla_rows: List[Dict[str, Any]],
) -> AuditReport:
    """Compare Salla vs Nahla variant sets for one parent product."""
    report = AuditReport()
    salla_by_id = {
        r["salla_variant_id"]: r
        for r in salla_rows
        if r.get("salla_variant_id")
    }
    nahla_by_sid = {
        r["salla_variant_id"]: r
        for r in nahla_rows
        if r.get("salla_variant_id")
    }
    nahla_defaults = [r for r in nahla_rows if r.get("is_default")]

    if not salla_rows and nahla_defaults and len(nahla_rows) <= 1:
        report.add(PASS, "no_salla_variants_simple_product",
                   "Salla returned no variants; Nahla has synthetic default only.")
        return report

    for sid, s_row in salla_by_id.items():
        n_row = nahla_by_sid.get(sid)
        if n_row is None:
            report.add(
                FAIL, "missing_in_nahla",
                f"Salla variant {sid} not found in Nahla product_variants.",
                sid,
            )
            continue
        report.add(PASS, "present_in_nahla", f"Variant {sid} exists in Nahla.", sid)

        if (s_row.get("sku") or "") != (n_row.get("sku") or ""):
            if s_row.get("sku") and n_row.get("sku"):
                report.add(WARN, "sku_mismatch",
                           f"SKU differs for {sid}: salla={s_row.get('sku')!r} "
                           f"nahla={n_row.get('sku')!r}", sid)

        if _prices_differ(s_row.get("price"), n_row.get("price")):
            report.add(FAIL, "price_mismatch",
                       f"Price differs for {sid}: salla={s_row.get('price')} "
                       f"nahla={n_row.get('price')}", sid)

        for field_name in ("sale_price", "regular_price"):
            s_val = s_row.get(field_name)
            n_val = n_row.get(field_name)
            if s_val and not n_val:
                report.add(WARN, f"{field_name}_missing_in_nahla",
                           f"Salla has {field_name}={s_val} but Nahla variant lacks it.", sid)

        s_stock = s_row.get("stock_quantity")
        n_stock = n_row.get("stock_quantity")
        if s_stock is not None and n_stock is not None and int(s_stock) != int(n_stock):
            report.add(FAIL, "stock_mismatch",
                       f"Stock differs for {sid}: salla={s_stock} nahla={n_stock}", sid)

        s_in = s_row.get("in_stock")
        n_in = n_row.get("in_stock")
        if s_in is not None and s_in != n_in:
            report.add(WARN, "in_stock_mismatch",
                       f"in_stock differs for {sid}: salla={s_in} nahla={n_in}", sid)

        if _options_present(s_row.get("options")) and not _options_present(n_row.get("options")):
            if not n_row.get("option_summary"):
                report.add(WARN, "options_missing_in_nahla",
                           f"Salla variant {sid} has options but Nahla variant has none.", sid)

        if s_row.get("image_url") and not n_row.get("image_url"):
            report.add(WARN, "image_missing_in_nahla",
                       f"Salla variant {sid} has image_url but Nahla variant does not.", sid)

    for sid, n_row in nahla_by_sid.items():
        if sid in salla_by_id:
            continue
        if not n_row.get("in_stock"):
            report.add(PASS, "nahla_soft_pruned",
                       f"Nahla variant {sid} absent from Salla but in_stock=false.", sid)
            continue
        report.add(FAIL, "stale_in_nahla",
                   f"Nahla variant {sid} not in Salla payload and still in_stock.", sid)

    if not report.findings:
        report.add(PASS, "no_variants", "No variants to compare on either side.")
    return report


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def _load_products(db, tenant_id: int, *, product_id: Optional[int],
                   external_id: Optional[str], limit: int) -> List[Any]:
    from models import Product  # noqa: PLC0415

    q = db.query(Product).filter(Product.tenant_id == tenant_id)
    if product_id is not None:
        q = q.filter(Product.id == product_id)
    elif external_id is not None:
        q = q.filter(Product.external_id == str(external_id))
    else:
        q = q.filter(Product.source == "salla").order_by(Product.id).limit(limit)
    return q.all()


async def _audit_one_product(db, adapter, product, report: AuditReport) -> None:
    from models import ProductVariant  # noqa: PLC0415

    ext_id = (product.external_id or "").strip()
    print(f"\n── Product nahla_id={product.id} external_id={ext_id!r} title={product.title!r}")

    nahla_variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product.id)
        .all()
    )
    nahla_rows = [extract_nahla_variant_row(v) for v in nahla_variants]
    print(f"Nahla variants: {len(nahla_rows)}")
    for row in nahla_rows[:SAMPLE_LIMIT]:
        print("  ", json.dumps(row, ensure_ascii=False, default=str))

    if not ext_id:
        report.add(WARN, "no_external_id",
                   f"Product {product.id} has no external_id — cannot query Salla.")
        return

    salla_raw: List[Dict[str, Any]] = []
    try:
        salla_raw = await adapter.get_raw_variants(ext_id)
    except Exception as exc:  # noqa: BLE001
        report.add(WARN, "salla_variants_fetch_error",
                   f"get_raw_variants failed for {ext_id}: {exc}")

    if not salla_raw:
        print("WARN: Salla dedicated /variants endpoint returned no rows "
              "(product may be simple SKU or list payload omitted variants).")
        report.add(WARN, "salla_variants_empty",
                   f"No variants from Salla /products/{ext_id}/variants.")

    salla_rows = [extract_salla_variant_row(v) for v in salla_raw if isinstance(v, dict)]
    print(f"Salla variants: {len(salla_rows)}")
    for row in salla_rows[:SAMPLE_LIMIT]:
        print("  ", json.dumps(row, ensure_ascii=False, default=str))

    product_report = compare_product_variants(salla_rows, nahla_rows)
    for f in product_report.findings:
        report.add(f.status, f.code, f.message, f.salla_variant_id)


async def _run_async(args: argparse.Namespace) -> int:
    from core.database import SessionLocal  # noqa: PLC0415
    from store_adapters.salla_adapter import SallaAdapter  # noqa: PLC0415
    from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

    _require_db_url()
    db = SessionLocal()
    report = AuditReport()
    try:
        intg = pick_active_salla_integration(db, args.tenant_id)
        if not intg:
            print(f"FAIL: No active Salla integration for tenant_id={args.tenant_id}")
            return 1
        cfg = intg.config or {}
        if not cfg.get("api_key"):
            print(f"FAIL: Salla integration {intg.id} has no api_key")
            return 1

        adapter = SallaAdapter(
            api_key=cfg.get("api_key", ""),
            store_id=cfg.get("store_id", "") or intg.external_store_id or "",
            refresh_token=cfg.get("refresh_token", ""),
            tenant_id=args.tenant_id,
            integration_id=intg.id,
        )
        print("Salla variant audit (read-only)")
        print(f"tenant_id={args.tenant_id} integration_id={intg.id} "
              f"store_id={cfg.get('store_id') or intg.external_store_id}")

        products = _load_products(
            db, args.tenant_id,
            product_id=args.product_id,
            external_id=args.external_id,
            limit=args.limit,
        )
        if not products:
            print("FAIL: No matching products in Nahla.")
            return 1

        for product in products:
            await _audit_one_product(db, adapter, product, report)

    finally:
        db.close()

    counts = report.counts
    print("\n=== Summary ===")
    print(f"PASS={counts[PASS]} WARN={counts[WARN]} FAIL={counts[FAIL]}")
    for f in report.findings:
        sid = f" variant={f.salla_variant_id}" if f.salla_variant_id else ""
        print(f"[{f.status}] {f.code}{sid}: {f.message}")

    return 1 if report.has_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit: Salla variants vs Nahla product_variants.",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--product-id", type=int, default=None,
                        help="Nahla products.id")
    parser.add_argument("--external-id", type=str, default=None,
                        help="Salla product external_id")
    parser.add_argument("--limit", type=int, default=5,
                        help="Max Salla products when no product filter (default 5)")
    args = parser.parse_args()
    if args.product_id is None and args.external_id is None and args.limit < 1:
        parser.error("--limit must be >= 1 when scanning multiple products")
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
