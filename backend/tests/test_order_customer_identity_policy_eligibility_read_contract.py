"""Evidence-backed policy_eligibility_ready for A1 safe read contracts."""
from __future__ import annotations

import pytest

from services.order_customer_identity_contract import (
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    EXTERNAL_PROVIDER_SALLA_V1,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    NAHLA_INTERNAL_ORDER_V1,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
    derive_policy_eligibility_ready,
)
from services.order_customer_identity_read_contract import (
    build_safe_external_profile_proof,
    build_safe_internal_customer_proof,
)
from services.order_customer_identity_service import (
    reconcile_external_profile_coverage,
    reconcile_internal_customer_coverage,
)
from tests.order_customer_identity_postgres_fixtures import (
    TEST_TENANT_A,
    TEST_TENANT_B,
    clear_capability_state,
    pg_session,
    postgres_engine,
    seed_capability_state,
    seed_customer,
    seed_external_order,
    seed_external_profile,
    seed_integration,
    seed_internal_order,
    seed_tenant,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")

_READY_KWARGS = dict(
    capability_validated=True,
    identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
    coverage_row_present=True,
    authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
    forward_sync_health=SYNC_HEALTH_HEALTHY,
    linked_orders_in_scope_count=1,
    unmapped_orders_in_scope_count=0,
    mislinked_orders_in_scope_count=0,
    watermark_present=True,
    integration_connection_present=True,
)


def test_derive_policy_eligibility_ready_true_when_fully_evidenced() -> None:
    assert derive_policy_eligibility_ready(**_READY_KWARGS) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"capability_validated": False},
        {"identity_namespace": "unknown_namespace_v9"},
        {"coverage_row_present": False},
        {"integration_connection_present": False},
        {"watermark_present": False},
        {"authoritative_source_history_completeness": SOURCE_HISTORY_INCOMPLETE},
        {"forward_sync_health": SYNC_HEALTH_DEGRADED},
        {"forward_sync_health": SYNC_HEALTH_STALE},
        {"linked_orders_in_scope_count": 0},
        {"unmapped_orders_in_scope_count": 1},
        {"mislinked_orders_in_scope_count": 1},
    ],
)
def test_derive_policy_eligibility_ready_fail_closed(overrides: dict) -> None:
    kwargs = dict(_READY_KWARGS)
    kwargs.update(overrides)
    assert derive_policy_eligibility_ready(**kwargs) is False


def test_derive_internal_namespace_does_not_require_integration_connection() -> None:
    assert derive_policy_eligibility_ready(
        capability_validated=True,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        coverage_row_present=True,
        authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
        forward_sync_health=SYNC_HEALTH_HEALTHY,
        linked_orders_in_scope_count=2,
        unmapped_orders_in_scope_count=0,
        mislinked_orders_in_scope_count=0,
        watermark_present=True,
    ) is True


def _seed_generic_external_linked_scope(pg_session) -> tuple:
    """Generic commerce merchant — external profile with one authoritative linked order."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE-GEN")
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="GEN-CUST-1",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-GEN-1",
        integration_connection_id=intg.id,
        external_customer_ref="GEN-CUST-1",
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    return profile


def test_expand_capability_policy_eligibility_stays_false_despite_linked_scope(pg_session) -> None:
    profile = _seed_generic_external_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    reconcile_external_profile_coverage(pg_session, profile=profile)

    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.authoritative_source_history_completeness == SOURCE_HISTORY_INCOMPLETE
    assert proof.forward_sync_health == SYNC_HEALTH_DEGRADED
    assert proof.policy_eligibility_ready is False


def test_validated_capability_policy_eligibility_true_when_fully_evidenced(pg_session) -> None:
    profile = _seed_generic_external_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    reconcile_external_profile_coverage(pg_session, profile=profile)

    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.authoritative_source_history_completeness == SOURCE_HISTORY_COMPLETE
    assert proof.forward_sync_health == SYNC_HEALTH_HEALTHY
    assert proof.watermark_present is True
    assert proof.unmapped_orders_in_scope_count == 0
    assert proof.mislinked_orders_in_scope_count == 0
    assert proof.policy_eligibility_ready is True


def test_validated_unmapped_order_policy_eligibility_false(pg_session) -> None:
    profile = _seed_generic_external_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-GEN-UNMAPPED",
        integration_connection_id=profile.integration_connection_id,
        external_customer_ref="GEN-CUST-1",
        external_customer_profile_id=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )
    reconcile_external_profile_coverage(pg_session, profile=profile)

    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.policy_eligibility_ready is False


def test_validated_missing_capability_policy_eligibility_false(pg_session) -> None:
    profile = _seed_generic_external_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    reconcile_external_profile_coverage(pg_session, profile=profile)
    clear_capability_state(pg_session)

    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.policy_eligibility_ready is False


def test_internal_customer_validated_policy_eligibility_true(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A, name="نورة عبدالله")
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-INT-GEN",
        customer_id=cust.id,
    )
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    reconcile_internal_customer_coverage(pg_session, tenant_id=TEST_TENANT_A, customer_id=cust.id)

    proof = build_safe_internal_customer_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=cust.id,
    )
    assert proof is not None
    assert proof.policy_eligibility_ready is True


def test_internal_customer_without_reconcile_policy_eligibility_false(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A)
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-INT-NO-COV",
        customer_id=cust.id,
    )
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")

    proof = build_safe_internal_customer_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=cust.id,
    )
    assert proof is not None
    assert proof.watermark_present is False
    assert proof.policy_eligibility_ready is False


def test_external_proof_tenant_isolation_wrong_tenant_returns_none(pg_session) -> None:
    profile = _seed_generic_external_linked_scope(pg_session)
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    reconcile_external_profile_coverage(pg_session, profile=profile)

    assert (
        build_safe_external_profile_proof(
            pg_session,
            tenant_id=TEST_TENANT_B,
            external_customer_profile_id=profile.id,
        )
        is None
    )


def test_proof_policy_eligibility_field_is_boolean_only() -> None:
    from services.order_customer_identity_read_contract import (
        SafeExternalProfileSourceHistoryProof,
        SafeInternalCustomerSourceHistoryProof,
    )

    ext_field = SafeExternalProfileSourceHistoryProof.__dataclass_fields__["policy_eligibility_ready"]
    int_field = SafeInternalCustomerSourceHistoryProof.__dataclass_fields__["policy_eligibility_ready"]
    assert ext_field.type in (bool, "bool")
    assert int_field.type in (bool, "bool")
