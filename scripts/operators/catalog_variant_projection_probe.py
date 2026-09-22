"""Read-only: what the catalog read tools actually hand the model for a product.

September 2026, Tenant 1: asked for a dress's colours and sizes, the pilot
answered that it did not have them. Two explanations are possible — the
variant facts never reach the projection, or the model's own search matched
nothing — and they need different fixes. This probe answers both from the
production database without changing anything:

* for each product id, the catalog row the read tools project, and the
  ``variant_options`` the tool derives from it;
* for each search query, what ``search_products`` returns, so a query that
  finds nothing is visible as such.

The session is held read-only by PostgreSQL itself. Output is one
``PROBE_RESULT=`` JSON line carrying ids, counts and option values — product
option values are merchant catalog data, never customer data.

Usage::

    DATABASE_URL=... NAHLA_PROBE_PRODUCT_IDS=23,37,38 \\
      NAHLA_PROBE_QUERIES='فستان|الفستان الأول' \\
      python scripts/operators/catalog_variant_projection_probe.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "backend"), str(ROOT / "database")):
    if path not in sys.path:
        sys.path.insert(0, path)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _ids(name: str) -> list[int]:
    raw = str(os.environ.get(name, "") or "").strip()
    return [int(part) for part in raw.replace(",", " ").split() if part]


def _row_view(row: dict, options: tuple) -> dict:
    variant_options, in_stock, total = options
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "in_stock": row.get("in_stock"),
        "stock_qty": row.get("stock_qty"),
        "orderable": row.get("orderable"),
        "has_product_url": bool(row.get("product_url")),
        "variants_key_len": len(row.get("variants") or []),
        "variant_options": variant_options,
        "variants_in_stock": in_stock,
        "variants_total": total,
    }


def main() -> int:
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415
    from models import Product  # noqa: PLC0415
    from modules.ai.commerce_agent_v2.tools import catalog  # noqa: PLC0415

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    tenant_id = int(os.environ.get("NAHLA_PROBE_TENANT_ID", "1"))
    queries = [q for q in str(os.environ.get("NAHLA_PROBE_QUERIES", "") or "").split("|") if q.strip()]
    product_ids = _ids("NAHLA_PROBE_PRODUCT_IDS")

    out: dict = {"tenant_id": tenant_id, "queries": {}, "products": []}
    with Session(engine) as session:
        builder = CatalogContextBuilder(session, tenant_id)
        for product in (session.query(Product)
                        .filter(Product.tenant_id == tenant_id, Product.id.in_(product_ids or [0]))
                        .all()):
            row = builder._format(product)
            out["products"].append(_row_view(row, catalog._variant_options(row.get("variants"))))
        for query in queries:
            try:
                result = builder.search_products(query, limit=10)
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                out["queries"][query] = {"error": type(exc).__name__}
                continue
            rows = result if isinstance(result, list) else list(getattr(result, "products", []) or [])
            out["queries"][query] = {
                "found": len(rows),
                "products": [_row_view(row, catalog._variant_options(row.get("variants")))
                             for row in rows],
            }
    print("PROBE_RESULT=" + json.dumps(out, default=str, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
