"""
customer_conditional_coupon_repository.py
─────────────────────────────────────────
Bounded read-only order counts for conditional-coupon Layer 0 facts.

Reuses canonical ``countable_order_sql_predicate`` — no duplicated status policy.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from services.order_countability_policy import countable_order_sql_predicate
from services.promotion_engine import ACTIVE_STATUS
from services.order_customer_identity_contract import (
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_VERIFIED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
)

from .customer_conditional_coupon_subject import ConditionalCouponSubjectHandle


class ConditionalCouponRepositoryError(Exception):
    """Bounded read failure — fail closed upstream."""


def promotion_liveness_sql_predicate(promotion: Any) -> Any:
    """
    SQL parity for ``promotion_engine.is_promotion_active``.

    Both sides use UTC: the engine compares against UTC-aware application time;
    PostgreSQL discovery uses the database UTC wall clock, converted to the
    naive UTC shape used by the Promotion timestamp columns.
    """
    now_utc = func.timezone("UTC", func.now())
    return and_(
        promotion.status == ACTIVE_STATUS,
        or_(promotion.starts_at.is_(None), promotion.starts_at <= now_utc),
        or_(promotion.ends_at.is_(None), promotion.ends_at > now_utc),
        or_(
            promotion.usage_limit.is_(None),
            func.coalesce(promotion.usage_count, 0) < promotion.usage_limit,
        ),
    )


def count_countable_orders_for_subject(
    db: Session,
    *,
    handle: ConditionalCouponSubjectHandle,
) -> int:
    """One bounded count query scoped to the resolved A1 subject."""
    if handle.subject_kind == "external_customer_profile":
        if handle.external_customer_profile_id is None:
            raise ConditionalCouponRepositoryError("missing_external_profile_id")
        return _count_external_profile_orders(
            db,
            tenant_id=int(handle.tenant_id),
            external_customer_profile_id=handle.external_customer_profile_id,
        )
    if handle.subject_kind == "nahla_internal_customer":
        if handle.customer_id is None:
            raise ConditionalCouponRepositoryError("missing_internal_customer_id")
        return _count_internal_customer_orders(
            db,
            tenant_id=int(handle.tenant_id),
            customer_id=int(handle.customer_id),
        )
    raise ConditionalCouponRepositoryError("unsupported_subject_kind")


def _count_internal_customer_orders(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> int:
    from models import Order  # noqa: PLC0415

    query = (
        db.query(func.count(Order.id))
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.customer_id == int(customer_id),
            Order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL,
            Order.customer_link_state == LINK_STATE_VERIFIED,
            Order.customer_link_evidence_class == EVIDENCE_AUTHORITATIVE,
            countable_order_sql_predicate(Order.status, Order.is_abandoned),
        )
    )
    try:
        return int(query.scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        raise ConditionalCouponRepositoryError(exc.__class__.__name__) from exc


def _count_external_profile_orders(
    db: Session,
    *,
    tenant_id: int,
    external_customer_profile_id: UUID,
) -> int:
    from models import ExternalCustomerProfile, Order  # noqa: PLC0415

    profile = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.id == external_customer_profile_id,
            ExternalCustomerProfile.tenant_id == int(tenant_id),
        )
        .first()
    )
    if profile is None:
        raise ConditionalCouponRepositoryError("external_profile_not_found")

    query = (
        db.query(func.count(Order.id))
        .filter(
            Order.tenant_id == int(profile.tenant_id),
            Order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER,
            Order.identity_namespace == profile.identity_namespace,
            Order.integration_connection_id == int(profile.integration_connection_id),
            Order.external_customer_ref == profile.external_customer_ref,
            Order.external_customer_profile_id == profile.id,
            Order.external_identity_link_state == LINK_STATE_VERIFIED,
            Order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE,
            countable_order_sql_predicate(Order.status, Order.is_abandoned),
        )
    )
    try:
        return int(query.scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        raise ConditionalCouponRepositoryError(exc.__class__.__name__) from exc


def scan_conditional_targets(
    db: Session,
    *,
    tenant_id: int,
    limit: int,
) -> list[Any]:
    """
    Read-only deterministic discovery for the closed promotion condition source.

    Returns ORM rows only for in-loader evaluation — never serialized to facts.
    """
    from models import Promotion  # noqa: PLC0415

    # ``Promotion.conditions`` is the only persisted, closed condition source
    # for this Layer 0 slice. JSONB ``astext`` is PostgreSQL-specific, matching
    # the production JSONB model; do not silently fall back to Python scans.
    # Liveness exactly mirrors ``promotion_engine.is_promotion_active``.
    min_orders = Promotion.conditions["min_orders_for_eligibility"].astext
    try:
        return (
            db.query(Promotion)
            .filter(
                Promotion.tenant_id == int(tenant_id),
                min_orders.isnot(None),
                min_orders != "",
                min_orders != "0",
                promotion_liveness_sql_predicate(Promotion),
            )
            .order_by(Promotion.id.asc())
            .limit(int(limit))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        raise ConditionalCouponRepositoryError(
            f"conditional_target_discovery_failed:{exc.__class__.__name__}",
        ) from exc


def extract_min_orders_threshold(row: Any) -> Optional[int]:
    cond = dict(getattr(row, "conditions", None) or {})
    raw = cond.get("min_orders_for_eligibility")
    if raw in (None, "", 0):
        return None
    try:
        threshold = int(raw)
    except (TypeError, ValueError):
        return None
    return threshold if threshold > 0 else None


def row_has_personalised_usage_gate(row: Any) -> bool:
    meta = dict(getattr(row, "extra_metadata", None) or {})
    if meta.get("customer_id") not in (None, ""):
        return True
    cond = dict(getattr(row, "conditions", None) or {})
    return bool(cond.get("customer_segments"))


__all__ = [
    "ConditionalCouponRepositoryError",
    "count_countable_orders_for_subject",
    "extract_min_orders_threshold",
    "promotion_liveness_sql_predicate",
    "row_has_personalised_usage_gate",
    "scan_conditional_targets",
]
