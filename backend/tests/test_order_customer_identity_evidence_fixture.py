"""Tests for staging-only A1 generic-commerce evidence fixture harness."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.order_customer_identity_contract import (
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_VERIFIED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
)
from services.order_customer_identity_evidence_fixture import (
    execute_order_customer_identity_evidence_fixture_cleanup,
    execute_order_customer_identity_evidence_fixture_seed,
)
from services.order_customer_identity_evidence_fixture_contract import (
    CONFIRMATION_ENV_CLEANUP,
    CONFIRMATION_ENV_WRITE,
    CONFIRMATION_TOKEN_CLEANUP,
    CONFIRMATION_TOKEN_WRITE,
    FIXTURE_EXTERNAL_CUSTOMER_REF,
    FIXTURE_EXTERNAL_ID_PREFIX,
    FIXTURE_MARKER_FIELD,
    FIXTURE_NAMESPACE,
    FIXTURE_SCHEMA_VERSION,
)
from services.order_customer_identity_logging import log_evidence_fixture_failure
from services.order_customer_identity_reconciliation_write import (
    validate_capability_and_revision_gates,
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

_GENERIC_TENANT_NAME = "متجر تجريبي عام"
_NON_FIXTURE_ORDER_EXT = "PROD-ORDER-KEEP-01"

_PII_PATTERNS = (
    re.compile(r"@"),
    re.compile(r"\b\d{10,}\b"),
    re.compile(r'"external_customer_ref"\s*:'),
    re.compile(r'"profile_id"\s*:'),
    re.compile(r'"customer_id"\s*:\s*\d'),
    re.compile(r'"order_id"\s*:\s*\d'),
    re.compile(r"postgresql://"),
    re.compile(r"Traceback"),
    re.compile(r'"phone"\s*:'),
    re.compile(r'"email"\s*:'),
    re.compile(r"أحمد"),
    re.compile(r"نورة"),
    re.compile(r"حذاء"),
    re.compile(r"عطر"),
)

_REPO = Path(__file__).resolve().parents[2]
_CLI = "backend/scripts/seed_a1_generic_commerce_evidence_fixture.py"


def _set_alembic_revision(pg_session, revision: str) -> None:
    pg_session.execute(
        text("UPDATE alembic_version SET version_num = :revision"),
        {"revision": revision},
    )
    pg_session.flush()


def _staging_env(**overrides: str) -> dict[str, str]:
    base = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        CONFIRMATION_ENV_WRITE: CONFIRMATION_TOKEN_WRITE,
        CONFIRMATION_ENV_CLEANUP: CONFIRMATION_TOKEN_CLEANUP,
        "DATABASE_URL": os.environ.get("DATABASE_URL", ""),
    }
    base.update(overrides)
    return base


def _fixture_dict_without_timestamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.pop("fixture_generated_at_utc", None)
    return out


def _assert_no_pii_in_fixture(payload: Dict[str, Any], *, known_safe: tuple[str, ...] = ()) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for token in known_safe:
        blob = blob.replace(token, "")
    for pattern in _PII_PATTERNS:
        assert not pattern.search(blob), f"PII pattern {pattern.pattern!r} matched fixture JSON"


def _seed_gates(pg_session, *, tenant_id: int = TEST_TENANT_A) -> None:
    seed_tenant(
        pg_session,
        tenant_id=tenant_id,
        name=f"{_GENERIC_TENANT_NAME} ({tenant_id})",
    )
    _set_alembic_revision(pg_session, "0087")
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)


def _count_fixture_orders(pg_session, *, tenant_id: int) -> int:
    prefix = f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-%"
    return int(
        pg_session.execute(
            text(
                """
                SELECT count(*)::int
                FROM orders
                WHERE tenant_id = :tenant_id
                  AND external_id LIKE :prefix
                  AND metadata ->> :marker_key = :marker_value
                """
            ),
            {
                "tenant_id": int(tenant_id),
                "prefix": prefix,
                "marker_key": FIXTURE_MARKER_FIELD,
                "marker_value": FIXTURE_NAMESPACE,
            },
        ).scalar_one()
    )


def test_dry_run_seed_reports_would_create_without_mutations(pg_session) -> None:
    _seed_gates(pg_session)
    before = _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A)

    result = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.read_only is True
    assert result.outcome == "success"
    assert result.committed is False
    assert sum(result.would_create.values()) > 0
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == before
    _assert_no_pii_in_fixture(result.to_dict(), known_safe=(str(TEST_TENANT_A),))


def test_write_creates_authoritative_internal_and_external_evidence(pg_session) -> None:
    _seed_gates(pg_session)

    result = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    assert result.outcome == "success", (
        result.gate_stage,
        result.gate_error_class,
        result.access_status,
    )
    assert result.committed is True
    assert result.created["internal_orders"] == 1
    assert result.created["external_orders"] == 1
    assert result.authoritative_internal_orders == 1
    assert result.authoritative_external_orders == 1

    row = pg_session.execute(
        text(
            """
            SELECT order_source_kind, customer_link_evidence_class,
                   external_identity_evidence_class, external_customer_ref
            FROM orders
            WHERE tenant_id = :tenant_id
              AND metadata ->> :marker_key = :marker_value
            """
        ),
        {
            "tenant_id": TEST_TENANT_A,
            "marker_key": FIXTURE_MARKER_FIELD,
            "marker_value": FIXTURE_NAMESPACE,
        },
    ).mappings().all()
    kinds = {r["order_source_kind"] for r in row}
    assert ORDER_SOURCE_NAHL_INTERNAL in kinds
    assert ORDER_SOURCE_EXTERNAL_PROVIDER in kinds
    assert any(r["customer_link_evidence_class"] == EVIDENCE_AUTHORITATIVE for r in row)
    assert any(r["external_identity_evidence_class"] == EVIDENCE_AUTHORITATIVE for r in row)
    assert any(r["external_customer_ref"] == FIXTURE_EXTERNAL_CUSTOMER_REF for r in row)


def test_seed_is_idempotent_on_rerun(pg_session) -> None:
    _seed_gates(pg_session)

    first = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    second = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    assert first.committed is True
    assert second.committed is False
    assert second.outcome == "success", (
        second.gate_stage,
        second.gate_error_class,
        second.existing_shape,
    )
    assert sum(second.skipped_existing.values()) >= 2
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == 2


def test_tenant_isolation_does_not_touch_other_tenant(pg_session) -> None:
    _seed_gates(pg_session, tenant_id=TEST_TENANT_A)
    _seed_gates(pg_session, tenant_id=TEST_TENANT_B)

    execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == 2
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_B) == 0


def test_fail_closed_when_revision_not_0087(pg_session) -> None:
    _seed_gates(pg_session)
    _set_alembic_revision(pg_session, "0089")

    failure = validate_capability_and_revision_gates(pg_session)
    assert failure is not None
    assert failure.stage == "revision_not_exactly_0087"

    result = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    assert result.access_status == "gate_rejected"
    assert result.gate_stage == "revision_not_exactly_0087"


def test_fail_closed_when_capability_validated(pg_session) -> None:
    _seed_gates(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")

    result = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    assert result.access_status == "gate_rejected"
    assert result.gate_stage == "capability_state_validated"


def test_cleanup_deletes_only_fixture_owned_rows(pg_session) -> None:
    _seed_gates(pg_session)
    execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    intg = seed_integration(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_store_id="PROD-STORE-KEEP",
    )
    cust = seed_customer(pg_session, tenant_id=TEST_TENANT_A, name="عميل إنتاج")
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id=_NON_FIXTURE_ORDER_EXT,
        customer_id=cust.id,
    )
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="PROD-CUST-KEEP",
    )
    seed_external_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id="PROD-EXT-KEEP",
        integration_connection_id=intg.id,
        external_customer_ref="PROD-CUST-KEEP",
        external_customer_profile_id=profile.id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
    )
    pg_session.flush()

    cleanup = execute_order_customer_identity_evidence_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert cleanup.outcome == "success"
    assert cleanup.cleanup_deleted["orders"] == 2
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == 0

    prod_count = pg_session.execute(
        text(
            """
            SELECT count(*)::int FROM orders
            WHERE tenant_id = :tenant_id AND external_id = :external_id
            """
        ),
        {"tenant_id": TEST_TENANT_A, "external_id": _NON_FIXTURE_ORDER_EXT},
    ).scalar_one()
    assert int(prod_count) == 1


def test_cleanup_dry_run_does_not_delete(pg_session) -> None:
    _seed_gates(pg_session)
    execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    preview = execute_order_customer_identity_evidence_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    assert preview.cleanup_selected["orders"] == 2
    assert preview.committed is False
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == 2


def test_cli_write_requires_confirmation_token(pg_session, postgres_engine) -> None:
    _seed_gates(pg_session)
    pg_session.commit()
    db_url = str(postgres_engine.url.render_as_string(hide_password=False))
    env = _staging_env(DATABASE_URL=db_url)
    env.pop(CONFIRMATION_ENV_WRITE, None)

    proc = subprocess.run(
        [sys.executable, _CLI, "--tenant-id", str(TEST_TENANT_A), "--write"],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["access_status"] == "gate_rejected"
    assert payload["gate_stage"] == "dangerous_action_not_confirmed"


def test_cli_cleanup_requires_separate_confirmation_token(pg_session, postgres_engine) -> None:
    _seed_gates(pg_session)
    pg_session.commit()
    db_url = str(postgres_engine.url.render_as_string(hide_password=False))
    env = _staging_env(DATABASE_URL=db_url)
    env.pop(CONFIRMATION_ENV_CLEANUP, None)

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "--tenant-id",
            str(TEST_TENANT_A),
            "--cleanup",
            "--write",
        ],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["access_status"] == "gate_rejected"


def test_fixture_json_schema_version_and_privacy(pg_session) -> None:
    _seed_gates(pg_session)
    result = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    payload = result.to_dict()
    assert payload["fixture_schema_version"] == FIXTURE_SCHEMA_VERSION
    assert payload["fixture_namespace"] == FIXTURE_NAMESPACE
    _assert_no_pii_in_fixture(payload, known_safe=(str(TEST_TENANT_A),))


def test_fixture_logging_emits_no_pii(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="nahla.order_customer_identity")
    log_evidence_fixture_failure(exception_class="IntegrityError")
    assert caplog.records
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "IntegrityError" in blob
    assert "tenant" not in blob.lower()
    assert "@" not in blob


def test_deterministic_dry_run_payload_excludes_timestamp(pg_session) -> None:
    _seed_gates(pg_session)
    first = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    second = execute_order_customer_identity_evidence_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    assert _fixture_dict_without_timestamp(first.to_dict()) == _fixture_dict_without_timestamp(
        second.to_dict()
    )
