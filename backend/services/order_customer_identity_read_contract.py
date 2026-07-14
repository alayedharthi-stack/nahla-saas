"""
Per-subject safe read contracts (A1-v3.7).

No cross-subject aggregate. No PII. policy_eligibility_ready always false.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from services.order_customer_identity_contract import (
    EXTERNAL_COVERAGE_SCOPE_CLAIM,
    INTERNAL_COVERAGE_SCOPE_CLAIM,
    NAHLA_INTERNAL_ORDER_V1,
    POLICY_ELIGIBILITY_READY,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_STALE,
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
    return SafeExternalProfileSourceHistoryProof(
        subject_kind="external_customer_profile",
        identity_namespace=str(profile.identity_namespace),
        integration_connection_present=profile.integration_connection_id is not None,
        authoritative_source_history_completeness=(
            cov.authoritative_source_history_completeness if cov else SOURCE_HISTORY_INCOMPLETE
        ),
        forward_sync_health=cov.forward_sync_health if cov else SYNC_HEALTH_STALE,
        linked_orders_in_scope_count=int(cov.linked_orders_in_scope_count if cov else 0),
        unmapped_orders_in_scope_count=int(cov.unmapped_orders_in_scope_count if cov else 0),
        mislinked_orders_in_scope_count=int(cov.mislinked_orders_in_scope_count if cov else 0),
        watermark_present=bool(cov and cov.watermark_at),
        coverage_scope_claim=EXTERNAL_COVERAGE_SCOPE_CLAIM,
        policy_eligibility_ready=POLICY_ELIGIBILITY_READY,
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
    return SafeInternalCustomerSourceHistoryProof(
        subject_kind="nahla_internal_customer",
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        authoritative_source_history_completeness=(
            cov.authoritative_source_history_completeness if cov else SOURCE_HISTORY_INCOMPLETE
        ),
        forward_sync_health=cov.forward_sync_health if cov else SYNC_HEALTH_STALE,
        linked_orders_in_scope_count=int(cov.linked_orders_in_scope_count if cov else 0),
        unmapped_orders_in_scope_count=int(cov.unmapped_orders_in_scope_count if cov else 0),
        mislinked_orders_in_scope_count=int(cov.mislinked_orders_in_scope_count if cov else 0),
        watermark_present=bool(cov and cov.watermark_at),
        coverage_scope_claim=INTERNAL_COVERAGE_SCOPE_CLAIM,
        policy_eligibility_ready=POLICY_ELIGIBILITY_READY,
    )


__all__ = [
    "SafeExternalProfileSourceHistoryProof",
    "SafeInternalCustomerSourceHistoryProof",
    "build_safe_external_profile_proof",
    "build_safe_internal_customer_proof",
]
