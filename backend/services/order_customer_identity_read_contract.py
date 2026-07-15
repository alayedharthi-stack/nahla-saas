"""
Per-subject safe read contracts (A1-v3.7).

No cross-subject aggregate. No PII.

``policy_eligibility_ready`` on these safe per-subject proofs is an
evidence-derived eligibility signal. It can become true only after the
capability is validated and the complete per-subject proof passes.

This is distinct from the tenant-level reconciliation report's identically
named field, which intentionally remains false and must never be consumed as
per-subject policy eligibility. Future AI and conditional-coupon consumers
must use only these safe per-subject proof builders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from services.order_customer_identity_capability import (
    cap_coverage_status_for_capability,
    order_customer_identity_reconciliation_ready,
)
from services.order_customer_identity_contract import (
    EXTERNAL_COVERAGE_SCOPE_CLAIM,
    INTERNAL_COVERAGE_SCOPE_CLAIM,
    NAHLA_INTERNAL_ORDER_V1,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_STALE,
    derive_policy_eligibility_ready,
)


@dataclass(frozen=True)
class SafeExternalProfileSourceHistoryProof:
    subject_kind: str
    identity_namespace: str
    integration_connection_present: bool
    authoritative_source_history_completeness: str
    forward_sync_health: str
    linked_orders_in_scope_count: int
    unmapped_orders_in_scope_count: int
    mislinked_orders_in_scope_count: int
    watermark_present: bool
    coverage_scope_claim: str
    policy_eligibility_ready: bool


@dataclass(frozen=True)
class SafeInternalCustomerSourceHistoryProof:
    subject_kind: str
    identity_namespace: str
    authoritative_source_history_completeness: str
    forward_sync_health: str
    linked_orders_in_scope_count: int
    unmapped_orders_in_scope_count: int
    mislinked_orders_in_scope_count: int
    watermark_present: bool
    coverage_scope_claim: str
    policy_eligibility_ready: bool


def build_safe_external_profile_proof(
    db: Session,
    *,
    tenant_id: int,
    external_customer_profile_id: UUID,
) -> Optional[SafeExternalProfileSourceHistoryProof]:
    from models import (  # noqa: PLC0415
        ExternalCustomerProfile,
        ExternalCustomerProfileOrderHistoryCoverage,
    )

    profile = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.id == external_customer_profile_id,
            ExternalCustomerProfile.tenant_id == int(tenant_id),
        )
        .first()
    )
    if profile is None:
        return None

    cov = (
        db.query(ExternalCustomerProfileOrderHistoryCoverage)
        .filter_by(external_customer_profile_id=profile.id)
        .first()
    )
    completeness = cov.authoritative_source_history_completeness if cov else SOURCE_HISTORY_INCOMPLETE
    forward_health = cov.forward_sync_health if cov else SYNC_HEALTH_STALE
    completeness, forward_health = cap_coverage_status_for_capability(
        db,
        completeness=completeness,
        forward_health=forward_health,
    )
    capability_validated = order_customer_identity_reconciliation_ready(db)
    linked = int(cov.linked_orders_in_scope_count if cov else 0)
    unmapped = int(cov.unmapped_orders_in_scope_count if cov else 0)
    mislinked = int(cov.mislinked_orders_in_scope_count if cov else 0)
    watermark_present = bool(cov and cov.watermark_at)
    return SafeExternalProfileSourceHistoryProof(
        subject_kind="external_customer_profile",
        identity_namespace=str(profile.identity_namespace),
        integration_connection_present=profile.integration_connection_id is not None,
        authoritative_source_history_completeness=completeness,
        forward_sync_health=forward_health,
        linked_orders_in_scope_count=linked,
        unmapped_orders_in_scope_count=unmapped,
        mislinked_orders_in_scope_count=mislinked,
        watermark_present=watermark_present,
        coverage_scope_claim=EXTERNAL_COVERAGE_SCOPE_CLAIM,
        policy_eligibility_ready=derive_policy_eligibility_ready(
            capability_validated=capability_validated,
            identity_namespace=str(profile.identity_namespace),
            coverage_row_present=cov is not None,
            authoritative_source_history_completeness=completeness,
            forward_sync_health=forward_health,
            linked_orders_in_scope_count=linked,
            unmapped_orders_in_scope_count=unmapped,
            mislinked_orders_in_scope_count=mislinked,
            watermark_present=watermark_present,
            integration_connection_present=profile.integration_connection_id is not None,
        ),
    )


def build_safe_internal_customer_proof(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
) -> Optional[SafeInternalCustomerSourceHistoryProof]:
    from models import NahlaInternalCustomerOrderHistoryCoverage  # noqa: PLC0415

    cov = (
        db.query(NahlaInternalCustomerOrderHistoryCoverage)
        .filter_by(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        )
        .first()
    )
    completeness = cov.authoritative_source_history_completeness if cov else SOURCE_HISTORY_INCOMPLETE
    forward_health = cov.forward_sync_health if cov else SYNC_HEALTH_STALE
    completeness, forward_health = cap_coverage_status_for_capability(
        db,
        completeness=completeness,
        forward_health=forward_health,
    )
    capability_validated = order_customer_identity_reconciliation_ready(db)
    linked = int(cov.linked_orders_in_scope_count if cov else 0)
    unmapped = int(cov.unmapped_orders_in_scope_count if cov else 0)
    mislinked = int(cov.mislinked_orders_in_scope_count if cov else 0)
    watermark_present = bool(cov and cov.watermark_at)
    return SafeInternalCustomerSourceHistoryProof(
        subject_kind="nahla_internal_customer",
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        authoritative_source_history_completeness=completeness,
        forward_sync_health=forward_health,
        linked_orders_in_scope_count=linked,
        unmapped_orders_in_scope_count=unmapped,
        mislinked_orders_in_scope_count=mislinked,
        watermark_present=watermark_present,
        coverage_scope_claim=INTERNAL_COVERAGE_SCOPE_CLAIM,
        policy_eligibility_ready=derive_policy_eligibility_ready(
            capability_validated=capability_validated,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            coverage_row_present=cov is not None,
            authoritative_source_history_completeness=completeness,
            forward_sync_health=forward_health,
            linked_orders_in_scope_count=linked,
            unmapped_orders_in_scope_count=unmapped,
            mislinked_orders_in_scope_count=mislinked,
            watermark_present=watermark_present,
        ),
    )


__all__ = [
    "SafeExternalProfileSourceHistoryProof",
    "SafeInternalCustomerSourceHistoryProof",
    "build_safe_external_profile_proof",
    "build_safe_internal_customer_proof",
]
