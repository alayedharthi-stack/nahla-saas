"""A1-v3.7 PostgreSQL integration tests (Q/R/S matrix)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

_BACKEND = Path(__file__).resolve().parents[1]

from services.order_customer_identity_contract import (
    EXTERNAL_PROVIDER_SALLA_V1,
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_MANUAL,
    ORDER_SOURCE_OTHER,
    ORDER_SOURCE_WHATSAPP,
    POLICY_ELIGIBILITY_READY,
)
from services.order_customer_identity_read_contract import (
    SafeExternalProfileSourceHistoryProof,
    SafeInternalCustomerSourceHistoryProof,
    build_safe_external_profile_proof,
    build_safe_internal_customer_proof,
)
from services.order_customer_identity_service import (
    apply_external_order_identity_from_salla,
    reconcile_external_profile_coverage,
)
from services.salla_integration_resolver import (
    ResolvedSallaIntegration,
    resolve_salla_integration_connection,
)
from tests.order_customer_identity_postgres_fixtures import (
    TEST_TENANT_A,
    TEST_TENANT_B,
    pg_session,
    postgres_engine,
    seed_customer,
    seed_external_order,
    seed_external_profile,
    seed_integration,
    seed_internal_order,
    seed_tenant,
    seed_untrusted_order,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")


# ── Blocker 3 / Q ─────────────────────────────────────────────────────────────


def test_q1_composite_fk_rejects_wrong_connection_profile(pg_session) -> None:
    """Q1: order connection A + profile from connection B → FK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg_a = seed_integration(
        pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q1_A",
    )
    intg_b = seed_integration(
        pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q1_B",
    )
    profile_b = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg_b.id,
        external_customer_ref="C1",
    )
    with pytest.raises(IntegrityError):
        seed_external_order(
            pg_session,
            tenant_id=TEST_TENANT_A,
            external_id="ORD-Q1",
            integration_connection_id=intg_a.id,
            external_customer_ref="C1",
            external_customer_profile_id=profile_b.id,
            external_identity_link_state=LINK_STATE_VERIFIED,
            external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
        )


def test_q2_external_profile_verified_leaves_customer_link_unlinked(pg_session) -> None:
    """Q2: external authoritative does not set customer_link authoritative."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q2")
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="C-Q2",
    )
    order = seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-Q2",
        integration_connection_id=intg.id,
        external_customer_ref="C-Q2",
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
        customer_link_state=LINK_STATE_UNLINKED,
    )
    pg_session.flush()
    assert order.customer_id is None
    assert order.customer_link_state == LINK_STATE_UNLINKED
    assert order.customer_link_evidence_class is None


def test_q3_external_proof_has_no_customer_claim(pg_session) -> None:
    """Q3: SafeExternalProfileSourceHistoryProof — no canonical customer fields."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q3")
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="C-Q3",
    )
    proof = build_safe_external_profile_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_customer_profile_id=profile.id,
    )
    assert proof is not None
    assert proof.subject_kind == "external_customer_profile"
    assert proof.policy_eligibility_ready is POLICY_ELIGIBILITY_READY
    fields = set(SafeExternalProfileSourceHistoryProof.__dataclass_fields__)
    assert "customer_id" not in fields
    assert "customer_link_state" not in fields


def test_q4_internal_proof_has_no_external_profile_fields(pg_session) -> None:
    """Q4: SafeInternalCustomerSourceHistoryProof — no external profile claim."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A)
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-Q4",
        customer_id=cust.id,
    )
    proof = build_safe_internal_customer_proof(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=cust.id,
    )
    assert proof is not None
    assert proof.subject_kind == "nahla_internal_customer"
    fields = set(SafeInternalCustomerSourceHistoryProof.__dataclass_fields__)
    assert "external_customer_profile_id" not in fields
    assert "integration_connection_present" not in fields


def test_q5_no_cross_subject_aggregate_in_a1_modules() -> None:
    """Q5: no registered_source_scopes_completeness in A1 code."""
    banned = "registered_source_scopes_completeness"
    a1_files = [
        _BACKEND / "services" / "order_customer_identity_contract.py",
        _BACKEND / "services" / "order_customer_identity_service.py",
        _BACKEND / "services" / "order_customer_identity_read_contract.py",
        _BACKEND / "services" / "order_customer_identity_logging.py",
        _BACKEND / "services" / "external_customer_profile_service.py",
        _BACKEND / "services" / "salla_integration_resolver.py",
    ]
    for path in a1_files:
        text = path.read_text(encoding="utf-8")
        assert banned not in text, f"{path.name} references banned aggregate"


def test_q6_same_ref_different_connections_independent_profiles(pg_session) -> None:
    """Q6 / N1a: ref C1 on connection A and B → independent profiles."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg_a = seed_integration(
        pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_ALPHA",
        config={"api_key": "k-a", "app_type": "easy", "api_sync_enabled": False},
    )
    intg_b = seed_integration(
        pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_BETA",
        config={"api_key": "k-b", "api_sync_enabled": True, "app_type": "custom_oauth_sync"},
    )
    prof_a = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg_a.id, external_customer_ref="C1",
    )
    prof_b = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg_b.id, external_customer_ref="C1",
    )
    assert prof_a.id != prof_b.id


