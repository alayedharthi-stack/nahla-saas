"""
trusted_context_brain_projection.py
───────────────────────────────────
Scoped structured projection from TrustedContextSnapshot into Brain/Compose.

Read-only: no DB, no loader calls, no snapshot mutation, no customer prose.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .contract import TrustedContextSnapshot, TrustedDomain, TrustedFact

SCHEMA_VERSION = "1"
SURFACE = "trusted_context_brain_projection"

_PRODUCT_IDENTITY_KEYS: Tuple[str, ...] = (
    "product_id",
    "variant_id",
    "title",
    "price",
    "sale_price",
    "regular_price",
    "currency",
    "available",
    "in_stock",
    "stock_status",
    "product_url",
    "image_url",
    "cart_url",
)

_ORDER_KEYS: Tuple[str, ...] = (
    "order_id",
    "external_id",
    "status",
    "order_status",
    "missing_fields",
    "line_items_count",
    "catalog_order_submitted",
    "item_count",
    "catalog_line_items_authoritative",
    "catalog_checkout_total",
    "product_id",
    "payment_receipt_received",
    "awaiting_payment_receipt",
)

_SHIPMENT_KEYS: Tuple[str, ...] = (
    "order_status",
    "tracking_number",
    "shipment_evidence_source",
    "tracking_present",
)

_PAYMENT_KEYS: Tuple[str, ...] = (
    "payment_evidence_status",
    "payment_receipt_received",
    "awaiting_payment_receipt",
)

_CUSTOMER_KEYS: Tuple[str, ...] = (
    "customer_id",
    "operational_name",
    "name_status",
    "city",
    "short_address",
    "maps_url",
    "accepted_delivery_address",
)

_MERCHANT_CAPABILITY_KEYS: Tuple[str, ...] = (
    "surface",
    "kind",
    "payments",
    "shipping",
    "freshness",
)

_CATALOG_BUNDLE_KEYS: Tuple[str, ...] = (
    "catalog:product_sale_offer",
)

# Pack A3 — policy existence only (status + doc_ref). Never project prose bodies.
_POLICY_KIND_KEYS: Tuple[str, ...] = (
    "return_policy",
    "refund_policy",
    "exchange_policy",
    "shipping_policy",
    "terms_policy",
    "privacy_policy",
    "warranty",
)


class TrustedContextBrainProjectionError(ValueError):
    """Schema or scope validation failure for brain projection."""


def _normalize_phone(phone: str) -> str:
    return str(phone or "").strip()


def validate_snapshot_scope(
    *,
    snapshot: TrustedContextSnapshot,
    tenant_id: int,
    customer_phone: str,
    conversation_id: Optional[int],
) -> None:
    if int(snapshot.tenant_id) != int(tenant_id):
        raise TrustedContextBrainProjectionError("tenant_mismatch")
    if _normalize_phone(snapshot.customer_phone) != _normalize_phone(customer_phone):
        raise TrustedContextBrainProjectionError("customer_mismatch")
    snap_conv = snapshot.conversation_id
    ctx_conv = conversation_id
    if (snap_conv is None) != (ctx_conv is None):
        raise TrustedContextBrainProjectionError("conversation_mismatch")
    if snap_conv is not None and ctx_conv is not None and int(snap_conv) != int(ctx_conv):
        raise TrustedContextBrainProjectionError("conversation_mismatch")


def _domain_scalar_map(
    snapshot: TrustedContextSnapshot,
    domain: TrustedDomain,
    *,
    allowed_keys: Tuple[str, ...],
    skip_keys: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    skip = set(skip_keys)
    allowed = set(allowed_keys)
    for fact in snapshot.facts_for_domain(domain):
        if fact.key in skip or fact.key not in allowed:
            continue
        if fact.value in (None, "", [], {}):
            continue
        out[fact.key] = fact.value
    return out


def _product_identity_from_catalog(snapshot: TrustedContextSnapshot) -> Dict[str, Any]:
    identity: Dict[str, Any] = {}
    for fact in snapshot.facts_for_domain(TrustedDomain.CATALOG):
        if fact.key in _CATALOG_BUNDLE_KEYS:
            continue
        if fact.key not in _PRODUCT_IDENTITY_KEYS:
            continue
        if fact.value in (None, "", [], {}):
            continue
        identity[fact.key] = fact.value

    for fact in snapshot.facts_for_domain(TrustedDomain.CATALOG):
        if fact.key != "catalog:product_sale_offer":
            continue
        record = fact.value
        if not isinstance(record, dict):
            continue
        target = record.get("target_product")
        if isinstance(target, dict):
            for key in _PRODUCT_IDENTITY_KEYS:
                value = target.get(key)
                if value in (None, "", [], {}):
                    continue
                identity.setdefault(key, value)
    return identity


_CATALOG_CANDIDATE_FACT_KEY = "product_candidates"
_CONVERSATIONAL_REFERENCE_SOURCE = "brain_state.last_search_candidates"

_CANDIDATE_ROW_KEYS: Tuple[str, ...] = (
    "ref",
    "product_id",
    "variant_id",
    "title",
    "price",
    "sale_price",
    "regular_price",
    "available",
    "in_stock",
    "product_url",
    "image_url",
    "cart_url",
)


def _filter_candidate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row[key] for key in _CANDIDATE_ROW_KEYS if key in row}


def _product_candidates_from_catalog(snapshot: TrustedContextSnapshot) -> List[Dict[str, Any]]:
    """
    Pass through ordered candidate rows only when present as a trusted snapshot fact.

    Does not synthesize candidates from sale-offer sample_products or other bundles.
    """
    for fact in snapshot.facts_for_domain(TrustedDomain.CATALOG):
        if fact.key != _CATALOG_CANDIDATE_FACT_KEY:
            continue
        if not isinstance(fact.value, list) or not fact.value:
            return []
        rows: List[Dict[str, Any]] = []
        for item in fact.value:
            if not isinstance(item, dict):
                continue
            filtered = _filter_candidate_row(item)
            if filtered:
                rows.append(filtered)
        return rows
    return []


def _conversational_reference_for_candidates(
    product_candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "source": _CONVERSATIONAL_REFERENCE_SOURCE,
        "ordering": "list_index_1_based",
        "candidate_count": len(product_candidates),
    }


def _merchant_policy_from_snapshot(
    snapshot: TrustedContextSnapshot,
) -> Dict[str, Dict[str, Any]]:
    """Project MERCHANT_POLICY existence facts (status + doc_ref only).

    Skips legacy prose ``shipping_policy`` and non-existence keys.
    Never emits KNOWN_ABSENT — defensive remap to UNKNOWN.
    """
    statuses: Dict[str, str] = {}
    doc_refs: Dict[str, str] = {}
    for fact in snapshot.facts_for_domain(TrustedDomain.MERCHANT_POLICY):
        key = str(fact.key or "")
        if key.startswith("policy_") and key.endswith(".status"):
            kind = key[len("policy_") : -len(".status")]
            if kind not in _POLICY_KIND_KEYS:
                continue
            status = str(fact.value or "UNKNOWN")
            if status == "KNOWN_ABSENT":
                status = "UNKNOWN"
            if status not in {"KNOWN_PRESENT", "UNKNOWN"}:
                status = "UNKNOWN"
            statuses[kind] = status
        elif key.startswith("policy_") and key.endswith(".doc_ref"):
            kind = key[len("policy_") : -len(".doc_ref")]
            if kind not in _POLICY_KIND_KEYS:
                continue
            if fact.value not in (None, "", [], {}):
                doc_refs[kind] = str(fact.value)

    out: Dict[str, Dict[str, Any]] = {}
    for kind in _POLICY_KIND_KEYS:
        if kind not in statuses and kind not in doc_refs:
            continue
        row: Dict[str, Any] = {
            "status": statuses.get(kind, "UNKNOWN"),
        }
        if row["status"] == "KNOWN_PRESENT" and doc_refs.get(kind):
            row["doc_ref"] = doc_refs[kind]
        out[kind] = row
    return out


def _has_projection_payload(payload: Dict[str, Any]) -> bool:
    for key in (
        "product_identity",
        "product_candidates",
        "order",
        "shipment",
        "payment",
        "customer",
        "merchant_capabilities",
        "merchant_profile",
        "merchant_policy",
    ):
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and value:
            return True
    return False


def project_trusted_context_brain_facts(
    *,
    snapshot: TrustedContextSnapshot,
    tenant_id: int,
    customer_phone: str,
    conversation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build scoped Brain/Compose projection from a trusted context snapshot.

    Returns only facts present on the snapshot — never fabricates values.
    """
    validate_snapshot_scope(
        snapshot=snapshot,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        conversation_id=conversation_id,
    )

    product_identity = _product_identity_from_catalog(snapshot)
    product_candidates = _product_candidates_from_catalog(snapshot)
    order = _domain_scalar_map(
        snapshot,
        TrustedDomain.ORDER,
        allowed_keys=_ORDER_KEYS,
    )
    order.update(
        _domain_scalar_map(
            snapshot,
            TrustedDomain.CATALOG,
            allowed_keys=("catalog_order_submitted", "item_count"),
        )
    )
    shipment = _domain_scalar_map(
        snapshot,
        TrustedDomain.SHIPMENT,
        allowed_keys=_SHIPMENT_KEYS,
    )
    payment = _domain_scalar_map(
        snapshot,
        TrustedDomain.PAYMENT,
        allowed_keys=_PAYMENT_KEYS,
    )
    customer = _domain_scalar_map(
        snapshot,
        TrustedDomain.CUSTOMER,
        allowed_keys=_CUSTOMER_KEYS,
    )
    merchant_capabilities = _domain_scalar_map(
        snapshot,
        TrustedDomain.MERCHANT_CAPABILITIES,
        allowed_keys=_MERCHANT_CAPABILITY_KEYS,
    )
    merchant_profile = _domain_scalar_map(
        snapshot,
        TrustedDomain.MERCHANT_PROFILE,
        allowed_keys=(
            "name",
            "description",
            "email",
            "domain",
            "logo_url",
            "social_links",
            "currency",
            "status",
            "phone",
            "location",
            "working_hours",
            "default_branch",
            "name.status",
            "description.status",
            "email.status",
            "domain.status",
            "phone.status",
            "social_links.status",
            "currency.status",
            "status.status",
            "location.status",
            "working_hours.status",
            "default_branch.status",
        ),
    )
    merchant_policy = _merchant_policy_from_snapshot(snapshot)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "surface": SURFACE,
        "facts_snapshot_id": snapshot.ensure_snapshot_id(),
        "loaded_domains": list(snapshot.loaded_domains or []),
        "provenance": "trusted_context_snapshot",
    }
    if product_identity:
        payload["product_identity"] = product_identity
    if product_candidates:
        payload["product_candidates"] = product_candidates
        payload["conversational_reference"] = _conversational_reference_for_candidates(
            product_candidates,
        )
    if order:
        payload["order"] = order
    if shipment:
        payload["shipment"] = shipment
    if payment:
        payload["payment"] = payment
    if customer:
        payload["customer"] = customer
    if merchant_capabilities:
        payload["merchant_capabilities"] = merchant_capabilities
    if merchant_profile:
        payload["merchant_profile"] = merchant_profile
    if merchant_policy:
        payload["merchant_policy"] = merchant_policy

    if not _has_projection_payload(payload):
        raise TrustedContextBrainProjectionError("empty_projection")

    return payload


def selected_product_from_projection(
    projection: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Map projection product_identity into selected_product shape when present."""
    if not isinstance(projection, dict):
        return None
    identity = projection.get("product_identity")
    if not isinstance(identity, dict) or not identity:
        return None
    out: Dict[str, Any] = {}
    if identity.get("product_id") not in (None, ""):
        out["product_id"] = identity.get("product_id")
        out["id"] = identity.get("product_id")
    for key in (
        "variant_id",
        "title",
        "price",
        "sale_price",
        "regular_price",
        "currency",
        "available",
        "in_stock",
        "stock_status",
        "product_url",
        "image_url",
        "cart_url",
    ):
        if identity.get(key) not in (None, "", [], {}):
            out[key] = identity.get(key)
    return out or None


__all__ = [
    "SCHEMA_VERSION",
    "SURFACE",
    "TrustedContextBrainProjectionError",
    "project_trusted_context_brain_facts",
    "selected_product_from_projection",
    "validate_snapshot_scope",
]
