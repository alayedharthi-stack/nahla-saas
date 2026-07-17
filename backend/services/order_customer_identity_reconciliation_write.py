"""
Tenant-scoped A1 reconciliation write operator (post-0087 Expand).

Invokes reconcile_* service APIs with bounded enumeration, staging gates for writes,
and privacy-safe aggregate output. Dry-run by default; writes require confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.order_customer_identity_capability import read_order_customer_identity_capability_state
from services.order_customer_identity_contract import (
    CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY,
    CAPABILITY_STATE_VALIDATED,
)
from services.order_customer_identity_logging import log_reconciliation_write_failure
from services.order_customer_identity_reconciliation_report import (
    DEFAULT_MAX_SUBJECTS_PER_KIND,
    MAX_SUBJECTS_PER_KIND,
    _internal_customer_ids_for_tenant,
    _rollup_external_profiles,
    _rollup_internal_customers,
)
from services.order_customer_identity_reconciliation_write_contract import (
    ALEMBIC_REVISIONS_0087_COMPATIBLE,
    CAPABILITY_KEY,
    CAPABILITY_STATE_EXPAND as CONTRACT_CAPABILITY_STATE_EXPAND,
    WRITE_SCHEMA_VERSION,
)
from services.order_customer_identity_service import (
    reconcile_external_profile_coverage,
    reconcile_internal_customer_coverage,
)

_REVISION_SQL = text("SELECT version_num FROM alembic_version LIMIT 1")
_CAPABILITY_DETAIL_SQL = text(
    """
    SELECT state, validation_revision
    FROM order_customer_identity_capability_state
    WHERE capability_key = :capability_key
    LIMIT 1
    """
)


@dataclass(frozen=True)
class WriteGateFailure:
    error_class: str
    stage: str


@dataclass
class OrderCustomerIdentityReconciliationWriteResult:
    write_schema_version: str = WRITE_SCHEMA_VERSION
    tenant_id: int = 0
    dry_run: bool = True
    read_only: bool = True
    outcome: str = "aborted"
    access_status: str = "ok"
    gate_stage: Optional[str] = None
    gate_error_class: Optional[str] = None
    tenant_present: bool = False
    capability_key: str = CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY
    capability_state: Optional[str] = None
    capability_state_readable: bool = False
    capability_validation_revision: Optional[str] = None
    alembic_revision: Optional[str] = None
    revision_0087_compatible: bool = False
    max_subjects_per_kind: int = DEFAULT_MAX_SUBJECTS_PER_KIND
    external_profiles_selected: int = 0
    internal_customers_selected: int = 0
    enumeration_truncated: bool = False
    subjects_attempted: int = 0
    subjects_succeeded: int = 0
    subjects_failed: int = 0
    subjects_skipped_cross_tenant: int = 0
    coverage_rows_created: int = 0
    coverage_rows_updated: int = 0
    committed: bool = False
    linked_orders_in_scope_total: int = 0
    unmapped_orders_in_scope_total: int = 0
    mislinked_orders_in_scope_total: int = 0
    failure_categories: Dict[str, int] = field(
        default_factory=lambda: {
            "subject_exception": 0,
            "cross_tenant_rejected": 0,
        }
    )
    write_generated_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "write_schema_version": self.write_schema_version,
            "tenant_id": int(self.tenant_id),
            "dry_run": bool(self.dry_run),
            "read_only": bool(self.read_only),
            "outcome": self.outcome,
            "access_status": self.access_status,
            "gate_stage": self.gate_stage,
            "gate_error_class": self.gate_error_class,
            "tenant_present": bool(self.tenant_present),
            "capability": {
                "key": self.capability_key,
                "state": self.capability_state,
                "state_readable": bool(self.capability_state_readable),
                "validation_revision": self.capability_validation_revision,
                "alembic_revision": self.alembic_revision,
                "revision_0087_compatible": bool(self.revision_0087_compatible),
            },
            "batch": {
                "max_subjects_per_kind": int(self.max_subjects_per_kind),
                "external_profiles_selected": int(self.external_profiles_selected),
                "internal_customers_selected": int(self.internal_customers_selected),
                "enumeration_truncated": bool(self.enumeration_truncated),
            },
            "execution": {
                "subjects_attempted": int(self.subjects_attempted),
                "subjects_succeeded": int(self.subjects_succeeded),
                "subjects_failed": int(self.subjects_failed),
                "subjects_skipped_cross_tenant": int(self.subjects_skipped_cross_tenant),
                "coverage_rows_created": int(self.coverage_rows_created),
                "coverage_rows_updated": int(self.coverage_rows_updated),
                "committed": bool(self.committed),
            },
            "aggregate": {
                "linked_orders_in_scope_total": int(self.linked_orders_in_scope_total),
                "unmapped_orders_in_scope_total": int(self.unmapped_orders_in_scope_total),
                "mislinked_orders_in_scope_total": int(self.mislinked_orders_in_scope_total),
            },
            "failure_categories": dict(self.failure_categories),
            "write_generated_at_utc": self.write_generated_at_utc,
        }

    def summary_line(self) -> str:
        return (
            f"a1_reconciliation_write tenant={self.tenant_id} "
            f"dry_run={int(self.dry_run)} outcome={self.outcome} "
            f"attempted={self.subjects_attempted} succeeded={self.subjects_succeeded} "
            f"failed={self.subjects_failed} committed={int(self.committed)}"
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_alembic_revision(db: Session) -> Optional[str]:
    try:
        row = db.execute(_REVISION_SQL).first()
        return str(row[0]).strip() if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def read_capability_detail(db: Session) -> Tuple[Optional[str], Optional[str]]:
    try:
        row = db.execute(
            _CAPABILITY_DETAIL_SQL,
            {"capability_key": CAPABILITY_KEY},
        ).mappings().first()
        if row is None:
            return None, None
        state = str(row["state"]).strip() if row["state"] is not None else None
        validation_revision = (
            str(row["validation_revision"]).strip()
            if row["validation_revision"] is not None
            else None
        )
        return state, validation_revision
    except Exception:  # noqa: BLE001
        return None, None


def validate_capability_and_revision_gates(
    db: Session,
) -> WriteGateFailure | None:
    revision = read_alembic_revision(db)
    if revision is None:
        return WriteGateFailure("revision_rejected", "alembic_version_missing")
    if revision not in ALEMBIC_REVISIONS_0087_COMPATIBLE:
        return WriteGateFailure("revision_rejected", "revision_not_0087_compatible")

    state, validation_revision = read_capability_detail(db)
    if state is None:
        return WriteGateFailure("capability_rejected", "capability_state_missing")
    if state == CAPABILITY_STATE_VALIDATED:
        return WriteGateFailure("capability_rejected", "capability_state_validated")
    if state != CONTRACT_CAPABILITY_STATE_EXPAND:
        return WriteGateFailure("capability_rejected", "capability_state_not_expand")
    if validation_revision is not None:
        return WriteGateFailure("capability_rejected", "capability_validation_revision_set")
    return None


def validate_write_input(
    *,
    tenant_id: int,
    max_subjects_per_kind: int,
) -> WriteGateFailure | None:
    if int(tenant_id) <= 0:
        return WriteGateFailure("input_rejected", "invalid_tenant_scope")
    if not 1 <= int(max_subjects_per_kind) <= MAX_SUBJECTS_PER_KIND:
        return WriteGateFailure("input_rejected", "invalid_batch_size")
    return None


def _enumerate_external_profiles(
    db: Session,
    *,
    tenant_id: int,
    max_subjects: int,
) -> Tuple[List[Any], bool]:
    from models import ExternalCustomerProfile  # noqa: PLC0415

    total = (
        db.query(ExternalCustomerProfile.id)
        .filter(ExternalCustomerProfile.tenant_id == int(tenant_id))
        .count()
    )
    profiles = (
        db.query(ExternalCustomerProfile)
        .filter(ExternalCustomerProfile.tenant_id == int(tenant_id))
        .order_by(ExternalCustomerProfile.created_at.asc(), ExternalCustomerProfile.id.asc())
        .limit(int(max_subjects))
        .all()
    )
    truncated = total > len(profiles)
    return profiles, truncated


def _count_coverage_rows_before(
    db: Session,
    *,
    tenant_id: int,
    profile_ids: List[Any],
    customer_ids: List[int],
) -> int:
    from models import (  # noqa: PLC0415
        ExternalCustomerProfileOrderHistoryCoverage,
        NahlaInternalCustomerOrderHistoryCoverage,
    )

    external_count = 0
    if profile_ids:
        external_count = (
            db.query(ExternalCustomerProfileOrderHistoryCoverage.id)
            .filter(
                ExternalCustomerProfileOrderHistoryCoverage.tenant_id == int(tenant_id),
                ExternalCustomerProfileOrderHistoryCoverage.external_customer_profile_id.in_(
                    profile_ids
                ),
            )
            .count()
        )
    internal_count = 0
    if customer_ids:
        internal_count = (
            db.query(NahlaInternalCustomerOrderHistoryCoverage.id)
            .filter(
                NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
                NahlaInternalCustomerOrderHistoryCoverage.customer_id.in_(customer_ids),
            )
            .count()
        )
    return int(external_count + internal_count)


def execute_order_customer_identity_reconciliation_write(
    db: Session,
    tenant_id: int,
    *,
    dry_run: bool = True,
    max_subjects_per_kind: int = DEFAULT_MAX_SUBJECTS_PER_KIND,
) -> OrderCustomerIdentityReconciliationWriteResult:
    """
    Bounded tenant-scoped reconciliation write (or dry-run preview).

    Individual subject failures are independent and safe to continue; outcome
    reflects partial success honestly. Commit failure rolls back all writes.
    """
    result = OrderCustomerIdentityReconciliationWriteResult(
        tenant_id=int(tenant_id),
        dry_run=bool(dry_run),
        read_only=bool(dry_run),
        max_subjects_per_kind=int(max_subjects_per_kind),
        write_generated_at_utc=_utcnow_iso(),
    )

    try:
        input_failure = validate_write_input(
            tenant_id=tenant_id,
            max_subjects_per_kind=max_subjects_per_kind,
        )
        if input_failure:
            result.outcome = "failed"
            result.access_status = "gate_rejected"
            result.gate_stage = input_failure.stage
            result.gate_error_class = input_failure.error_class
            return result

        from models import Tenant  # noqa: PLC0415

        tenant = db.query(Tenant.id).filter(Tenant.id == int(tenant_id)).first()
        result.tenant_present = tenant is not None
        if not result.tenant_present:
            result.outcome = "failed"
            result.access_status = "tenant_missing"
            return result

        capability_failure = validate_capability_and_revision_gates(db)
        result.alembic_revision = read_alembic_revision(db)
        result.revision_0087_compatible = (
            result.alembic_revision in ALEMBIC_REVISIONS_0087_COMPATIBLE
        )
        state, validation_revision = read_capability_detail(db)
        result.capability_state = state or read_order_customer_identity_capability_state(db)
        result.capability_state_readable = result.capability_state is not None
        result.capability_validation_revision = validation_revision
        if capability_failure:
            result.outcome = "failed"
            result.access_status = "gate_rejected"
            result.gate_stage = capability_failure.stage
            result.gate_error_class = capability_failure.error_class
            return result

        external_profiles, external_truncated = _enumerate_external_profiles(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )
        internal_ids, internal_truncated = _internal_customer_ids_for_tenant(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )
        result.external_profiles_selected = len(external_profiles)
        result.internal_customers_selected = len(internal_ids)
        result.enumeration_truncated = external_truncated or internal_truncated
        if result.enumeration_truncated:
            result.outcome = "failed"
            result.access_status = "enumeration_truncated"
            return result

        external_rollup = _rollup_external_profiles(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )
        internal_rollup = _rollup_internal_customers(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )
        result.linked_orders_in_scope_total = (
            external_rollup.linked_orders_in_scope_total
            + internal_rollup.linked_orders_in_scope_total
        )
        result.unmapped_orders_in_scope_total = (
            external_rollup.unmapped_orders_in_scope_total
            + internal_rollup.unmapped_orders_in_scope_total
        )
        result.mislinked_orders_in_scope_total = (
            external_rollup.mislinked_orders_in_scope_total
            + internal_rollup.mislinked_orders_in_scope_total
        )

        subjects_total = len(external_profiles) + len(internal_ids)
        result.subjects_attempted = subjects_total

        if dry_run:
            result.subjects_succeeded = subjects_total
            result.outcome = "success"
            result.access_status = "ok"
            return result

        coverage_rows_before = _count_coverage_rows_before(
            db,
            tenant_id=int(tenant_id),
            profile_ids=[profile.id for profile in external_profiles],
            customer_ids=internal_ids,
        )

        for profile in external_profiles:
            if int(profile.tenant_id) != int(tenant_id):
                result.subjects_skipped_cross_tenant += 1
                result.failure_categories["cross_tenant_rejected"] += 1
                result.subjects_failed += 1
                continue
            try:
                reconcile_external_profile_coverage(db, profile=profile)
                result.subjects_succeeded += 1
            except Exception as exc:  # noqa: BLE001
                log_reconciliation_write_failure(exception_class=type(exc).__name__)
                result.subjects_failed += 1
                result.failure_categories["subject_exception"] += 1

        for customer_id in internal_ids:
            try:
                reconcile_internal_customer_coverage(
                    db,
                    tenant_id=int(tenant_id),
                    customer_id=int(customer_id),
                )
                result.subjects_succeeded += 1
            except Exception as exc:  # noqa: BLE001
                log_reconciliation_write_failure(exception_class=type(exc).__name__)
                result.subjects_failed += 1
                result.failure_categories["subject_exception"] += 1

        if result.subjects_succeeded == 0:
            db.rollback()
            result.outcome = "failed"
            result.access_status = "degraded"
            result.committed = False
            return result

        try:
            db.commit()
            result.committed = True
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log_reconciliation_write_failure(exception_class=type(exc).__name__)
            result.outcome = "failed"
            result.access_status = "degraded"
            result.committed = False
            result.failure_categories["subject_exception"] += 1
            return result

        coverage_rows_after = _count_coverage_rows_before(
            db,
            tenant_id=int(tenant_id),
            profile_ids=[profile.id for profile in external_profiles],
            customer_ids=internal_ids,
        )
        result.coverage_rows_created = max(0, coverage_rows_after - coverage_rows_before)
        result.coverage_rows_updated = max(
            0,
            result.subjects_succeeded - result.coverage_rows_created,
        )
        if result.subjects_failed > 0:
            result.outcome = "partial"
            result.access_status = "degraded"
        else:
            result.outcome = "success"
            result.access_status = "ok"
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log_reconciliation_write_failure(exception_class=type(exc).__name__)
        result.outcome = "failed"
        result.access_status = "degraded"
        result.committed = False
        result.failure_categories["subject_exception"] += 1
        return result


__all__ = [
    "OrderCustomerIdentityReconciliationWriteResult",
    "WriteGateFailure",
    "execute_order_customer_identity_reconciliation_write",
    "read_alembic_revision",
    "read_capability_detail",
    "validate_capability_and_revision_gates",
    "validate_write_input",
]