def test_q7_external_order_links_profile_not_customer_id(pg_session) -> None:
    """Q7: external ingest sets profile_id; customer_id stays NULL."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q7")
    resolution = ResolvedSallaIntegration(
        integration_id=intg.id, tenant_id=TEST_TENANT_A, matched_via="test",
    )
    order = seed_external_order(
        pg_session, tenant_id=TEST_TENANT_A, external_id="ORD-Q7",
    )
    outcome = apply_external_order_identity_from_salla(
        pg_session,
        order=order,
        tenant_id=TEST_TENANT_A,
        integration_resolution=resolution,
        order_payload={"customer": {"id": "777"}},
        ingest_source="test",
    )
    pg_session.flush()
    assert outcome == "linked"
    assert order.external_customer_profile_id is not None
    assert order.customer_id is None
    assert order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE


def test_q8_nahla_internal_sets_customer_id_only(pg_session) -> None:
    """Q8: internal authoritative — customer_id set, external NULL."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A)
    order = seed_internal_order(
        pg_session, tenant_id=TEST_TENANT_A, external_id="ORD-Q8", customer_id=cust.id,
    )
    assert order.customer_id == cust.id
    assert order.external_customer_profile_id is None
    assert order.external_identity_evidence_class is None


def test_q9_salla_customer_id_does_not_affect_profile_lookup(pg_session) -> None:
    """Q9: Customer.salla_customer_id present — profile lookup still by quartet."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_customer(pg_session, tenant_id=TEST_TENANT_A, salla_customer_id="LEGACY-C9")
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_Q9")
    resolution = ResolvedSallaIntegration(
        integration_id=intg.id, tenant_id=TEST_TENANT_A, matched_via="test",
    )
    order = seed_external_order(
        pg_session, tenant_id=TEST_TENANT_A, external_id="ORD-Q9",
    )
    apply_external_order_identity_from_salla(
        pg_session,
        order=order,
        tenant_id=TEST_TENANT_A,
        integration_resolution=resolution,
        order_payload={"customer": {"id": "LEGACY-C9"}},
        ingest_source="test",
    )
    pg_session.flush()
    assert order.customer_id is None
    assert order.external_customer_profile_id is not None


def test_q10_resolver_integration_first_derives_tenant(pg_session) -> None:
    """Q10 / T4: resolver returns integration → tenant from row."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_store_id="STORE_RES",
        config={"api_key": "k", "app_type": "easy", "api_sync_enabled": False},
    )
    result = resolve_salla_integration_connection(
        pg_session,
        webhook_provider_channel="salla",
        canonical_store_id="STORE_RES",
    )
    assert isinstance(result, ResolvedSallaIntegration)
    assert result.integration_id == intg.id
    assert result.tenant_id == TEST_TENANT_A


def test_q11_dual_channel_resolver_picks_correct_integration(pg_session) -> None:
    """Q11 / N1b: easy vs oauth channel → different integration rows."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    easy = seed_integration(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_store_id="STORE_DUAL",
        config={"api_key": "easy", "app_type": "easy", "api_sync_enabled": False},
    )
    oauth = seed_integration(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_store_id=None,
        config={
            "api_key": "oauth",
            "store_id": "STORE_DUAL",
            "api_sync_enabled": True,
            "app_type": "custom_oauth_sync",
        },
    )
    r_easy = resolve_salla_integration_connection(
        pg_session, webhook_provider_channel="salla", canonical_store_id="STORE_DUAL",
    )
    r_oauth = resolve_salla_integration_connection(
        pg_session, webhook_provider_channel="salla_oauth", canonical_store_id="STORE_DUAL",
    )
    assert isinstance(r_easy, ResolvedSallaIntegration)
    assert isinstance(r_oauth, ResolvedSallaIntegration)
    assert r_easy.integration_id == easy.id
    assert r_oauth.integration_id == oauth.id


def test_q12_guest_order_unlinked_no_profile(pg_session) -> None:
    """Q12: guest Salla — external_provider unlinked, no profile."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_GUEST")
    resolution = ResolvedSallaIntegration(
        integration_id=intg.id, tenant_id=TEST_TENANT_A, matched_via="test",
    )
    order = seed_external_order(
        pg_session, tenant_id=TEST_TENANT_A, external_id="ORD-GUEST",
    )
    apply_external_order_identity_from_salla(
        pg_session,
        order=order,
        tenant_id=TEST_TENANT_A,
        integration_resolution=resolution,
        order_payload={"customer": {}},
        ingest_source="test",
    )
    pg_session.flush()
    assert order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER
    assert order.external_customer_profile_id is None
    assert order.customer_id is None


