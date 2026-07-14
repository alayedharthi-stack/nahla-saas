"""Capability-state rollout gate for A1 order-customer identity."""
from __future__ import annotations

import pytest
from pathlib import Path

from services.order_customer_identity_capability import (
    cap_coverage_status_for_capability,
    order_customer_identity_reconciliation_ready,
    read_order_customer_identity_capability_state,
)
from services.order_customer_identity_contract import (
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
)
from services.order_customer_identity_read_contract import build_safe_external_profile_proof
from services.order_customer_identity_service import reconcile_external_profile_coverage
from tests.order_customer_identity_postgres_fixtures import (
    TEST_TENANT_A,
    clear_capability_state,
    pg_session,
    postgres_engine,
    seed_capability_state,
    seed_external_order,
    seed_external_profile,
    seed_integration,
    seed_tenant,
)


def _seed_linked_tuple_scope(pg_session) -> tuple:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_CAP")
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="CAP1",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-CAP-LINKED",
        integration_connection_id=intg.id,
        external_customer_ref="CAP1",
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    return profile


def test_capability_expand_reconciliation_stays_degraded_incomplete(pg_session) -> None:
    profile = _seed_linked_tuple_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.linked == 1
    assert result.unmapped == 0
    assert result.completeness == SOURCE_HISTORY_INCOMPLETE
    assert result.forward_health == SYNC_HEALTH_DEGRADED
    assert order_customer_identity_reconciliation_ready(pg_session) is False


def test_capability_missing_fail_closed(pg_session) -> None:
    profile = _seed_linked_tuple_scope(pg_session)
    clear_capability_state(pg_session)

    assert read_order_customer_identity_capability_state(pg_session) is None
    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.completeness == SOURCE_HISTORY_INCOMPLETE
    assert result.forward_health == SYNC_HEALTH_DEGRADED

    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.authoritative_source_history_completeness == SOURCE_HISTORY_INCOMPLETE
    assert proof.forward_sync_health == SYNC_HEALTH_DEGRADED


def test_read_capability_rejects_unknown_state_value(pg_session, monkeypatch) -> None:
    class _Result:
        def first(self):
            return ("obsolete_future_state",)

    monkeypatch.setattr(pg_session, "execute", lambda *args, **kwargs: _Result())
    assert read_order_customer_identity_capability_state(pg_session) is None


def test_capability_unknown_state_fail_closed(pg_session, monkeypatch) -> None:
    profile = _seed_linked_tuple_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    monkeypatch.setattr(
        "services.order_customer_identity_capability.read_order_customer_identity_capability_state",
        lambda _db: "obsolete_future_state",
    )

    assert order_customer_identity_reconciliation_ready(pg_session) is False
    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.completeness == SOURCE_HISTORY_INCOMPLETE
    assert result.forward_health == SYNC_HEALTH_DEGRADED


def test_capability_validated_allows_healthy_complete_when_scope_linked(pg_session) -> None:
    profile = _seed_linked_tuple_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")

    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.linked == 1
    assert result.unmapped == 0
    assert result.completeness == SOURCE_HISTORY_COMPLETE
    assert result.forward_health == SYNC_HEALTH_HEALTHY


def test_capability_validated_still_incomplete_when_unmapped(pg_session) -> None:
    profile = _seed_linked_tuple_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-CAP-UNMAPPED",
        integration_connection_id=profile.integration_connection_id,
        external_customer_ref="CAP1",
        external_customer_profile_id=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )

    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.unmapped >= 1
    assert result.completeness == SOURCE_HISTORY_INCOMPLETE
    assert result.forward_health == SYNC_HEALTH_DEGRADED


def test_runtime_service_does_not_reference_alembic_version() -> None:
    repo = Path(__file__).resolve().parents[2]
    runtime_files = (
        repo / "backend/services/order_customer_identity_service.py",
        repo / "backend/services/order_customer_identity_capability.py",
        repo / "backend/services/order_customer_identity_read_contract.py",
    )
    for path in runtime_files:
        body = path.read_text(encoding="utf-8")
        assert "alembic_version" not in body, f"{path.name} must not read alembic_version"


def test_cap_coverage_status_unit_missing_db_fail_closed() -> None:
    class _Db:
        pass

    completeness, health = cap_coverage_status_for_capability(
        _Db(),  # type: ignore[arg-type]
        completeness=SOURCE_HISTORY_COMPLETE,
        forward_health=SYNC_HEALTH_HEALTHY,
    )
    assert completeness == SOURCE_HISTORY_INCOMPLETE
    assert health == SYNC_HEALTH_DEGRADED
