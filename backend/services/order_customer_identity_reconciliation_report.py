"""
Tenant-scoped, read-only A1 reconciliation operator report (G4 gate).

Computes tuple-scoped linkage evidence from orders without mutating coverage rows.
Does not call reconcile_* write helpers. policy_eligibility_ready is always false.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services.order_customer_identity_capability import (
    cap_coverage_status_for_capability,
    order_customer_identity_reconciliation_ready,
    read_order_customer_identity_capability_state,
)
from services.order_customer_identity_contract import (
    CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY,
    CAPABILITY_STATE_EXPAND,
    EXTERNAL_COVERAGE_SCOPE_CLAIM,
    INTERNAL_COVERAGE_SCOPE_CLAIM,
    NAHLA_INTERNAL_ORDER_V1,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
    POLICY_ELIGIBILITY_READY,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)
from services.order_customer_identity_logging import log_reconciliation_report_failure
from services.order_customer_identity_reconciliation_classification import (
    TupleLinkageCounts,
    classify_external_tuple_order,
    classify_internal_customer_order,
    count_classifications,
)

REPORT_SCHEMA_VERSION = "a1_reconciliation_report_v1"
DEFAULT_MAX_SUBJECTS_PER_KIND = 1_000
MAX_SUBJECTS_PER_KIND = 1_000
MAX_ORDERS_PER_SUBJECT = 1_000


@dataclass(frozen=True)
class _SubjectRollup:
    subjects_total: int
    subjects_enumerated: int
    enumeration_truncated: bool
    order_enumeration_truncated: bool
    subjects_with_orders_in_scope: int
    subjects_tuple_linkage_clean: int
    subjects_coverage_row_present: int
    subjects_watermark_present: int
    subjects_runtime_complete: int
    subjects_runtime_incomplete: int
    subjects_forward_healthy: int
    subjects_forward_degraded: int
    subjects_forward_stale: int
    linked_orders_in_scope_total: int
    unmapped_orders_in_scope_total: int
    mislinked_orders_in_scope_total: int


@dataclass
class OrderCustomerIdentityReconciliationReport:
    report_schema_version: str = REPORT_SCHEMA_VERSION
    tenant_id: int = 0
    dry_run: bool = True
    read_only: bool = True
    tenant_present: bool = False
    policy_eligibility_ready: bool = POLICY_ELIGIBILITY_READY
    capability_key: str = CAPABILITY_KEY_ORDER_CUSTOMER_IDENTITY
    capability_state: Optional[str] = None
    capability_state_readable: bool = False
    reconciliation_consumer_ready: bool = False
    external_profiles: Dict[str, Any] = field(default_factory=dict)
    internal_customers: Dict[str, Any] = field(default_factory=dict)
    aggregate: Dict[str, Any] = field(default_factory=dict)
    evidence_gates: Dict[str, bool] = field(default_factory=dict)
    ready_for_validate: bool = False
    readiness_blockers: List[str] = field(default_factory=list)
    access_status: str = "ok"
    report_generated_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_schema_version": self.report_schema_version,
            "tenant_id": int(self.tenant_id),
            "dry_run": bool(self.dry_run),
            "read_only": bool(self.read_only),
            "tenant_present": bool(self.tenant_present),
            "policy_eligibility_ready": bool(self.policy_eligibility_ready),
            "coverage_scope_claims": {
                "external": EXTERNAL_COVERAGE_SCOPE_CLAIM,
                "internal": INTERNAL_COVERAGE_SCOPE_CLAIM,
            },
            "capability": {
                "key": self.capability_key,
                "state": self.capability_state,
                "state_readable": bool(self.capability_state_readable),
                "reconciliation_consumer_ready": bool(self.reconciliation_consumer_ready),
            },
            "external_profiles": dict(self.external_profiles),
            "internal_customers": dict(self.internal_customers),
            "aggregate": dict(self.aggregate),
            "evidence_gates": dict(self.evidence_gates),
            "ready_for_validate": bool(self.ready_for_validate),
            "readiness_blockers": list(self.readiness_blockers),
            "access_status": self.access_status,
            "report_generated_at_utc": self.report_generated_at_utc,
        }

    def summary_line(self) -> str:
        agg = self.aggregate
        return (
            f"a1_reconciliation tenant={self.tenant_id} "
            f"ready_for_validate={int(self.ready_for_validate)} "
            f"subjects={agg.get('subjects_total', 0)} "
            f"linked_orders={agg.get('linked_orders_in_scope_total', 0)} "
            f"unmapped_orders={agg.get('unmapped_orders_in_scope_total', 0)} "
            f"mislinked_orders={agg.get('mislinked_orders_in_scope_total', 0)} "
            f"blockers={len(self.readiness_blockers)}"
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rollup_to_dict(rollup: _SubjectRollup) -> Dict[str, Any]:
    return {
        "subjects_total": rollup.subjects_total,
        "subjects_enumerated": rollup.subjects_enumerated,
        "enumeration_truncated": rollup.enumeration_truncated,
        "order_enumeration_truncated": rollup.order_enumeration_truncated,
        "subjects_with_orders_in_scope": rollup.subjects_with_orders_in_scope,
        "subjects_tuple_linkage_clean": rollup.subjects_tuple_linkage_clean,
        "subjects_coverage_row_present": rollup.subjects_coverage_row_present,
        "subjects_watermark_present": rollup.subjects_watermark_present,
        "subjects_runtime_complete": rollup.subjects_runtime_complete,
        "subjects_runtime_incomplete": rollup.subjects_runtime_incomplete,
        "subjects_forward_healthy": rollup.subjects_forward_healthy,
        "subjects_forward_degraded": rollup.subjects_forward_degraded,
        "subjects_forward_stale": rollup.subjects_forward_stale,
        "linked_orders_in_scope_total": rollup.linked_orders_in_scope_total,
        "unmapped_orders_in_scope_total": rollup.unmapped_orders_in_scope_total,
        "mislinked_orders_in_scope_total": rollup.mislinked_orders_in_scope_total,
    }


def _merge_rollups(external: _SubjectRollup, internal: _SubjectRollup) -> Dict[str, Any]:
    return {
        "subjects_total": external.subjects_total + internal.subjects_total,
        "subjects_enumerated": external.subjects_enumerated + internal.subjects_enumerated,
        "enumeration_truncated": external.enumeration_truncated or internal.enumeration_truncated,
        "order_enumeration_truncated": (
            external.order_enumeration_truncated or internal.order_enumeration_truncated
        ),
        "subjects_with_orders_in_scope": (
            external.subjects_with_orders_in_scope + internal.subjects_with_orders_in_scope
        ),
        "subjects_tuple_linkage_clean": (
            external.subjects_tuple_linkage_clean + internal.subjects_tuple_linkage_clean
        ),
        "subjects_coverage_row_present": (
            external.subjects_coverage_row_present + internal.subjects_coverage_row_present
        ),
        "subjects_watermark_present": (
            external.subjects_watermark_present + internal.subjects_watermark_present
        ),
        "subjects_runtime_complete": (
            external.subjects_runtime_complete + internal.subjects_runtime_complete
        ),
        "subjects_runtime_incomplete": (
            external.subjects_runtime_incomplete + internal.subjects_runtime_incomplete
        ),
        "subjects_forward_healthy": (
            external.subjects_forward_healthy + internal.subjects_forward_healthy
        ),
        "subjects_forward_degraded": (
            external.subjects_forward_degraded + internal.subjects_forward_degraded
        ),
        "subjects_forward_stale": external.subjects_forward_stale + internal.subjects_forward_stale,
        "linked_orders_in_scope_total": (
            external.linked_orders_in_scope_total + internal.linked_orders_in_scope_total
        ),
        "unmapped_orders_in_scope_total": (
            external.unmapped_orders_in_scope_total + internal.unmapped_orders_in_scope_total
        ),
        "mislinked_orders_in_scope_total": (
            external.mislinked_orders_in_scope_total + internal.mislinked_orders_in_scope_total
        ),
    }


def _counts_and_truncation(
    classifications: List[str],
) -> Tuple[TupleLinkageCounts, bool]:
    """A bounded subject read: one extra row proves an incomplete enumeration."""
    truncated = len(classifications) > MAX_ORDERS_PER_SUBJECT
    return count_classifications(classifications[:MAX_ORDERS_PER_SUBJECT]), truncated


def _compute_external_tuple_counts_readonly(
    db: Session,
    *,
    profile: Any,
) -> Tuple[TupleLinkageCounts, bool]:
    from models import Order  # noqa: PLC0415

    orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == int(profile.tenant_id),
            Order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER,
            Order.identity_namespace == profile.identity_namespace,
            Order.integration_connection_id == int(profile.integration_connection_id),
            Order.external_customer_ref == profile.external_customer_ref,
        )
        .order_by(Order.id.asc())
        .limit(MAX_ORDERS_PER_SUBJECT + 1)
        .all()
    )
    return _counts_and_truncation([
        classify_external_tuple_order(order=order, profile_id=profile.id)
        for order in orders
    ])


def _compute_internal_tuple_counts_readonly(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> Tuple[TupleLinkageCounts, bool]:
    from models import Order  # noqa: PLC0415

    orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.customer_id == int(customer_id),
            Order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL,
        )
        .order_by(Order.id.asc())
        .limit(MAX_ORDERS_PER_SUBJECT + 1)
        .all()
    )
    return _counts_and_truncation([
        classify_internal_customer_order(order=order) for order in orders
    ])


def _accumulate_subject_rollup(
    rollup: _SubjectRollup,
    *,
    counts: TupleLinkageCounts,
    completeness: str,
    forward_health: str,
    coverage_row_present: bool,
    watermark_present: bool,
) -> _SubjectRollup:
    return _SubjectRollup(
        subjects_total=rollup.subjects_total,
        subjects_enumerated=rollup.subjects_enumerated,
        enumeration_truncated=rollup.enumeration_truncated,
        order_enumeration_truncated=rollup.order_enumeration_truncated,
        subjects_with_orders_in_scope=rollup.subjects_with_orders_in_scope
        + (1 if counts.orders_in_scope > 0 else 0),
        subjects_tuple_linkage_clean=rollup.subjects_tuple_linkage_clean
        + (
            1
            if counts.unmapped == 0
            and counts.mislinked == 0
            and counts.linked > 0
            else 0
        ),
        subjects_coverage_row_present=rollup.subjects_coverage_row_present
        + (1 if coverage_row_present else 0),
        subjects_watermark_present=rollup.subjects_watermark_present
        + (1 if watermark_present else 0),
        subjects_runtime_complete=rollup.subjects_runtime_complete
        + (1 if completeness == SOURCE_HISTORY_COMPLETE else 0),
        subjects_runtime_incomplete=rollup.subjects_runtime_incomplete
        + (0 if completeness == SOURCE_HISTORY_COMPLETE else 1),
        subjects_forward_healthy=rollup.subjects_forward_healthy
        + (1 if forward_health == SYNC_HEALTH_HEALTHY else 0),
        subjects_forward_degraded=rollup.subjects_forward_degraded
        + (1 if forward_health == SYNC_HEALTH_DEGRADED else 0),
        subjects_forward_stale=rollup.subjects_forward_stale
        + (1 if forward_health == SYNC_HEALTH_STALE else 0),
        linked_orders_in_scope_total=rollup.linked_orders_in_scope_total + counts.linked,
        unmapped_orders_in_scope_total=rollup.unmapped_orders_in_scope_total + counts.unmapped,
        mislinked_orders_in_scope_total=rollup.mislinked_orders_in_scope_total + counts.mislinked,
    )


def _runtime_status_from_counts(
    db: Session,
    *,
    counts: TupleLinkageCounts,
) -> Tuple[str, str]:
    if counts.unmapped == 0 and counts.mislinked == 0 and counts.linked > 0:
        completeness = SOURCE_HISTORY_COMPLETE
        forward_health = SYNC_HEALTH_HEALTHY
    else:
        completeness = SOURCE_HISTORY_INCOMPLETE
        forward_health = SYNC_HEALTH_DEGRADED if counts.orders_in_scope > 0 else SYNC_HEALTH_STALE
    return cap_coverage_status_for_capability(
        db,
        completeness=completeness,
        forward_health=forward_health,
    )


def _rollup_external_profiles(
    db: Session,
    *,
    tenant_id: int,
    max_subjects: int,
) -> _SubjectRollup:
    from models import (  # noqa: PLC0415
        ExternalCustomerProfile,
        ExternalCustomerProfileOrderHistoryCoverage,
    )

    total = (
        db.query(ExternalCustomerProfile.id)
        .filter(ExternalCustomerProfile.tenant_id == int(tenant_id))
        .count()
    )
    profile_rows = (
        db.query(ExternalCustomerProfile)
        .filter(ExternalCustomerProfile.tenant_id == int(tenant_id))
        .order_by(ExternalCustomerProfile.created_at.asc(), ExternalCustomerProfile.id.asc())
        .limit(int(max_subjects))
        .all()
    )
    truncated = total > len(profile_rows)

    rollup = _SubjectRollup(
        subjects_total=total,
        subjects_enumerated=len(profile_rows),
        enumeration_truncated=truncated,
        order_enumeration_truncated=False,
        subjects_with_orders_in_scope=0,
        subjects_tuple_linkage_clean=0,
        subjects_coverage_row_present=0,
        subjects_watermark_present=0,
        subjects_runtime_complete=0,
        subjects_runtime_incomplete=0,
        subjects_forward_healthy=0,
        subjects_forward_degraded=0,
        subjects_forward_stale=0,
        linked_orders_in_scope_total=0,
        unmapped_orders_in_scope_total=0,
        mislinked_orders_in_scope_total=0,
    )

    for profile in profile_rows:
        counts, order_truncated = _compute_external_tuple_counts_readonly(db, profile=profile)
        completeness, forward_health = _runtime_status_from_counts(db, counts=counts)
        cov = (
            db.query(ExternalCustomerProfileOrderHistoryCoverage)
            .filter_by(external_customer_profile_id=profile.id)
            .first()
        )
        rollup = _accumulate_subject_rollup(
            rollup,
            counts=counts,
            completeness=completeness,
            forward_health=forward_health,
            coverage_row_present=cov is not None,
            watermark_present=bool(cov and cov.watermark_at),
        )
        if order_truncated:
            rollup = _SubjectRollup(
                **{**rollup.__dict__, "order_enumeration_truncated": True}
            )

    return rollup


def _external_orphan_tuple_orders_total(db: Session, *, tenant_id: int) -> int:
    """Count authoritative external tuples lacking a profile without exposing tuples."""
    from models import Order  # noqa: PLC0415

    return int(
        db.query(Order.id)
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER,
            Order.identity_namespace.isnot(None),
            Order.integration_connection_id.isnot(None),
            Order.external_customer_ref.isnot(None),
            Order.external_customer_profile_id.is_(None),
        )
        .count()
    )


def _internal_customer_ids_for_tenant(
    db: Session,
    *,
    tenant_id: int,
    max_subjects: int,
) -> Tuple[List[int], bool]:
    from models import NahlaInternalCustomerOrderHistoryCoverage, Order  # noqa: PLC0415

    from_orders = {
        int(row[0])
        for row in db.query(Order.customer_id)
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL,
            Order.customer_id.isnot(None),
        )
        .distinct()
        .order_by(Order.customer_id.asc())
        .limit(int(max_subjects) + 1)
        .all()
        if row[0] is not None
    }
    from_coverage = {
        int(row[0])
        for row in db.query(NahlaInternalCustomerOrderHistoryCoverage.customer_id)
        .filter(
            NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
            NahlaInternalCustomerOrderHistoryCoverage.identity_namespace == NAHLA_INTERNAL_ORDER_V1,
        )
        .distinct()
        .order_by(NahlaInternalCustomerOrderHistoryCoverage.customer_id.asc())
        .limit(int(max_subjects) + 1)
        .all()
    }
    subject_ids = sorted(from_orders | from_coverage)
    truncated = (
        len(from_orders) > int(max_subjects)
        or len(from_coverage) > int(max_subjects)
        or len(subject_ids) > int(max_subjects)
    )
    return subject_ids[: int(max_subjects)], truncated


def _rollup_internal_customers(
    db: Session,
    *,
    tenant_id: int,
    max_subjects: int,
) -> _SubjectRollup:
    from models import NahlaInternalCustomerOrderHistoryCoverage  # noqa: PLC0415

    enumerated_ids, truncated = _internal_customer_ids_for_tenant(
        db,
        tenant_id=tenant_id,
        max_subjects=max_subjects,
    )
    total = len(enumerated_ids) + (1 if truncated else 0)

    rollup = _SubjectRollup(
        subjects_total=total,
        subjects_enumerated=len(enumerated_ids),
        enumeration_truncated=truncated,
        order_enumeration_truncated=False,
        subjects_with_orders_in_scope=0,
        subjects_tuple_linkage_clean=0,
        subjects_coverage_row_present=0,
        subjects_watermark_present=0,
        subjects_runtime_complete=0,
        subjects_runtime_incomplete=0,
        subjects_forward_healthy=0,
        subjects_forward_degraded=0,
        subjects_forward_stale=0,
        linked_orders_in_scope_total=0,
        unmapped_orders_in_scope_total=0,
        mislinked_orders_in_scope_total=0,
    )

    for customer_id in enumerated_ids:
        counts, order_truncated = _compute_internal_tuple_counts_readonly(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        completeness, forward_health = _runtime_status_from_counts(db, counts=counts)
        cov = (
            db.query(NahlaInternalCustomerOrderHistoryCoverage)
            .filter_by(
                tenant_id=int(tenant_id),
                customer_id=int(customer_id),
                identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            )
            .first()
        )
        rollup = _accumulate_subject_rollup(
            rollup,
            counts=counts,
            completeness=completeness,
            forward_health=forward_health,
            coverage_row_present=cov is not None,
            watermark_present=bool(cov and cov.watermark_at),
        )
        if order_truncated:
            rollup = _SubjectRollup(
                **{**rollup.__dict__, "order_enumeration_truncated": True}
            )

    return rollup


def _build_evidence_gates(
    *,
    tenant_present: bool,
    capability_state: Optional[str],
    capability_state_readable: bool,
    external: _SubjectRollup,
    internal: _SubjectRollup,
    external_orphan_tuple_orders_total: int,
) -> Tuple[Dict[str, bool], List[str]]:
    gates: Dict[str, bool] = {
        "tenant_present": tenant_present,
        "capability_state_readable": capability_state_readable,
        "capability_state_is_expand": capability_state == CAPABILITY_STATE_EXPAND,
        "no_enumeration_truncation": not (
            external.enumeration_truncated or internal.enumeration_truncated
        ),
        "no_order_enumeration_truncation": not (
            external.order_enumeration_truncated or internal.order_enumeration_truncated
        ),
        "subjects_enumerated": (
            external.subjects_enumerated + internal.subjects_enumerated
        )
        > 0,
        "all_enumerated_subjects_tuple_linkage_clean": (
            external.subjects_tuple_linkage_clean == external.subjects_enumerated
            and internal.subjects_tuple_linkage_clean == internal.subjects_enumerated
            and (external.subjects_enumerated + internal.subjects_enumerated) > 0
        ),
        "all_enumerated_subjects_coverage_row_present": (
            external.subjects_coverage_row_present == external.subjects_enumerated
            and internal.subjects_coverage_row_present == internal.subjects_enumerated
        ),
        "all_enumerated_subjects_watermark_present": (
            external.subjects_watermark_present == external.subjects_enumerated
            and internal.subjects_watermark_present == internal.subjects_enumerated
        ),
        "no_unmapped_orders_in_scope": (
            external.unmapped_orders_in_scope_total == 0
            and internal.unmapped_orders_in_scope_total == 0
        ),
        "no_mislinked_orders_in_scope": (
            external.mislinked_orders_in_scope_total == 0
            and internal.mislinked_orders_in_scope_total == 0
        ),
        "no_external_orphan_tuple_orders": external_orphan_tuple_orders_total == 0,
        "linked_orders_present": (
            external.linked_orders_in_scope_total + internal.linked_orders_in_scope_total
        )
        > 0,
        "runtime_reconciliation_consumer_ready": False,
    }

    blockers: List[str] = []
    if not gates["tenant_present"]:
        blockers.append("tenant_missing")
    if not gates["capability_state_readable"]:
        blockers.append("capability_state_unreadable")
    if not gates["capability_state_is_expand"]:
        blockers.append("capability_not_in_expand")
    if not gates["no_enumeration_truncation"]:
        blockers.append("subject_enumeration_truncated")
    if not gates["no_order_enumeration_truncation"]:
        blockers.append("subject_order_enumeration_truncated")
    if not gates["subjects_enumerated"]:
        blockers.append("no_subjects_enumerated")
    if not gates["all_enumerated_subjects_tuple_linkage_clean"]:
        blockers.append("tuple_linkage_incomplete")
    if not gates["all_enumerated_subjects_coverage_row_present"]:
        blockers.append("coverage_row_missing")
    if not gates["all_enumerated_subjects_watermark_present"]:
        blockers.append("watermark_missing")
    if not gates["no_unmapped_orders_in_scope"]:
        blockers.append("unmapped_orders_present")
    if not gates["no_mislinked_orders_in_scope"]:
        blockers.append("mislinked_orders_present")
    if not gates["no_external_orphan_tuple_orders"]:
        blockers.append("external_orphan_tuple_orders_present")
    if not gates["linked_orders_present"]:
        blockers.append("no_linked_orders")

    return gates, blockers


def build_order_customer_identity_reconciliation_report(
    db: Session,
    tenant_id: int,
    *,
    max_subjects_per_kind: int = DEFAULT_MAX_SUBJECTS_PER_KIND,
) -> OrderCustomerIdentityReconciliationReport:
    """
    Build a tenant-scoped reconciliation report. Read-only — no coverage mutations.

    Raises no exceptions for data-access failures; returns access_status=degraded.
    """
    from models import Tenant  # noqa: PLC0415

    report = OrderCustomerIdentityReconciliationReport(
        tenant_id=int(tenant_id),
        dry_run=True,
        read_only=True,
        report_generated_at_utc=_utcnow_iso(),
    )

    try:
        if int(tenant_id) <= 0:
            raise ValueError("invalid_tenant_scope")
        if not 1 <= int(max_subjects_per_kind) <= MAX_SUBJECTS_PER_KIND:
            raise ValueError("invalid_max_subjects_per_kind")
        tenant = db.query(Tenant.id).filter(Tenant.id == int(tenant_id)).first()
        report.tenant_present = tenant is not None

        capability_state = read_order_customer_identity_capability_state(db)
        report.capability_state = capability_state
        report.capability_state_readable = capability_state is not None
        report.reconciliation_consumer_ready = order_customer_identity_reconciliation_ready(db)

        external = _rollup_external_profiles(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )
        internal = _rollup_internal_customers(
            db,
            tenant_id=int(tenant_id),
            max_subjects=max_subjects_per_kind,
        )

        report.external_profiles = _rollup_to_dict(external)
        report.external_profiles["orphan_tuple_orders_total"] = _external_orphan_tuple_orders_total(
            db,
            tenant_id=int(tenant_id),
        )
        report.internal_customers = _rollup_to_dict(internal)
        report.aggregate = _merge_rollups(external, internal)

        gates, blockers = _build_evidence_gates(
            tenant_present=report.tenant_present,
            capability_state=capability_state,
            capability_state_readable=report.capability_state_readable,
            external=external,
            internal=internal,
            external_orphan_tuple_orders_total=report.external_profiles[
                "orphan_tuple_orders_total"
            ],
        )
        report.evidence_gates = gates
        report.readiness_blockers = blockers
        report.ready_for_validate = report.tenant_present and len(blockers) == 0

        if not report.tenant_present:
            report.access_status = "tenant_missing"
        elif not report.capability_state_readable:
            report.access_status = "capability_unreadable"
        elif (
            external.enumeration_truncated
            or internal.enumeration_truncated
            or external.order_enumeration_truncated
            or internal.order_enumeration_truncated
        ):
            report.access_status = "enumeration_truncated"
        else:
            report.access_status = "ok"

    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — privacy-safe failure event only
        log_reconciliation_report_failure(exception_class=type(exc).__name__)
        report.access_status = "degraded"
        report.ready_for_validate = False
        report.readiness_blockers = ["access_degraded"]
        report.evidence_gates = {
            "tenant_present": report.tenant_present,
            "capability_state_readable": False,
            "capability_state_is_expand": False,
            "no_enumeration_truncation": False,
            "no_order_enumeration_truncation": False,
            "subjects_enumerated": False,
            "all_enumerated_subjects_tuple_linkage_clean": False,
            "all_enumerated_subjects_coverage_row_present": False,
            "all_enumerated_subjects_watermark_present": False,
            "no_unmapped_orders_in_scope": False,
            "no_mislinked_orders_in_scope": False,
            "no_external_orphan_tuple_orders": False,
            "linked_orders_present": False,
            "runtime_reconciliation_consumer_ready": False,
        }

    return report


__all__ = [
    "DEFAULT_MAX_SUBJECTS_PER_KIND",
    "MAX_ORDERS_PER_SUBJECT",
    "MAX_SUBJECTS_PER_KIND",
    "OrderCustomerIdentityReconciliationReport",
    "REPORT_SCHEMA_VERSION",
    "build_order_customer_identity_reconciliation_report",
]