# ── Blocker 1 / R ─────────────────────────────────────────────────────────────


def test_r1_unmapped_tuple_order_degrades_coverage(pg_session) -> None:
    """R1: new order same tuple with profile_id NULL → incomplete coverage."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_R1")
    profile = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id, external_customer_ref="CR1",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-R1-LINKED",
        integration_connection_id=intg.id,
        external_customer_ref="CR1",
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-R1-UNMAPPED",
        integration_connection_id=intg.id,
        external_customer_ref="CR1",
        external_customer_profile_id=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )
    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.unmapped >= 1
    assert result.completeness == "incomplete"
    assert result.forward_health == "degraded"


def test_r2_mislinked_profile_id_counts_incomplete(pg_session) -> None:
    """R2: tuple matches profile A but order points at profile B → mislinked."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_R2")
    prof_a = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id, external_customer_ref="CR2",
    )
    prof_b = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id, external_customer_ref="CR2-B",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-R2-MIS",
        integration_connection_id=intg.id,
        external_customer_ref="CR2",
        external_customer_profile_id=prof_b.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    result = reconcile_external_profile_coverage(pg_session, profile=prof_a)
    assert result.mislinked >= 1
    assert result.completeness == "incomplete"


def test_r3_fk_blocks_wrong_connection_on_commit(pg_session) -> None:
    """R3: wrong connection on FK triple rejected at DB (same as Q1)."""
    test_q1_composite_fk_rejects_wrong_connection_profile(pg_session)


def test_r4_guest_orders_outside_profile_scope(pg_session) -> None:
    """R4: guest/no-ref orders do not affect profile scope counts."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_R4")
    profile = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id, external_customer_ref="CR4",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORD-R4-GUEST",
        integration_connection_id=intg.id,
        external_customer_ref=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )
    result = reconcile_external_profile_coverage(pg_session, profile=profile)
    assert result.linked == 0
    assert result.unmapped == 0
    assert result.mislinked == 0


# ── Blocker 2 / S ─────────────────────────────────────────────────────────────


def test_s1_manual_with_customer_id_rejected(pg_session) -> None:
    """S1: manual + customer_id → CHECK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A)
    order = seed_untrusted_order(
        pg_session, tenant_id=TEST_TENANT_A, kind=ORDER_SOURCE_MANUAL, external_id="ORD-S1",
    )
    order.customer_id = cust.id
    pg_session.add(order)
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_s2_manual_with_profile_id_rejected(pg_session) -> None:
    """S2: manual + external_customer_profile_id → CHECK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE_S2")
    profile = seed_external_profile(
        pg_session, tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id, external_customer_ref="CS2",
    )
    order = seed_untrusted_order(
        pg_session, tenant_id=TEST_TENANT_A, kind=ORDER_SOURCE_MANUAL, external_id="ORD-S2",
    )
    order.external_customer_profile_id = profile.id
    pg_session.add(order)
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_s3_whatsapp_with_customer_id_rejected(pg_session) -> None:
    """S3: whatsapp + customer_id → CHECK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A)
    order = seed_untrusted_order(
        pg_session, tenant_id=TEST_TENANT_A, kind=ORDER_SOURCE_WHATSAPP, external_id="ORD-S3",
    )
    order.customer_id = cust.id
    pg_session.add(order)
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_s4_other_with_evidence_rejected(pg_session) -> None:
    """S4: other + authoritative evidence → CHECK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    order = seed_untrusted_order(
        pg_session, tenant_id=TEST_TENANT_A, kind=ORDER_SOURCE_OTHER, external_id="ORD-S4",
    )
    order.external_identity_evidence_class = EVIDENCE_AUTHORITATIVE
    pg_session.add(order)
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_s5_whatsapp_all_null_passes_check(pg_session) -> None:
    """S5: whatsapp with all identifiers NULL → CHECK pass."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    order = seed_untrusted_order(
        pg_session, tenant_id=TEST_TENANT_A, kind=ORDER_SOURCE_WHATSAPP, external_id="ORD-S5",
    )
    pg_session.flush()
    assert order.customer_id is None
    assert order.external_customer_profile_id is None


def test_cross_tenant_customer_fk_rejected(pg_session) -> None:
    """Cross-tenant Order.customer_id composite FK reject."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B)
    cust_b = seed_customer(pg_session, tenant_id=TEST_TENANT_B)
    with pytest.raises(IntegrityError):
        seed_internal_order(
            pg_session, tenant_id=TEST_TENANT_A, external_id="ORD-XT", customer_id=cust_b.id,
        )
