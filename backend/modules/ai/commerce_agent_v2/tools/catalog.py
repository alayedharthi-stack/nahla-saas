"""Tenant-bound wrappers around the existing catalog domain service."""
from __future__ import annotations

from typing import Any

from agents import RunContextWrapper, function_tool

from core.store_knowledge import CatalogContextBuilder
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CatalogSearchResult,
    EvidenceRecord,
    ProductDetailsResult,
    ProductSnapshot,
)
from modules.ai.security.tenant_isolation import TenantIsolationLayer


def _product_evidence(row: dict[str, Any]) -> tuple[ProductSnapshot, EvidenceRecord]:
    product_id = int(row["id"])
    evidence_ref = f"catalog:product:{product_id}"
    fields = {
        "product_id": product_id,
        "external_id": row.get("external_id"),
        "title": str(row.get("title") or ""),
        "description": str(row.get("description") or ""),
        "price": row.get("price"),
        "sale_price": row.get("sale_price"),
        "regular_price": row.get("regular_price"),
        "in_stock": row.get("in_stock"),
        "stock_quantity": row.get("stock_qty"),
        "image_url": str(row.get("image_url") or ""),
        "product_url": str(row.get("product_url") or ""),
        "orderable": bool(row.get("orderable")),
    }
    evidence = EvidenceRecord(
        ref=evidence_ref,
        source="catalog_product",
        source_id=str(product_id),
        fields=fields,
        provenance={
            "service": "core.store_knowledge.CatalogContextBuilder",
            "record": "products",
            "freshness": "synced_catalog",
        },
    )
    snapshot = ProductSnapshot(
        product_id=product_id,
        external_id=(str(row.get("external_id")) if row.get("external_id") else None),
        title=fields["title"],
        description=fields["description"],
        price=(str(row.get("price")) if row.get("price") not in (None, "") else None),
        sale_price=(
            str(row.get("sale_price")) if row.get("sale_price") not in (None, "") else None
        ),
        regular_price=(
            str(row.get("regular_price"))
            if row.get("regular_price") not in (None, "")
            else None
        ),
        in_stock=row.get("in_stock"),
        stock_quantity=row.get("stock_qty"),
        image_url=fields["image_url"],
        product_url=fields["product_url"],
        orderable=fields["orderable"],
        evidence_ref=evidence_ref,
    )
    return snapshot, evidence


def _assert_catalog_rows_belong_to_tenant(
    context: CommerceAgentContext,
    rows: list[dict[str, Any]],
) -> None:
    from models import Product

    ids = [int(row["id"]) for row in rows]
    if not ids:
        return
    db_rows = (
        context.db.query(Product)
        .filter(Product.id.in_(ids), Product.tenant_id == context.tenant_id)
        .all()
    )
    found = {int(row.id) for row in db_rows}
    if found != set(ids):
        raise RuntimeError("catalog_service_returned_out_of_scope_product")
    for row in db_rows:
        TenantIsolationLayer.assert_belongs(row, context.tenant_context)


@function_tool(timeout=8.0)
async def search_products(
    run_context: RunContextWrapper[CommerceAgentContext],
    query: str,
    limit: int,
) -> CatalogSearchResult:
    """Search the current merchant's synced catalog.

    Use an empty query to browse the merchant's top available products. Limit
    must be between 1 and 10. Tenant identity is taken only from trusted context.
    """
    context = run_context.context
    context.assert_scope()
    bounded_limit = max(1, min(int(limit), 10))
    catalog = CatalogContextBuilder(context.db, context.tenant_id)
    clean_query = str(query or "").strip()
    if clean_query:
        domain_result = catalog.search_products(
            clean_query,
            limit=bounded_limit,
            include_non_orderable_facts=True,
        )
        rows = [
            *list(domain_result.products or []),
            *list(domain_result.catalog_fact_products or []),
        ]
    else:
        rows = list(catalog.get_top_products(limit=bounded_limit) or [])
    _assert_catalog_rows_belong_to_tenant(context, rows)
    snapshots: list[ProductSnapshot] = []
    evidence: list[EvidenceRecord] = []
    for row in rows:
        snapshot, record = _product_evidence(row)
        snapshots.append(snapshot)
        evidence.append(record)
    context.authorize_products([item.product_id for item in snapshots])
    context.register_evidence(evidence)
    if not snapshots:
        return CatalogSearchResult(
            status="not_found",
            failure_reason="no_catalog_product_matched",
        )
    return CatalogSearchResult(status="ok", products=snapshots, evidence=evidence)


@function_tool(timeout=8.0)
async def get_product_details(
    run_context: RunContextWrapper[CommerceAgentContext],
    product_id: int,
) -> ProductDetailsResult:
    """Get exact details for a product returned earlier by search_products."""
    context = run_context.context
    context.assert_scope()
    context.require_authorized_product(product_id)
    row = CatalogContextBuilder(context.db, context.tenant_id).get_by_id(int(product_id))
    if row is None:
        return ProductDetailsResult(
            status="not_found",
            failure_reason="product_not_found_in_tenant_catalog",
        )
    _assert_catalog_rows_belong_to_tenant(context, [row])
    snapshot, evidence = _product_evidence(row)
    context.register_evidence([evidence])
    return ProductDetailsResult(status="ok", product=snapshot, evidence=[evidence])
