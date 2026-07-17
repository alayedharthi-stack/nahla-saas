"""
Staging-only A1 generic-commerce evidence fixture harness.

Creates internal + external authoritative order evidence strictly through
existing A1/order/integration service APIs. Does not run reconciliation write,
validations, migrations, or customer-visible behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from services.order_customer_identity_evidence_fixture_contract import (
    FIXTURE_EXTERNAL_CUSTOMER_REF,
    FIXTURE_EXTERNAL_ID_PREFIX,
    FIXTURE_EXTERNAL_ORDER_SUFFIX,
    FIXTURE_EXTERNAL_STORE_SUFFIX,
    FIXTURE_INTERNAL_ORDER_SUFFIX,
    FIXTURE_MARKER_FIELD,
    FIXTURE_NAMESPACE,
    FIXTURE_SCHEMA_VERSION,
    FIXTURE_SLOT_EXTERNAL_ORDER,
    FIXTURE_SLOT_INTEGRATION,
    FIXTURE_SLOT_INTERNAL_CUSTOMER,
    FIXTURE_SLOT_INTERNAL_ORDER,
    FIXTURE_SLOT_FIELD,
    GENERIC_CITY,
    GENERIC_EXTERNAL_CUSTOMER_NAME,
    GENERIC_INTERNAL_CUSTOMER_NAME,
    GENERIC_PRODUCT_PERFUME,
    GENERIC_PRODUCT_SHOES,
    GENERIC_SHORT_CODE,
    MAX_EXTERNAL_ORDERS,
    MAX_EXTERNAL_PROFILES,
    MAX_INTEGRATIONS,
    MAX_INTERNAL_CUSTOMERS,
    MAX_INTERNAL_ORDERS,
)
from services.order_customer_identity_logging import log_evidence_fixture_failure
from services.order_customer_identity_reconciliation_write import (
    read_alembic_revision,
    read_capability_detail,
    validate_capability_and_revision_gates,
)
from services.order_customer_identity_service import (
    apply_external_order_identity_from_salla,
    apply_nahla_internal_order_identity,
)
from services.salla_integration_resolver import ResolvedSallaIntegration


@dataclass(frozen=True)
class FixtureGateFailure:
    error_class: str
    stage: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fixture_external_id(tenant_id: int, suffix: str) -> str:
    return f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-{suffix}"


def _fixture_store_id(tenant_id: int) -> str:
    return _fixture_external_id(tenant_id, FIXTURE_EXTERNAL_STORE_SUFFIX)


def _fixture_marker(*, slot: str) -> Dict[str, str]:
    return {
        FIXTURE_MARKER_FIELD: FIXTURE_NAMESPACE,
        FIXTURE_SLOT_FIELD: slot,
    }


def _merge_fixture_metadata(row: Any, *, slot: str) -> None:
    meta = dict(getattr(row, "extra_metadata", None) or {})
    meta.update(_fixture_marker(slot=slot))
    row.extra_metadata = meta


def _row_has_fixture_namespace(row: Any) -> bool:
    meta = getattr(row, "extra_metadata", None) or {}
    return str(meta.get(FIXTURE_MARKER_FIELD) or "").strip() == FIXTURE_NAMESPACE


def _generic_internal_line_items() -> List[Dict[str, Any]]:
    return [
        {"name": GENERIC_PRODUCT_SHOES, "quantity": 1, "unit_price": "199.00"},
    ]


def _generic_external_line_items() -> List[Dict[str, Any]]:
    return [
        {"name": GENERIC_PRODUCT_PERFUME, "quantity": 1, "unit_price": "249.00"},
    ]


def _fixture_orders_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Order  # noqa: PLC0415

    prefix = f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-"
    rows = (
        db.query(Order)
        .filter(Order.tenant_id == int(tenant_id))
        .filter(Order.external_id.like(f"{prefix}%"))
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_integrations_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Integration  # noqa: PLC0415

    store_id = _fixture_store_id(tenant_id)
    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == int(tenant_id),
            Integration.external_store_id == store_id,
        )
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_customers_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Customer  # noqa: PLC0415

    rows = (
        db.query(Customer)
        .filter(Customer.tenant_id == int(tenant_id))
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_profiles_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import ExternalCustomerProfile  # noqa: PLC0415

    rows = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.tenant_id == int(tenant_id),
            ExternalCustomerProfile.external_customer_ref == FIXTURE_EXTERNAL_CUSTOMER_REF,
        )
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _count_fixture_shape(db: Session, *, tenant_id: int) -> Dict[str, int]:
    orders = _fixture_orders_for_tenant(db, tenant_id=tenant_id)
    internal_orders = sum(
        1 for order in orders if str((order.extra_metadata or {}).get(FIXTURE_SLOT_FIELD)) == FIXTURE_SLOT_INTERNAL_ORDER
    )
    external_orders = sum(
        1 for order in orders if str((order.extra_metadata or {}).get(FIXTURE_SLOT_FIELD)) == FIXTURE_SLOT_EXTERNAL_ORDER
    )
    return {
        "integrations": len(_fixture_integrations_for_tenant(db, tenant_id=tenant_id)),
        "internal_customers": len(_fixture_customers_for_tenant(db, tenant_id=tenant_id)),
        "external_profiles": len(_fixture_profiles_for_tenant(db, tenant_id=tenant_id)),
        "internal_orders": internal_orders,
        "external_orders": external_orders,
        "total_orders": len(orders),
    }


def validate_fixture_input(*, tenant_id: int) -> FixtureGateFailure | None:
    if int(tenant_id) <= 0:
        return FixtureGateFailure("input_rejected", "invalid_tenant_scope")
    return None


@dataclass
class OrderCustomerIdentityEvidenceFixtureResult:
    fixture_schema_version: str = FIXTURE_SCHEMA_VERSION
    tenant_id: int = 0
    mode: str = "seed"
    dry_run: bool = True
    read_only: bool = True
    outcome: str = "aborted"
    access_status: str = "ok"
    gate_stage: Optional[str] = None
    gate_error_class: Optional[str] = None
    tenant_present: bool = False
    capability_state: Optional[str] = None
    capability_state_readable: bool = False
    capability_validation_revision: Optional[str] = None
    alembic_revision: Optional[str] = None
    alembic_revision_is_0087: bool = False
    fixture_namespace: str = FIXTURE_NAMESPACE
    existing_shape: Dict[str, int] = field(default_factory=dict)
    would_create: Dict[str, int] = field(default_factory=dict)
    created: Dict[str, int] = field(default_factory=dict)
    skipped_existing: Dict[str, int] = field(default_factory=dict)
    authoritative_internal_orders: int = 0
    authoritative_external_orders: int = 0
    cleanup_selected: Dict[str, int] = field(default_factory=dict)
    cleanup_deleted: Dict[str, int] = field(default_factory=dict)
    committed: bool = False
    fixture_generated_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fixture_schema_version": self.fixture_schema_version,
            "tenant_id": int(self.tenant_id),
            "mode": self.mode,
            "dry_run": bool(self.dry_run),
            "read_only": bool(self.read_only),
            "outcome": self.outcome,
            "access_status": self.access_status,
            "gate_stage": self.gate_stage,
            "gate_error_class": self.gate_error_class,
            "tenant_present": bool(self.tenant_present),
            "capability": {
                "state": self.capability_state,
                "state_readable": bool(self.capability_state_readable),
                "validation_revision": self.capability_validation_revision,
                "alembic_revision": self.alembic_revision,
                "alembic_revision_is_0087": bool(self.alembic_revision_is_0087),
            },
            "fixture_namespace": self.fixture_namespace,
            "shape": {
                "existing": dict(self.existing_shape),
                "would_create": dict(self.would_create),
                "created": dict(self.created),
                "skipped_existing": dict(self.skipped_existing),
            },
            "authoritative": {
                "internal_orders": int(self.authoritative_internal_orders),
                "external_orders": int(self.authoritative_external_orders),
            },
            "cleanup": {
                "selected": dict(self.cleanup_selected),
                "deleted": dict(self.cleanup_deleted),
            },
            "committed": bool(self.committed),
            "fixture_generated_at_utc": self.fixture_generated_at_utc,
        }

    def summary_line(self) -> str:
        return (
            f"a1_evidence_fixture tenant={self.tenant_id} mode={self.mode} "
            f"dry_run={int(self.dry_run)} outcome={self.outcome} "
            f"committed={int(self.committed)}"
        )


def _apply_common_gates(
    db: Session,
    result: OrderCustomerIdentityEvidenceFixtureResult,
) -> Optional[FixtureGateFailure]:
    input_failure = validate_fixture_input(tenant_id=result.tenant_id)
    if input_failure:
        result.outcome = "failed"
        result.access_status = "gate_rejected"
        result.gate_stage = input_failure.stage
        result.gate_error_class = input_failure.error_class
        return input_failure

    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant.id).filter(Tenant.id == int(result.tenant_id)).first()
    result.tenant_present = tenant is not None
    if not result.tenant_present:
        result.outcome = "failed"
        result.access_status = "tenant_missing"
        return FixtureGateFailure("tenant_rejected", "tenant_missing")

    result.alembic_revision = read_alembic_revision(db)
    result.alembic_revision_is_0087 = result.alembic_revision == "0087"
    state, validation_revision = read_capability_detail(db)
    result.capability_state = state
    result.capability_state_readable = state is not None
    result.capability_validation_revision = validation_revision

    capability_failure = validate_capability_and_revision_gates(db)
    if capability_failure:
        result.outcome = "failed"
        result.access_status = "gate_rejected"
        result.gate_stage = capability_failure.stage
        result.gate_error_class = capability_failure.error_class
        return FixtureGateFailure(capability_failure.error_class, capability_failure.stage)
    return None


def _count_authoritative_orders(db: Session, *, tenant_id: int) -> tuple[int, int]:
    from models import Order  # noqa: PLC0415
    from services.order_customer_identity_contract import (  # noqa: PLC0415
        EVIDENCE_AUTHORITATIVE,
        ORDER_SOURCE_EXTERNAL_PROVIDER,
        ORDER_SOURCE_NAHL_INTERNAL,
    )

    fixture_orders = _fixture_orders_for_tenant(db, tenant_id=tenant_id)
    internal = sum(
        1
        for order in fixture_orders
        if order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL
        and order.customer_link_evidence_class == EVIDENCE_AUTHORITATIVE
    )
    external = sum(
        1
        for order in fixture_orders
        if order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER
        and order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE
    )
    return internal, external


def _ensure_integration(db: Session, *, tenant_id: int, dry_run: bool) -> tuple[Any | None, bool]:
    existing = _fixture_integrations_for_tenant(db, tenant_id=tenant_id)
    if existing:
        return existing[0], False
    if dry_run:
        return None, True
    from models import Integration  # noqa: PLC0415

    row = Integration(
        tenant_id=int(tenant_id),
        provider="salla",
        external_store_id=_fixture_store_id(tenant_id),
        config={"api_key": "fixture-no-network", "api_sync_enabled": False},
        enabled=True,
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_INTEGRATION)
    db.add(row)
    db.flush()
    return row, True


def _ensure_internal_customer(db: Session, *, tenant_id: int, dry_run: bool) -> tuple[Any | None, bool]:
    existing = _fixture_customers_for_tenant(db, tenant_id=tenant_id)
    if existing:
        return existing[0], False
    if dry_run:
        return None, True
    from models import Customer  # noqa: PLC0415

    row = Customer(
        tenant_id=int(tenant_id),
        name=GENERIC_INTERNAL_CUSTOMER_NAME,
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_INTERNAL_CUSTOMER)
    db.add(row)
    db.flush()
    return row, True


def _existing_fixture_order(db: Session, *, tenant_id: int, slot: str) -> Any | None:
    for order in _fixture_orders_for_tenant(db, tenant_id=tenant_id):
        if str((order.extra_metadata or {}).get(FIXTURE_SLOT_FIELD)) == slot:
            return order
    return None


def _ensure_internal_order(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    dry_run: bool,
) -> tuple[Any | None, bool]:
    existing = _existing_fixture_order(db, tenant_id=tenant_id, slot=FIXTURE_SLOT_INTERNAL_ORDER)
    if existing:
        return existing, False
    if dry_run:
        return None, True
    from models import Order  # noqa: PLC0415

    row = Order(
        tenant_id=int(tenant_id),
        external_id=_fixture_external_id(tenant_id, FIXTURE_INTERNAL_ORDER_SUFFIX),
        status="pending",
        total="199.00",
        source="whatsapp",
        customer_name=GENERIC_INTERNAL_CUSTOMER_NAME,
        line_items=_generic_internal_line_items(),
        customer_info={"city": GENERIC_CITY, "short_address_code": GENERIC_SHORT_CODE},
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_INTERNAL_ORDER)
    db.add(row)
    db.flush()
    apply_nahla_internal_order_identity(
        row,
        db=db,
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_INTERNAL_ORDER)
    db.flush()
    return row, True


def _ensure_external_order(
    db: Session,
    *,
    tenant_id: int,
    integration_id: int,
    dry_run: bool,
) -> tuple[Any | None, bool]:
    existing = _existing_fixture_order(db, tenant_id=tenant_id, slot=FIXTURE_SLOT_EXTERNAL_ORDER)
    if existing:
        return existing, False
    if dry_run:
        return None, True
    from models import Order  # noqa: PLC0415

    row = Order(
        tenant_id=int(tenant_id),
        external_id=_fixture_external_id(tenant_id, FIXTURE_EXTERNAL_ORDER_SUFFIX),
        status="pending",
        total="249.00",
        source="salla",
        customer_name=GENERIC_EXTERNAL_CUSTOMER_NAME,
        line_items=_generic_external_line_items(),
        customer_info={"city": GENERIC_CITY, "short_address_code": GENERIC_SHORT_CODE},
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_EXTERNAL_ORDER)
    db.add(row)
    db.flush()
    resolution = ResolvedSallaIntegration(
        integration_id=int(integration_id),
        tenant_id=int(tenant_id),
        matched_via="a1_evidence_fixture",
    )
    apply_external_order_identity_from_salla(
        db,
        order=row,
        tenant_id=int(tenant_id),
        integration_resolution=resolution,
        order_payload={"customer": {"id": FIXTURE_EXTERNAL_CUSTOMER_REF}},
        ingest_source="a1_evidence_fixture",
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_EXTERNAL_ORDER)
    if row.external_customer_profile_id is not None:
        from models import ExternalCustomerProfile  # noqa: PLC0415

        profile = db.get(ExternalCustomerProfile, row.external_customer_profile_id)
        if profile is not None:
            _merge_fixture_metadata(profile, slot=FIXTURE_SLOT_EXTERNAL_ORDER)
    db.flush()
    return row, True


def execute_order_customer_identity_evidence_fixture_seed(
    db: Session,
    tenant_id: int,
    *,
    dry_run: bool = True,
) -> OrderCustomerIdentityEvidenceFixtureResult:
    result = OrderCustomerIdentityEvidenceFixtureResult(
        tenant_id=int(tenant_id),
        mode="seed",
        dry_run=bool(dry_run),
        read_only=bool(dry_run),
        fixture_generated_at_utc=_utcnow_iso(),
    )
    try:
        gate = _apply_common_gates(db, result)
        if gate:
            return result

        existing = _count_fixture_shape(db, tenant_id=int(tenant_id))
        result.existing_shape = existing

        would_create = {
            "integrations": 0 if existing["integrations"] >= MAX_INTEGRATIONS else 1,
            "internal_customers": 0 if existing["internal_customers"] >= MAX_INTERNAL_CUSTOMERS else 1,
            "internal_orders": 0 if existing["internal_orders"] >= MAX_INTERNAL_ORDERS else 1,
            "external_orders": 0 if existing["external_orders"] >= MAX_EXTERNAL_ORDERS else 1,
            "external_profiles": 0 if existing["external_profiles"] >= MAX_EXTERNAL_PROFILES else 1,
        }
        result.would_create = would_create

        if sum(would_create.values()) == 0:
            result.outcome = "success"
            result.skipped_existing = {
                "integrations": existing["integrations"],
                "internal_customers": existing["internal_customers"],
                "internal_orders": existing["internal_orders"],
                "external_orders": existing["external_orders"],
                "external_profiles": existing["external_profiles"],
            }
            internal_auth, external_auth = _count_authoritative_orders(db, tenant_id=int(tenant_id))
            result.authoritative_internal_orders = internal_auth
            result.authoritative_external_orders = external_auth
            return result

        if dry_run:
            result.outcome = "success"
            internal_auth, external_auth = _count_authoritative_orders(db, tenant_id=int(tenant_id))
            result.authoritative_internal_orders = internal_auth
            result.authoritative_external_orders = external_auth
            return result

        created = {
            "integrations": 0,
            "internal_customers": 0,
            "internal_orders": 0,
            "external_orders": 0,
            "external_profiles": 0,
        }

        integration, created_integration = _ensure_integration(db, tenant_id=int(tenant_id), dry_run=False)
        if created_integration:
            created["integrations"] = 1
        if integration is None:
            raise RuntimeError("integration_missing_after_ensure")

        customer, created_customer = _ensure_internal_customer(db, tenant_id=int(tenant_id), dry_run=False)
        if created_customer:
            created["internal_customers"] = 1
        if customer is None:
            raise RuntimeError("customer_missing_after_ensure")

        _, created_internal = _ensure_internal_order(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(customer.id),
            dry_run=False,
        )
        if created_internal:
            created["internal_orders"] = 1

        _, created_external = _ensure_external_order(
            db,
            tenant_id=int(tenant_id),
            integration_id=int(integration.id),
            dry_run=False,
        )
        if created_external:
            created["external_orders"] = 1
            created["external_profiles"] = 1

        db.commit()
        result.committed = True
        result.created = created
        result.outcome = "success"
        internal_auth, external_auth = _count_authoritative_orders(db, tenant_id=int(tenant_id))
        result.authoritative_internal_orders = internal_auth
        result.authoritative_external_orders = external_auth
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log_evidence_fixture_failure(exception_class=type(exc).__name__)
        result.outcome = "failed"
        result.access_status = "execution_failed"
        return result


def _profile_ids_from_fixture_orders(orders: List[Any]) -> Set[Any]:
    ids: Set[Any] = set()
    for order in orders:
        profile_id = getattr(order, "external_customer_profile_id", None)
        if profile_id is not None:
            ids.add(profile_id)
    return ids


def _customer_ids_from_fixture_orders(orders: List[Any]) -> Set[int]:
    ids: Set[int] = set()
    for order in orders:
        customer_id = getattr(order, "customer_id", None)
        if customer_id is not None:
            ids.add(int(customer_id))
    return ids


def execute_order_customer_identity_evidence_fixture_cleanup(
    db: Session,
    tenant_id: int,
    *,
    dry_run: bool = True,
) -> OrderCustomerIdentityEvidenceFixtureResult:
    result = OrderCustomerIdentityEvidenceFixtureResult(
        tenant_id=int(tenant_id),
        mode="cleanup",
        dry_run=bool(dry_run),
        read_only=bool(dry_run),
        fixture_generated_at_utc=_utcnow_iso(),
    )
    try:
        gate = _apply_common_gates(db, result)
        if gate:
            return result

        fixture_orders = _fixture_orders_for_tenant(db, tenant_id=int(tenant_id))
        fixture_integrations = _fixture_integrations_for_tenant(db, tenant_id=int(tenant_id))
        fixture_customers = _fixture_customers_for_tenant(db, tenant_id=int(tenant_id))
        fixture_profiles = _fixture_profiles_for_tenant(db, tenant_id=int(tenant_id))

        profile_ids = _profile_ids_from_fixture_orders(fixture_orders) | {
            profile.id for profile in fixture_profiles
        }
        customer_ids = _customer_ids_from_fixture_orders(fixture_orders) | {
            int(customer.id) for customer in fixture_customers
        }

        selected = {
            "orders": len(fixture_orders),
            "external_profiles": len(profile_ids),
            "internal_customers": len(customer_ids),
            "integrations": len(fixture_integrations),
            "external_coverage_rows": 0,
            "internal_coverage_rows": 0,
        }

        from models import (  # noqa: PLC0415
            ExternalCustomerProfileOrderHistoryCoverage,
            NahlaInternalCustomerOrderHistoryCoverage,
        )

        if profile_ids:
            selected["external_coverage_rows"] = (
                db.query(ExternalCustomerProfileOrderHistoryCoverage.id)
                .filter(
                    ExternalCustomerProfileOrderHistoryCoverage.tenant_id == int(tenant_id),
                    ExternalCustomerProfileOrderHistoryCoverage.external_customer_profile_id.in_(
                        list(profile_ids)
                    ),
                )
                .count()
            )
        if customer_ids:
            selected["internal_coverage_rows"] = (
                db.query(NahlaInternalCustomerOrderHistoryCoverage.id)
                .filter(
                    NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
                    NahlaInternalCustomerOrderHistoryCoverage.customer_id.in_(list(customer_ids)),
                )
                .count()
            )

        result.cleanup_selected = selected
        result.existing_shape = _count_fixture_shape(db, tenant_id=int(tenant_id))

        if dry_run:
            result.outcome = "success"
            return result

        deleted = {
            "orders": 0,
            "external_coverage_rows": 0,
            "internal_coverage_rows": 0,
            "external_profiles": 0,
            "internal_customers": 0,
            "integrations": 0,
        }

        for order in fixture_orders:
            db.delete(order)
            deleted["orders"] += 1
        db.flush()

        if profile_ids:
            deleted["external_coverage_rows"] = (
                db.query(ExternalCustomerProfileOrderHistoryCoverage)
                .filter(
                    ExternalCustomerProfileOrderHistoryCoverage.tenant_id == int(tenant_id),
                    ExternalCustomerProfileOrderHistoryCoverage.external_customer_profile_id.in_(
                        list(profile_ids)
                    ),
                )
                .delete(synchronize_session=False)
            )
        if customer_ids:
            deleted["internal_coverage_rows"] = (
                db.query(NahlaInternalCustomerOrderHistoryCoverage)
                .filter(
                    NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
                    NahlaInternalCustomerOrderHistoryCoverage.customer_id.in_(list(customer_ids)),
                )
                .delete(synchronize_session=False)
            )

        if profile_ids:
            from models import ExternalCustomerProfile  # noqa: PLC0415

            for profile in (
                db.query(ExternalCustomerProfile)
                .filter(
                    ExternalCustomerProfile.tenant_id == int(tenant_id),
                    ExternalCustomerProfile.id.in_(list(profile_ids)),
                )
                .all()
            ):
                if _row_has_fixture_namespace(profile):
                    db.delete(profile)
                    deleted["external_profiles"] += 1

        for customer in fixture_customers:
            if int(customer.id) in customer_ids:
                db.delete(customer)
                deleted["internal_customers"] += 1

        for integration in fixture_integrations:
            db.delete(integration)
            deleted["integrations"] += 1

        db.commit()
        result.committed = True
        result.cleanup_deleted = deleted
        result.outcome = "success"
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log_evidence_fixture_failure(exception_class=type(exc).__name__)
        result.outcome = "failed"
        result.access_status = "execution_failed"
        return result


__all__ = [
    "FixtureGateFailure",
    "OrderCustomerIdentityEvidenceFixtureResult",
    "execute_order_customer_identity_evidence_fixture_cleanup",
    "execute_order_customer_identity_evidence_fixture_seed",
    "validate_fixture_input",
]
