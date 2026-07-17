"""
A1-v3.7 order identity application + tuple-scoped reconciliation.

External path: external_identity_* + external_customer_profile_id; customer_id NULL.
Internal path: customer_link_* + customer_id; external fields NULL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from services.order_customer_identity_capability import cap_coverage_status_for_capability
from services.external_customer_profile_service import upsert_external_customer_profile
from services.order_customer_identity_contract import (
    CUSTOMER_LINK_SOURCE_NAHL_BRIDGE,
    EVIDENCE_AUTHORITATIVE,
    EXTERNAL_LINK_SOURCE_SALLA_PROFILE,
    EXTERNAL_PROVIDER_SALLA_V1,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    NAHLA_INTERNAL_ORDER_V1,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
    ORDER_SOURCE_OTHER,
    ORDER_SOURCE_WHATSAPP,
    PROFILE_SOURCE_SALLA_ORDER_REF,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)
from services.order_customer_identity_logging import log_identity_sync_event
from services.order_customer_identity_reconciliation_classification import (
    TupleLinkageCounts,
    classify_external_tuple_order,
    classify_internal_customer_order,
    count_classifications,
)
from services.salla_integration_resolver import (
    ResolvedSallaIntegration,
    UnresolvedSallaIntegration,
    extract_salla_customer_ref_from_order_payload,
)

_LINK_OUTCOME_LINKED = "linked"
_LINK_OUTCOME_UNLINKED = "unlinked"
_LINK_OUTCOME_FAILED = "link_failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clear_canonical_link_fields(order: Any) -> None:
    order.customer_id = None
    order.customer_link_state = LINK_STATE_UNLINKED
    order.customer_link_evidence_class = None
    order.customer_link_source = None
    order.customer_linked_at = None


def _clear_external_link_fields(order: Any) -> None:
    order.external_customer_profile_id = None
    order.external_identity_link_state = LINK_STATE_UNLINKED
    order.external_identity_evidence_class = None
    order.integration_connection_id = None
    order.external_customer_ref = None
    order.identity_namespace = None


def _null_external_link_fields_for_internal_authoritative(order: Any) -> None:
    """0087 CHECK requires NULL external tuple fields on internal authoritative rows."""
    order.external_customer_profile_id = None
    order.external_identity_link_state = None
    order.external_identity_evidence_class = None
    order.integration_connection_id = None
    order.external_customer_ref = None
    order.identity_namespace = None


def apply_external_order_identity_unlinked(
    order: Any,
    *,
    integration_connection_id: Optional[int] = None,
    external_customer_ref: Optional[str] = None,
    identity_namespace: Optional[str] = None,
    link_outcome: str = _LINK_OUTCOME_UNLINKED,
) -> None:
    """Fail-closed external classification — no canonical customer link."""
    order.order_source_kind = ORDER_SOURCE_EXTERNAL_PROVIDER
    _clear_canonical_link_fields(order)
    order.external_identity_link_state = LINK_STATE_UNLINKED
    order.external_identity_evidence_class = None
    order.external_customer_profile_id = None
    if integration_connection_id is not None:
        order.integration_connection_id = int(integration_connection_id)
    else:
        order.integration_connection_id = None
    if external_customer_ref:
        order.external_customer_ref = str(external_customer_ref).strip()
    else:
        order.external_customer_ref = None
    order.identity_namespace = identity_namespace if external_customer_ref and integration_connection_id else None
    meta = dict(order.extra_metadata or {})
    meta["a1_link_outcome"] = link_outcome
    order.extra_metadata = meta


def apply_external_order_identity_from_salla(
    db: Session,
    *,
    order: Any,
    tenant_id: int,
    integration_resolution: ResolvedSallaIntegration | UnresolvedSallaIntegration,
    order_payload: dict,
    ingest_source: str,
) -> str:
    """
    Apply A1 external identity to an order row. Returns link_outcome token.
    Does not touch Customer canonical columns or upsert Customer.
    """
    customer_ref = extract_salla_customer_ref_from_order_payload(order_payload)

    if isinstance(integration_resolution, UnresolvedSallaIntegration):
        apply_external_order_identity_unlinked(
            order,
            external_customer_ref=customer_ref,
            link_outcome=_LINK_OUTCOME_UNLINKED,
        )
        log_identity_sync_event(
            event="external_identity_unresolved",
            tenant_id=tenant_id,
            order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
            external_identity_link_state=LINK_STATE_UNLINKED,
            customer_link_state=LINK_STATE_UNLINKED,
            link_outcome=_LINK_OUTCOME_UNLINKED,
            reason=integration_resolution.reason,
        )
        return _LINK_OUTCOME_UNLINKED

    conn_id = int(integration_resolution.integration_id)
    if tenant_id != int(integration_resolution.tenant_id):
        apply_external_order_identity_unlinked(
            order,
            integration_connection_id=conn_id,
            external_customer_ref=customer_ref,
            link_outcome=_LINK_OUTCOME_UNLINKED,
        )
        log_identity_sync_event(
            event="tenant_integration_mismatch",
            tenant_id=tenant_id,
            order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
            link_outcome=_LINK_OUTCOME_UNLINKED,
            reason="tenant_mismatch",
        )
        return _LINK_OUTCOME_UNLINKED

    if not customer_ref:
        apply_external_order_identity_unlinked(
            order,
            integration_connection_id=conn_id,
            link_outcome=_LINK_OUTCOME_UNLINKED,
        )
        return _LINK_OUTCOME_UNLINKED

    order.order_source_kind = ORDER_SOURCE_EXTERNAL_PROVIDER
    _clear_canonical_link_fields(order)
    order.integration_connection_id = conn_id
    order.external_customer_ref = customer_ref
    order.identity_namespace = EXTERNAL_PROVIDER_SALLA_V1

    try:
        profile = upsert_external_customer_profile(
            db,
            tenant_id=tenant_id,
            integration_connection_id=conn_id,
            external_customer_ref=customer_ref,
            profile_source=PROFILE_SOURCE_SALLA_ORDER_REF,
        )
        order.external_customer_profile_id = profile.id
        order.external_identity_link_state = LINK_STATE_VERIFIED
        order.external_identity_evidence_class = EVIDENCE_AUTHORITATIVE
        meta = dict(order.extra_metadata or {})
        meta["a1_link_outcome"] = _LINK_OUTCOME_LINKED
        meta["a1_external_link_source"] = EXTERNAL_LINK_SOURCE_SALLA_PROFILE
        order.extra_metadata = meta
        ensure_external_profile_coverage_row(db, profile=profile)
        mark_external_coverage_forward_degraded(db, profile_id=profile.id)
        log_identity_sync_event(
            event="external_identity_linked",
            tenant_id=tenant_id,
            order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
            external_identity_link_state=LINK_STATE_VERIFIED,
            customer_link_state=LINK_STATE_UNLINKED,
            link_outcome=_LINK_OUTCOME_LINKED,
            matched_via=integration_resolution.matched_via,
        )
        return _LINK_OUTCOME_LINKED
    except Exception:  # noqa: BLE001
        from models import ExternalCustomerProfile  # noqa: PLC0415

        # Tuple known but profile link failed — stay in scope for reconciliation.
        order.external_customer_profile_id = None
        order.external_identity_link_state = LINK_STATE_UNLINKED
        order.external_identity_evidence_class = None
        meta = dict(order.extra_metadata or {})
        meta["a1_link_outcome"] = _LINK_OUTCOME_FAILED
        order.extra_metadata = meta
        profile = (
            db.query(ExternalCustomerProfile)
            .filter(
                ExternalCustomerProfile.tenant_id == tenant_id,
                ExternalCustomerProfile.identity_namespace == EXTERNAL_PROVIDER_SALLA_V1,
                ExternalCustomerProfile.integration_connection_id == conn_id,
                ExternalCustomerProfile.external_customer_ref == customer_ref,
            )
            .first()
        )
        if profile is not None:
            ensure_external_profile_coverage_row(db, profile=profile)
            mark_external_coverage_forward_degraded(db, profile_id=profile.id)
        log_identity_sync_event(
            event="external_identity_link_failed",
            tenant_id=tenant_id,
            order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
            link_outcome=_LINK_OUTCOME_FAILED,
            reason="profile_link_exception",
        )
        return _LINK_OUTCOME_FAILED


def apply_nahla_internal_order_identity(
    order: Any,
    *,
    db: Session,
    tenant_id: int,
    customer_id: int,
) -> None:
    """Canonical internal link — sole path that sets Order.customer_id."""
    order.order_source_kind = ORDER_SOURCE_NAHL_INTERNAL
    order.customer_id = int(customer_id)
    order.customer_link_state = LINK_STATE_VERIFIED
    order.customer_link_evidence_class = EVIDENCE_AUTHORITATIVE
    order.customer_link_source = CUSTOMER_LINK_SOURCE_NAHL_BRIDGE
    order.customer_linked_at = _utcnow()
    _null_external_link_fields_for_internal_authoritative(order)
    # Internal authoritative rows keep only the internal namespace populated.
    order.identity_namespace = NAHLA_INTERNAL_ORDER_V1
    ensure_internal_customer_coverage_row(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )


def apply_whatsapp_order_identity_unlinked(order: Any) -> None:
    order.order_source_kind = ORDER_SOURCE_WHATSAPP
    _clear_canonical_link_fields(order)
    _clear_external_link_fields(order)


def classify_order_source_kind_from_legacy_source(source: Optional[str]) -> str:
    src = str(source or "").strip().lower()
    if src == "whatsapp":
        return ORDER_SOURCE_WHATSAPP
    if src == "manual":
        return "manual"
    if src in ("salla", "zid", "shopify"):
        return ORDER_SOURCE_EXTERNAL_PROVIDER
    return ORDER_SOURCE_OTHER


# ── Coverage helpers ──────────────────────────────────────────────────────────


def ensure_external_profile_coverage_row(db: Session, *, profile: Any) -> Any:
    from models import ExternalCustomerProfileOrderHistoryCoverage  # noqa: PLC0415

    row = (
        db.query(ExternalCustomerProfileOrderHistoryCoverage)
        .filter_by(external_customer_profile_id=profile.id)
        .first()
    )
    now = _utcnow()
    if row is None:
        row = ExternalCustomerProfileOrderHistoryCoverage(
            tenant_id=int(profile.tenant_id),
            external_customer_profile_id=profile.id,
            identity_namespace=profile.identity_namespace,
            integration_connection_id=int(profile.integration_connection_id),
            external_customer_ref=profile.external_customer_ref,
            forward_sync_health=SYNC_HEALTH_STALE,
            authoritative_source_history_completeness=SOURCE_HISTORY_INCOMPLETE,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    return row


def mark_external_coverage_forward_degraded(db: Session, *, profile_id: Any) -> None:
    from models import ExternalCustomerProfileOrderHistoryCoverage  # noqa: PLC0415

    row = (
        db.query(ExternalCustomerProfileOrderHistoryCoverage)
        .filter_by(external_customer_profile_id=profile_id)
        .first()
    )
    if row is None:
        return
    row.forward_sync_health = SYNC_HEALTH_DEGRADED
    row.authoritative_source_history_completeness = SOURCE_HISTORY_INCOMPLETE
    row.updated_at = _utcnow()


def ensure_internal_customer_coverage_row(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> None:
    from models import NahlaInternalCustomerOrderHistoryCoverage  # noqa: PLC0415

    row = (
        db.query(NahlaInternalCustomerOrderHistoryCoverage)
        .filter_by(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        )
        .first()
    )
    now = _utcnow()
    if row is None:
        row = NahlaInternalCustomerOrderHistoryCoverage(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            forward_sync_health=SYNC_HEALTH_STALE,
            authoritative_source_history_completeness=SOURCE_HISTORY_INCOMPLETE,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()


@dataclass(frozen=True)
class ExternalTupleReconcileResult:
    linked: int
    unmapped: int
    mislinked: int
    completeness: str
    forward_health: str


def _result_from_counts(
    db: Session,
    *,
    counts: TupleLinkageCounts,
) -> ExternalTupleReconcileResult:
    completeness = (
        SOURCE_HISTORY_COMPLETE
        if counts.unmapped == 0 and counts.mislinked == 0 and counts.linked > 0
        else SOURCE_HISTORY_INCOMPLETE
    )
    forward_health = (
        SYNC_HEALTH_HEALTHY
        if completeness == SOURCE_HISTORY_COMPLETE
        else SYNC_HEALTH_DEGRADED
    )
    completeness, forward_health = cap_coverage_status_for_capability(
        db,
        completeness=completeness,
        forward_health=forward_health,
    )
    return ExternalTupleReconcileResult(
        linked=counts.linked,
        unmapped=counts.unmapped,
        mislinked=counts.mislinked,
        completeness=completeness,
        forward_health=forward_health,
    )


def reconcile_external_profile_coverage(
    db: Session,
    *,
    profile: Any,
) -> ExternalTupleReconcileResult:
    """Tuple-scoped reconciliation — includes failed/missing profile links."""
    from models import Order  # noqa: PLC0415

    ensure_external_profile_coverage_row(db, profile=profile)
    tuple_filter = (
        Order.tenant_id == int(profile.tenant_id),
        Order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER,
        Order.identity_namespace == profile.identity_namespace,
        Order.integration_connection_id == int(profile.integration_connection_id),
        Order.external_customer_ref == profile.external_customer_ref,
    )
    orders = db.query(Order).filter(*tuple_filter).all()
    counts = count_classifications(
        classify_external_tuple_order(order=order, profile_id=profile.id)
        for order in orders
    )
    result = _result_from_counts(db, counts=counts)

    from models import ExternalCustomerProfileOrderHistoryCoverage  # noqa: PLC0415

    cov = (
        db.query(ExternalCustomerProfileOrderHistoryCoverage)
        .filter_by(external_customer_profile_id=profile.id)
        .first()
    )
    if cov is not None:
        now = _utcnow()
        cov.linked_orders_in_scope_count = result.linked
        cov.unmapped_orders_in_scope_count = result.unmapped
        cov.mislinked_orders_in_scope_count = result.mislinked
        cov.authoritative_source_history_completeness = result.completeness
        cov.forward_sync_health = result.forward_health
        cov.watermark_at = now
        cov.updated_at = now

    return result


def reconcile_internal_customer_coverage(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> ExternalTupleReconcileResult:
    from models import NahlaInternalCustomerOrderHistoryCoverage, Order  # noqa: PLC0415

    ensure_internal_customer_coverage_row(db, tenant_id=tenant_id, customer_id=customer_id)
    orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.customer_id == int(customer_id),
            Order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL,
        )
        .all()
    )
    counts = count_classifications(
        classify_internal_customer_order(order=order)
        for order in orders
    )
    result = _result_from_counts(db, counts=counts)

    cov = (
        db.query(NahlaInternalCustomerOrderHistoryCoverage)
        .filter_by(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        )
        .first()
    )
    if cov is not None:
        now = _utcnow()
        cov.linked_orders_in_scope_count = result.linked
        cov.unmapped_orders_in_scope_count = result.unmapped
        cov.mislinked_orders_in_scope_count = result.mislinked
        cov.authoritative_source_history_completeness = result.completeness
        cov.forward_sync_health = result.forward_health
        cov.watermark_at = now
        cov.updated_at = now

    return result


__all__ = [
    "ExternalTupleReconcileResult",
    "apply_external_order_identity_from_salla",
    "apply_external_order_identity_unlinked",
    "apply_nahla_internal_order_identity",
    "apply_whatsapp_order_identity_unlinked",
    "classify_order_source_kind_from_legacy_source",
    "reconcile_external_profile_coverage",
    "reconcile_internal_customer_coverage",
]
