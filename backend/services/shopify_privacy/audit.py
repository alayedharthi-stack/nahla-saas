"""
Counts-only data-flow audit behind every bounded privacy assessment.

What an assessment here can — and cannot — say
──────────────────────────────────────────────
The audited scope is ``AUDITED_SCOPE``: the Shopify-owned tables written by the
dormant connection foundation and the product-only catalog mirror (scope
``read_products``). Their schema (``AUDITED_SHOPIFY_SCHEMA``, pinned to
``AUDIT_VERSION``) holds **no structured Shopify customer or order resource**:
no customer, order, address, email or phone column. That is the only "no
data" statement this slice ever records, and only when:

  * the live columns of every audited table equal the pinned snapshot (any
    drift routes to manual review until the audit is re-done and versioned);
  * the **legacy footprint** below is empty platform-wide — any
    ``provider/source = 'shopify'`` integration, receipt, shared-hub row,
    customer profile or settings value (or an unavailable check) blocks the
    conclusion and routes the request to bounded manual review.

It cannot say there is no personal data at all: product titles, options,
SKUs, vendors, location names and media alt text are free-form catalog text
(``catalog_free_text = not_assessed``). Generic Nahlah / WhatsApp customer
data is separate and is not searched (``generic_customer_mapping =
unresolved``), and nothing here asserts that a legal request was fulfilled
(``legal_fulfillment = not_asserted``).

Every function is read-only and returns counts only — never a row, a
config value or a customer value.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from services.shopify_privacy.config import AUDIT_VERSION

AUDITED_SCOPE: Dict[str, str] = {
    "scope": "shopify_product_only_integration_schema",
    "audit_version": AUDIT_VERSION,
    "structured_shopify_customer_or_order_resources": "none_in_audited_schema",
    "catalog_free_text": "not_assessed",
    "generic_customer_mapping": "unresolved",
    "legal_fulfillment": "not_asserted",
}

# Columns of every audited Shopify-owned table at AUDIT_VERSION. None of them
# is a customer, order, address, email or phone field.
AUDITED_SHOPIFY_SCHEMA: Dict[str, frozenset] = {
    "shopify_connections": frozenset({
        "id", "tenant_id", "shop_domain", "shop_gid", "status", "generation", "credential_version",
        "access_token_enc", "access_token_expires_at", "refresh_token_enc", "refresh_token_expires_at",
        "granted_scopes", "connected_by_user_id", "connected_at", "disconnected_at", "disconnect_reason",
        "quarantined_at", "quarantine_reason", "revalidation_requested_at", "reconcile_request_version",
        "reconcile_attempts", "reconcile_next_at", "reconcile_lease_id", "reconcile_lease_expires_at",
        "last_refreshed_at", "last_verified_at", "created_at", "updated_at"}),
    "shopify_shop_leases": frozenset({
        "shop_domain", "lease_id", "purpose", "tenant_id", "acquired_at", "expires_at"}),
    "shopify_oauth_states": frozenset({
        "id", "state_hash", "tenant_id", "actor_user_id", "session_ref_hash", "shop_domain",
        "redirect_uri_fingerprint", "return_path", "connection_generation_at_start", "status", "failure_code",
        "code_enc", "completion_handle_hash", "expires_at", "completion_expires_at", "callback_at",
        "completed_at", "created_at"}),
    "shopify_webhook_events": frozenset({
        "id", "webhook_id", "topic", "shop_domain", "shop_gid", "body_sha256", "header_triggered_at",
        "connection_id", "connection_generation", "outcome", "resolution", "received_at", "processed_at",
        "resolved_at"}),
    "shopify_connection_audit": frozenset({
        "id", "tenant_id", "connection_id", "shop_domain", "action", "actor_user_id", "generation", "detail",
        "created_at"}),
    "shopify_catalog_import_runs": frozenset({
        "id", "tenant_id", "connection_id", "run_seq", "shop_domain", "shop_gid", "generation", "api_version",
        "status", "failure_reason", "inventory_mode", "lease_id", "lease_expires_at", "requested_by_user_id",
        "shop_currency", "expected_products", "traversal_complete", "pages_fetched", "products_count",
        "variants_count", "inventory_items_count", "locations_count", "inventory_levels_count",
        "products_removed", "variants_removed", "stale_skipped", "started_at", "heartbeat_at", "finished_at",
        "committed_at"}),
    "shopify_catalog_staged_records": frozenset({"id", "run_id", "kind", "gid", "parent_gid", "payload"}),
    "shopify_catalog_products": frozenset({
        "id", "tenant_id", "connection_id", "product_gid", "title", "handle", "status", "vendor", "product_type",
        "options", "image_url", "image_alt", "images", "online_store_published_at", "is_gift_card",
        "requires_selling_plan", "combined_listing_role", "has_only_default_variant", "variants_count",
        "source_created_at", "source_updated_at", "lifecycle", "removed_at", "first_seen_run_id",
        "last_seen_run_id", "created_at", "updated_at"}),
    "shopify_catalog_inventory_items": frozenset({
        "id", "tenant_id", "connection_id", "inventory_item_gid", "tracking", "source_updated_at", "lifecycle",
        "removed_at", "first_seen_run_id", "last_seen_run_id", "created_at", "updated_at"}),
    "shopify_catalog_variants": frozenset({
        "id", "tenant_id", "connection_id", "product_id", "inventory_item_id", "variant_gid", "title", "sku",
        "barcode", "position", "price", "compare_at_price", "currency_code", "selected_options", "image_url",
        "image_alt", "image_source", "inventory_policy", "available_for_sale", "requires_components",
        "source_created_at", "source_updated_at", "lifecycle", "removed_at", "first_seen_run_id",
        "last_seen_run_id", "created_at", "updated_at"}),
    "shopify_catalog_locations": frozenset({
        "id", "tenant_id", "connection_id", "location_gid", "name", "is_active", "fulfills_online_orders",
        "lifecycle", "removed_at", "first_seen_run_id", "last_seen_run_id", "created_at", "updated_at"}),
    "shopify_catalog_inventory_levels": frozenset({
        "id", "tenant_id", "connection_id", "inventory_item_id", "location_id", "inventory_level_gid",
        "available", "source_updated_at", "lifecycle", "removed_at", "first_seen_run_id", "last_seen_run_id",
        "created_at", "updated_at"}),
}

# Legacy / generic storage that may hold Shopify-sourced data. Each entry:
# (name, table, required columns, predicate, shop-link predicate or None).
# A missing table or column makes the check "unavailable", which blocks.
_SHOP_PARAMS = "(:shop_id, :shop_domain)"
LEGACY_CHECKS: Tuple[Tuple[str, str, frozenset, str, Optional[str]], ...] = (
    ("integrations", "integrations", frozenset({"provider", "tenant_id", "external_store_id", "config"}),
     "lower(t.provider) = 'shopify'",
     f"(t.external_store_id IN {_SHOP_PARAMS} OR t.config->>'store_id' IN {_SHOP_PARAMS}"
     " OR lower(t.config->>'store_domain') = :shop_domain)"),
    ("webhook_events", "webhook_events", frozenset({"provider", "tenant_id", "store_id"}),
     "lower(t.provider) = 'shopify'", f"t.store_id IN {_SHOP_PARAMS}"),
    ("products", "products", frozenset({"source", "tenant_id"}), "lower(t.source) = 'shopify'", None),
    ("orders", "orders", frozenset({"source", "tenant_id"}), "lower(t.source) = 'shopify'", None),
    ("external_customer_profiles", "external_customer_profiles",
     frozenset({"tenant_id", "integration_connection_id"}),
     "EXISTS (SELECT 1 FROM integrations i WHERE i.id = t.integration_connection_id"
     " AND lower(i.provider) = 'shopify')", None),
    ("customer_name_provenance", "customer_name_provenance",
     frozenset({"tenant_id", "source", "last_attempt_source"}),
     "(lower(coalesce(t.source, '')) LIKE 'shopify%' OR lower(coalesce(t.last_attempt_source, '')) LIKE 'shopify%')",
     None),
    ("tenant_settings", "tenant_settings", frozenset({"tenant_id", "store_settings"}),
     "(coalesce(t.store_settings->>'shopify_access_token', '') <> ''"
     " OR coalesce(t.store_settings->>'shopify_shop_domain', '') <> '')",
     "lower(t.store_settings->>'shopify_shop_domain') = :shop_domain"),
)
# Tables a check reads besides its own.
_LEGACY_DEPENDENCIES = {"external_customer_profiles": ("integrations",)}


def _columns(insp, table: str) -> frozenset:
    """Column names of a table the caller already found in the schema."""
    return frozenset(c["name"] for c in insp.get_columns(table))


def schema_drift(db: Session) -> Optional[str]:
    """None when every audited table exists with exactly the pinned columns;
    otherwise a fixed reason (the audit must be re-done and re-versioned)."""
    insp = inspect(db.connection())
    names = set(insp.get_table_names())
    for table, expected in AUDITED_SHOPIFY_SCHEMA.items():
        if table not in names:
            return "audit_table_missing"
        if _columns(insp, table) != expected:
            return "audit_schema_drift"
    return None


def legacy_footprint(db: Session, *, tenant_id: Optional[int], shop_domain: str, shop_id: int) -> Dict[str, Any]:
    """Counts of legacy Shopify-sourced rows: platform-wide (``total``), for
    the tenant (``tenant``) and linked to the shop identity (``shop``, where
    a link column exists). ``unavailable`` lists checks that could not run."""
    insp = inspect(db.connection())
    names = set(insp.get_table_names())
    params = {"tenant_id": tenant_id, "shop_domain": shop_domain, "shop_id": str(shop_id)}
    out: Dict[str, Any] = {"total": {}, "tenant": {}, "shop": {}, "unavailable": []}
    for name, table, required, predicate, shop_predicate in LEGACY_CHECKS:
        deps = (table,) + _LEGACY_DEPENDENCIES.get(name, ())
        if any(d not in names for d in deps) or not required <= _columns(insp, table):
            out["unavailable"].append(name)
            continue
        shop_count = f"count(*) FILTER (WHERE {shop_predicate})" if shop_predicate else "NULL"
        row = db.execute(text(
            f"SELECT count(*) AS total, count(*) FILTER (WHERE t.tenant_id = :tenant_id) AS tenant,"
            f" {shop_count} AS shop FROM {table} t WHERE {predicate}"), params).mappings().one()
        out["total"][name] = int(row["total"])
        out["tenant"][name] = int(row["tenant"])
        if shop_predicate:
            out["shop"][name] = int(row["shop"])
    return out


def footprint_blocks(footprint: Dict[str, Any]) -> bool:
    """Any legacy evidence anywhere, or any check that could not run."""
    return bool(footprint.get("unavailable")) or any(v > 0 for v in footprint.get("total", {}).values())


def _count(db: Session, sql: str, **params) -> int:
    return int(db.execute(text(sql), params).scalar_one())


def shop_rows(db: Session, *, shop_domain: str, shop_gid: str) -> Dict[str, int]:
    """Shopify-owned rows of any tenant that reference the signed shop identity."""
    p = {"d": shop_domain, "g": shop_gid}
    return {
        "shopify_connections": _count(db, "SELECT count(*) FROM shopify_connections WHERE shop_domain = :d OR shop_gid = :g", **p),
        "shopify_oauth_states": _count(db, "SELECT count(*) FROM shopify_oauth_states WHERE shop_domain = :d", **p),
        "shopify_shop_leases": _count(db, "SELECT count(*) FROM shopify_shop_leases WHERE shop_domain = :d", **p),
        "shopify_webhook_events": _count(
            db, "SELECT count(*) FROM shopify_webhook_events WHERE shop_domain = :d OR shop_gid = :g", **p),
        "shopify_connection_audit": _count(
            db, "SELECT count(*) FROM shopify_connection_audit WHERE shop_domain = :d", **p),
        "shopify_catalog_import_runs": _count(
            db, "SELECT count(*) FROM shopify_catalog_import_runs WHERE shop_domain = :d OR shop_gid = :g", **p),
    }


# Provider-owned rows of exactly one (connection, tenant) that a shop redaction
# would erase on approval, in foreign-key-safe deletion order.
MIRROR_DELETE_ORDER = (
    "shopify_catalog_inventory_levels",
    "shopify_catalog_variants",
    "shopify_catalog_inventory_items",
    "shopify_catalog_locations",
    "shopify_catalog_products",
    "shopify_catalog_staged_records",
    "shopify_catalog_import_runs",
)
_RUNS_OF = "SELECT r.id FROM shopify_catalog_import_runs r WHERE r.connection_id = :c AND r.tenant_id = :t"


def mirror_counts(db: Session, *, connection_id: int, tenant_id: int) -> Dict[str, int]:
    p = {"c": int(connection_id), "t": int(tenant_id)}
    out: Dict[str, int] = {}
    for table in MIRROR_DELETE_ORDER:
        if table == "shopify_catalog_staged_records":
            out[table] = _count(db, f"SELECT count(*) FROM {table} WHERE run_id IN ({_RUNS_OF})", **p)
        else:
            out[table] = _count(db, f"SELECT count(*) FROM {table} WHERE connection_id = :c AND tenant_id = :t", **p)
    return out


def shop_redaction_inventory(db: Session, *, connection_id: int, tenant_id: int, shop_domain: str,
                             shop_gid: str, footprint: Dict[str, Any]) -> Dict[str, Any]:
    """Counts-only proposed action for a shop redaction. Nothing is decided
    here: ``delete_on_approval`` is what the fixture-tested cleanup helper
    would erase once a policy is approved; everything else stays pending
    owner policy."""
    p = {"c": int(connection_id), "t": int(tenant_id), "d": shop_domain, "g": shop_gid}
    retained = {
        "shopify_connections": 1,
        "shopify_connection_audit": _count(
            db, "SELECT count(*) FROM shopify_connection_audit WHERE connection_id = :c"
                " OR (tenant_id = :t AND shop_domain = :d)", **p),
        "shopify_webhook_events": _count(
            db, "SELECT count(*) FROM shopify_webhook_events WHERE connection_id = :c OR shop_domain = :d"
                " OR shop_gid = :g", **p),
        "shopify_oauth_states": _count(db, "SELECT count(*) FROM shopify_oauth_states WHERE shop_domain = :d", **p),
        "shopify_shop_leases": _count(db, "SELECT count(*) FROM shopify_shop_leases WHERE shop_domain = :d", **p),
        "shopify_privacy_requests": _count(
            db, "SELECT count(*) FROM shopify_privacy_requests WHERE shop_domain = :d OR shop_gid = :g", **p),
    }
    return {
        **AUDITED_SCOPE,
        "action": "shop_redaction_proposal",
        "delete_on_approval": mirror_counts(db, connection_id=connection_id, tenant_id=tenant_id),
        "retained_pending_policy": retained,
        "legacy_shopify_footprint": _footprint_summary(footprint),
        "blocked_on": ["destructive_erasure_policy", "ownership_tombstone_retention_policy",
                       "audit_and_receipt_retention_policy"],
    }


def customer_assessment(*, topic: str, footprint: Dict[str, Any], matched: bool) -> Dict[str, Any]:
    blocked_on = ["generic_customer_data_mapping_policy", "catalog_free_text_personal_data_policy"]
    if topic == "customers/data_request":
        blocked_on.append("merchant_response_policy")
    return {
        **AUDITED_SCOPE,
        "action": "customer_request_assessment",
        "shopify_connection": "matched" if matched else "none",
        "legacy_shopify_footprint": _footprint_summary(footprint),
        "blocked_on": blocked_on,
    }


def shop_assessment(*, footprint: Dict[str, Any], rows: Dict[str, int]) -> Dict[str, Any]:
    return {
        **AUDITED_SCOPE,
        "action": "shop_request_assessment",
        "shopify_connection": "none",
        "shopify_owned_rows_for_shop": rows,
        "legacy_shopify_footprint": _footprint_summary(footprint),
        "blocked_on": ["audit_and_receipt_retention_policy"],
    }


def _footprint_summary(footprint: Dict[str, Any]) -> Dict[str, Any]:
    return {k: footprint.get(k) for k in ("total", "tenant", "shop", "unavailable")}


def in_flight_lifecycle(db: Session, *, connection_id: Optional[int], shop_domain: str,
                        now: datetime) -> Optional[str]:
    """A fixed reason when an authorization, a credential call or an import
    for this shop may still change it; None when nothing is in flight."""
    p = {"d": shop_domain, "now": now, "c": connection_id}
    if _count(db, "SELECT count(*) FROM shopify_oauth_states WHERE shop_domain = :d AND ("
                  " (status IN ('pending', 'callback_received') AND expires_at > :now)"
                  " OR (status IN ('callback_received', 'exchanging') AND completion_expires_at > :now))", **p):
        return "in_flight_authorization"
    if _count(db, "SELECT count(*) FROM shopify_shop_leases WHERE shop_domain = :d AND expires_at > :now", **p):
        return "shop_lease_held"
    if connection_id is not None and _count(
            db, "SELECT count(*) FROM shopify_catalog_import_runs WHERE connection_id = :c"
                " AND status = 'running' AND lease_expires_at > :now", **p):
        return "import_running"
    return None


def legacy_table_names() -> List[str]:
    return sorted({t for _n, t, _r, _p, _s in LEGACY_CHECKS} | {"integrations"})
