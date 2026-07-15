"""Tests for tenant-scoped A1 reconciliation operator report (G4)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from sqlalchemy import text

from services import order_customer_identity_reconciliation_report as reconciliation_report
from services.order_customer_identity_contract import (
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    POLICY_ELIGIBILITY_READY,
    SOURCE_HISTORY_INCOMPLETE,
)
from services.order_customer_identity_reconciliation_report import (
    MAX_ORDERS_PER_SUBJECT,
    REPORT_SCHEMA_VERSION,
    build_order_customer_identity_reconciliation_report,
)
from services.order_customer_identity_service import reconcile_external_profile_coverage
from services.order_customer_identity_reconciliation_classification import TupleLinkageCounts
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

_GENERIC_TENANT_NAME = "متجر تجريبي عام"
_GENERIC_CUSTOMER_NAME = "نورة عبدالله"
_GENERIC_ORDER_REF = "RRRD1234"
_GENERIC_PROFILE_REF = "GEN-CUST-01"

_PII_PATTERNS = (
    re.compile(r"@"),
    re.compile(r"\b\d{10,}\b"),
    re.compile(r'"external_customer_ref"\s*:'),
    re.compile(r'"customer_id"\s*:\s*\d'),
    re.compile(r'"order_id"\s*:\s*\d'),
    re.compile(r"postgresql://"),
    re.compile(r"Traceback"),
    re.compile(r'"phone"\s*:'),
    re.compile(r'"email"\s*:'),
)


def _report_dict_without_timestamp(report_dict: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(report_dict)
    out.pop("report_generated_at_utc", None)
    return out


def _assert_no_pii_in_report(payload: Dict[str, Any], *, known_safe: tuple[str, ...] = ()) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for token in known_safe:
        blob = blob.replace(token, "")
    for pattern in _PII_PATTERNS:
        assert not pattern.search(blob), f"PII pattern {pattern.pattern!r} matched report JSON"


def _seed_external_coverage_row(
    pg_session,
    *,
    tenant_id: int,
    profile,
    watermark: bool = True,
) -> None:
    now = datetime.now(timezone.utc)
    pg_session.execute(
        text(
            """
            INSERT INTO external_customer_profile_order_history_coverage (
                id, tenant_id, external_customer_profile_id, identity_namespace,
                integration_connection_id, external_customer_ref,
                watermark_at, forward_sync_health,
                authoritative_source_history_completeness,
                linked_orders_in_scope_count, unmapped_orders_in_scope_count,
                mislinked_orders_in_scope_count, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), :tenant_id, :profile_id, :identity_namespace,
                :integration_connection_id, :external_customer_ref,
                :watermark_at, 'degraded', 'incomplete',
                0, 0, 0, :now, :now
            )
            ON CONFLICT (external_customer_profile_id) DO UPDATE
            SET watermark_at = EXCLUDED.watermark_at,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "tenant_id": int(tenant_id),
            "profile_id": profile.id,
            "identity_namespace": profile.identity_namespace,
            "integration_connection_id": int(profile.integration_connection_id),
            "external_customer_ref": profile.external_customer_ref,
            "watermark_at": now if watermark else None,
            "now": now,
        },
    )
    pg_session.flush()


def _seed_internal_coverage_row(
    pg_session,
    *,
    tenant_id: int,
    customer_id: int,
    watermark: bool = True,
) -> None:
    now = datetime.now(timezone.utc)
    pg_session.execute(
        text(
            """
            INSERT INTO nahla_internal_customer_order_history_coverage (
                id, tenant_id, customer_id, identity_namespace,
                watermark_at, forward_sync_health,
                authoritative_source_history_completeness,
                linked_orders_in_scope_count, unmapped_orders_in_scope_count,
                mislinked_orders_in_scope_count, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), :tenant_id, :customer_id, 'nahla_internal_order_v1',
                :watermark_at, 'stale', 'incomplete',
                0, 0, 0, :now, :now
            )
            ON CONFLICT (tenant_id, customer_id, identity_namespace) DO UPDATE
            SET watermark_at = EXCLUDED.watermark_at,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "tenant_id": int(tenant_id),
            "customer_id": int(customer_id),
            "watermark_at": now if watermark else None,
            "now": now,
        },
    )
    pg_session.flush()


def _seed_generic_commerce_linked_scope(pg_session, *, tenant_id: int = TEST_TENANT_A) -> None:
    seed_tenant(pg_session, tenant_id=tenant_id, name=_GENERIC_TENANT_NAME)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    intg = seed_integration(
        pg_session,
        tenant_id=tenant_id,
        external_store_id="STORE-GENERIC",
    )
    profile = seed_external_profile(
        pg_session,
        tenant_id=tenant_id,
        integration_connection_id=intg.id,
        external_customer_ref=_GENERIC_PROFILE_REF,
    )
    seed_external_order(
        pg_session,
        tenant_id=tenant_id,
        external_id=_GENERIC_ORDER_REF,
        integration_connection_id=intg.id,
        external_customer_ref=_GENERIC_PROFILE_REF,
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    _seed_external_coverage_row(pg_session, tenant_id=tenant_id, profile=profile)

    cust = seed_customer(
        pg_session,
        tenant_id=tenant_id,
        name=_GENERIC_CUSTOMER_NAME,
    )
    seed_internal_order(
        pg_session,
        tenant_id=tenant_id,
        external_id="INT-GEN-01",
        customer_id=cust.id,
    )
    _seed_internal_coverage_row(pg_session, tenant_id=tenant_id, customer_id=cust.id)


def test_report_schema_version_and_policy_eligibility(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    payload = report.to_dict()

    assert payload["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert payload["dry_run"] is True
    assert payload["read_only"] is True
    assert payload["policy_eligibility_ready"] is POLICY_ELIGIBILITY_READY
    assert payload["ready_for_validate"] is False
    assert "coverage_scope_claims" in payload
    _assert_no_pii_in_report(payload)


def test_tenant_isolation_excludes_other_tenant_subjects(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="Tenant A")
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B, name="Tenant B")
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    intg_a = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="A-STORE")
    intg_b = seed_integration(pg_session, tenant_id=TEST_TENANT_B, external_store_id="B-STORE")
    seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg_a.id,
        external_customer_ref="ONLY-A",
    )
    seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_B,
        integration_connection_id=intg_b.id,
        external_customer_ref="ONLY-B",
    )

    report_a = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    report_b = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_B)

    assert report_a.external_profiles["subjects_total"] == 1
    assert report_b.external_profiles["subjects_total"] == 1
    assert report_a.aggregate["subjects_total"] == 1
    assert report_b.aggregate["subjects_total"] == 1


def test_fail_closed_missing_capability(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    clear_capability_state(pg_session)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    payload = report.to_dict()

    assert report.capability_state_readable is False
    assert report.ready_for_validate is False
    assert "capability_state_unreadable" in report.readiness_blockers
    assert payload["capability"]["state"] is None
    assert report.external_profiles["subjects_runtime_complete"] == 0


def test_fail_closed_unmapped_orders(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    intg = (
        pg_session.execute(
            text(
                "SELECT integration_connection_id FROM external_customer_profiles "
                "WHERE tenant_id = :tid LIMIT 1"
            ),
            {"tid": TEST_TENANT_A},
        ).scalar()
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="UNMAPPED-GEN",
        integration_connection_id=intg,
        external_customer_ref=_GENERIC_PROFILE_REF,
        external_customer_profile_id=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.aggregate["unmapped_orders_in_scope_total"] >= 1
    assert report.ready_for_validate is False
    assert "unmapped_orders_present" in report.readiness_blockers
    assert "tuple_linkage_incomplete" in report.readiness_blockers


def test_external_orphan_tuple_is_aggregate_gate(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    intg = pg_session.execute(
        text(
            "SELECT integration_connection_id FROM external_customer_profiles "
            "WHERE tenant_id = :tid LIMIT 1"
        ),
        {"tid": TEST_TENANT_A},
    ).scalar()
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="ORPHAN-GENERIC",
        integration_connection_id=intg,
        external_customer_ref="ORPHAN-TUPLE-REF",
        external_customer_profile_id=None,
        external_identity_link_state=LINK_STATE_UNLINKED,
    )

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    payload = report.to_dict()

    assert report.external_profiles["orphan_tuple_orders_total"] == 1
    assert report.ready_for_validate is False
    assert "external_orphan_tuple_orders_present" in report.readiness_blockers
    _assert_no_pii_in_report(payload)


def test_mislinked_orders_fail_closed(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    intg = pg_session.execute(
        text(
            "SELECT integration_connection_id FROM external_customer_profiles "
            "WHERE tenant_id = :tid LIMIT 1"
        ),
        {"tid": TEST_TENANT_A},
    ).scalar()
    other = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg,
        external_customer_ref="OTHER-TUPLE",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="MISLINKED-GENERIC",
        integration_connection_id=intg,
        external_customer_ref=_GENERIC_PROFILE_REF,
        external_customer_profile_id=other.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.aggregate["mislinked_orders_in_scope_total"] == 1
    assert report.ready_for_validate is False
    assert "mislinked_orders_present" in report.readiness_blockers


def test_missing_and_invalid_tenant_fail_closed(pg_session) -> None:
    missing = build_order_customer_identity_reconciliation_report(pg_session, 765_432)
    invalid = build_order_customer_identity_reconciliation_report(pg_session, 0)

    assert missing.access_status == "tenant_missing"
    assert missing.ready_for_validate is False
    assert "tenant_missing" in missing.readiness_blockers
    assert invalid.access_status == "degraded"
    assert invalid.ready_for_validate is False
    assert invalid.readiness_blockers == ["access_degraded"]


def test_access_exception_logs_only_safe_event(pg_session, monkeypatch) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    original_query = pg_session.query
    logged: Dict[str, str] = {}

    def failing_query(*args, **kwargs):
        if args:
            raise RuntimeError("postgresql://secret.example/hidden customer=Jane")
        return original_query(*args, **kwargs)

    monkeypatch.setattr(pg_session, "query", failing_query)
    monkeypatch.setattr(
        "services.order_customer_identity_reconciliation_report.log_reconciliation_report_failure",
        lambda *, exception_class: logged.update(exception_class=exception_class),
    )
    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.access_status == "degraded"
    assert report.ready_for_validate is False
    assert logged == {"exception_class": "RuntimeError"}


def test_report_mode_does_not_mutate_coverage_rows(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    before = pg_session.execute(
        text(
            """
            SELECT updated_at::text
            FROM external_customer_profile_order_history_coverage
            WHERE tenant_id = :tid
            """
        ),
        {"tid": TEST_TENANT_A},
    ).fetchall()

    build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    after = pg_session.execute(
        text(
            """
            SELECT updated_at::text
            FROM external_customer_profile_order_history_coverage
            WHERE tenant_id = :tid
            """
        ),
        {"tid": TEST_TENANT_A},
    ).fetchall()

    assert before == after


def test_write_reconcile_differs_from_report_mode(pg_session) -> None:
    """Document boundary: write reconcile mutates rows; report mode does not."""
    _seed_generic_commerce_linked_scope(pg_session)
    profile = (
        pg_session.execute(
            text("SELECT id FROM external_customer_profiles WHERE tenant_id = :tid LIMIT 1"),
            {"tid": TEST_TENANT_A},
        )
        .first()
    )
    from models import ExternalCustomerProfile  # noqa: PLC0415

    prof = pg_session.get(ExternalCustomerProfile, profile[0])
    before_linked = pg_session.execute(
        text(
            "SELECT linked_orders_in_scope_count FROM "
            "external_customer_profile_order_history_coverage "
            "WHERE external_customer_profile_id = :pid"
        ),
        {"pid": prof.id},
    ).scalar()

    build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)
    mid_linked = pg_session.execute(
        text(
            "SELECT linked_orders_in_scope_count FROM "
            "external_customer_profile_order_history_coverage "
            "WHERE external_customer_profile_id = :pid"
        ),
        {"pid": prof.id},
    ).scalar()
    assert mid_linked == before_linked

    reconcile_external_profile_coverage(pg_session, profile=prof)
    pg_session.flush()
    after_linked = pg_session.execute(
        text(
            "SELECT linked_orders_in_scope_count FROM "
            "external_customer_profile_order_history_coverage "
            "WHERE external_customer_profile_id = :pid"
        ),
        {"pid": prof.id},
    ).scalar()
    assert after_linked == 1
    assert before_linked == 0


def test_idempotent_aggregates_for_identical_state(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    first = _report_dict_without_timestamp(
        build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A).to_dict()
    )
    second = _report_dict_without_timestamp(
        build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A).to_dict()
    )

    assert first == second


def test_ready_for_validate_false_when_watermark_missing(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    pg_session.execute(
        text(
            "UPDATE external_customer_profile_order_history_coverage "
            "SET watermark_at = NULL WHERE tenant_id = :tid"
        ),
        {"tid": TEST_TENANT_A},
    )
    pg_session.flush()

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.ready_for_validate is False
    assert "watermark_missing" in report.readiness_blockers


def test_ready_for_validate_true_when_all_gates_pass(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.capability_state == CAPABILITY_STATE_EXPAND
    assert report.aggregate["unmapped_orders_in_scope_total"] == 0
    assert report.aggregate["mislinked_orders_in_scope_total"] == 0
    assert report.ready_for_validate is True
    assert report.readiness_blockers == []
    assert report.evidence_gates["runtime_reconciliation_consumer_ready"] is False


def test_zero_linked_orders_never_passes_vacuously(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="ZERO")
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="ZERO-TUPLE",
    )
    _seed_external_coverage_row(pg_session, tenant_id=TEST_TENANT_A, profile=profile)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.aggregate["linked_orders_in_scope_total"] == 0
    assert report.ready_for_validate is False
    assert "no_linked_orders" in report.readiness_blockers


def test_capability_validated_blocks_ready_for_validate(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.ready_for_validate is False
    assert "capability_not_in_expand" in report.readiness_blockers


def test_enumeration_truncation_fails_closed(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="TRUNC")
    for idx in range(3):
        seed_external_profile(
            pg_session,
            tenant_id=TEST_TENANT_A,
            integration_connection_id=intg.id,
            external_customer_ref=f"TRUNC-{idx}",
        )

    report = build_order_customer_identity_reconciliation_report(
        pg_session,
        TEST_TENANT_A,
        max_subjects_per_kind=2,
    )

    assert report.external_profiles["enumeration_truncated"] is True
    assert report.ready_for_validate is False
    assert "subject_enumeration_truncated" in report.readiness_blockers
    assert report.access_status == "enumeration_truncated"


def test_external_order_enumeration_limit_fails_closed(pg_session, monkeypatch) -> None:
    """A bounded extra-row read must reach the production readiness gate."""
    _seed_generic_commerce_linked_scope(pg_session)
    counts, truncated = reconciliation_report._counts_and_truncation(
        ["linked"] * (MAX_ORDERS_PER_SUBJECT + 1)
    )
    assert truncated is True
    assert counts.linked == MAX_ORDERS_PER_SUBJECT

    monkeypatch.setattr(
        reconciliation_report,
        "_compute_external_tuple_counts_readonly",
        lambda _db, *, profile: (TupleLinkageCounts(linked=1), True),
    )
    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.external_profiles["order_enumeration_truncated"] is True
    assert report.evidence_gates["no_order_enumeration_truncation"] is False
    assert "subject_order_enumeration_truncated" in report.readiness_blockers
    assert report.ready_for_validate is False
    assert report.access_status == "enumeration_truncated"


def test_internal_subject_enumeration_limit_fails_closed(pg_session) -> None:
    """Internal discovery is bounded and reports truncation rather than hiding subjects."""
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    for index in range(3):
        customer = seed_customer(
            pg_session,
            tenant_id=TEST_TENANT_A,
            name=f"Generic Customer {index}",
        )
        seed_internal_order(
            pg_session,
            tenant_id=TEST_TENANT_A,
            external_id=f"INT-LIMIT-{index}",
            customer_id=customer.id,
        )

    report = build_order_customer_identity_reconciliation_report(
        pg_session,
        TEST_TENANT_A,
        max_subjects_per_kind=2,
    )

    assert report.internal_customers["enumeration_truncated"] is True
    assert report.evidence_gates["no_enumeration_truncation"] is False
    assert "subject_enumeration_truncated" in report.readiness_blockers
    assert report.ready_for_validate is False
    assert report.access_status == "enumeration_truncated"


def test_generic_merchant_scenario_counts(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.tenant_present is True
    assert report.external_profiles["linked_orders_in_scope_total"] == 1
    assert report.internal_customers["linked_orders_in_scope_total"] == 1
    assert report.aggregate["subjects_total"] == 2
    _assert_no_pii_in_report(report.to_dict())


def test_report_module_does_not_import_write_reconcile_helpers() -> None:
    repo = Path(__file__).resolve().parents[1]
    body = (repo / "services" / "order_customer_identity_reconciliation_report.py").read_text(
        encoding="utf-8",
    )
    assert "reconcile_external_profile_coverage" not in body
    assert "reconcile_internal_customer_coverage" not in body
    assert "ensure_external_profile_coverage_row" not in body
    assert "ensure_internal_customer_coverage_row" not in body


def test_report_and_write_paths_share_pure_classification_helper() -> None:
    repo = Path(__file__).resolve().parents[1]
    report_body = (repo / "services" / "order_customer_identity_reconciliation_report.py").read_text(
        encoding="utf-8",
    )
    service_body = (repo / "services" / "order_customer_identity_service.py").read_text(
        encoding="utf-8",
    )
    for symbol in (
        "classify_external_tuple_order",
        "classify_internal_customer_order",
        "count_classifications",
    ):
        assert symbol in report_body
        assert symbol in service_body


def test_runtime_status_capped_incomplete_during_expand(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    report = build_order_customer_identity_reconciliation_report(pg_session, TEST_TENANT_A)

    assert report.external_profiles["subjects_runtime_complete"] == 0
    assert report.external_profiles["subjects_runtime_incomplete"] >= 1
    assert report.evidence_gates["runtime_reconciliation_consumer_ready"] is False


def test_cli_degraded_access_returns_nonzero_and_safe_summary(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [
            sys.executable,
            "backend/scripts/report_order_customer_identity_reconciliation.py",
            "--tenant-id",
            "42",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["access_status"] == "degraded"
    assert payload["ready_for_validate"] is False
    assert "a1_reconciliation" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert "sqlite://" not in result.stderr
