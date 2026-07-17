"""Tests for tenant-scoped A1 reconciliation write operator (post-0087 Expand)."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
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
)
from services.order_customer_identity_logging import log_reconciliation_write_failure
from services.order_customer_identity_reconciliation_write import (
    execute_order_customer_identity_reconciliation_write,
    validate_capability_and_revision_gates,
)
from services.order_customer_identity_reconciliation_write_contract import (
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    WRITE_SCHEMA_VERSION,
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
_GENERIC_CUSTOMER_NAME = "نورة عبدالله"
_GENERIC_ORDER_REF = "RRRD1234"
_GENERIC_PROFILE_REF = "GEN-CUST-01"

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
)

_REPO = Path(__file__).resolve().parents[2]
_CLI = "backend/scripts/reconcile_order_customer_identity_coverage.py"


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
        CONFIRMATION_ENV: CONFIRMATION_TOKEN,
        "DATABASE_URL": os.environ.get("DATABASE_URL", ""),
    }
    base.update(overrides)
    return base


def _write_dict_without_timestamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.pop("write_generated_at_utc", None)
    return out


def _assert_no_pii_in_write(payload: Dict[str, Any], *, known_safe: tuple[str, ...] = ()) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for token in known_safe:
        blob = blob.replace(token, "")
    for pattern in _PII_PATTERNS:
        assert not pattern.search(blob), f"PII pattern {pattern.pattern!r} matched write JSON"


def _seed_generic_commerce_linked_scope(
    pg_session,
    *,
    tenant_id: int = TEST_TENANT_A,
    tenant_name: str | None = None,
    store_suffix: str | None = None,
) -> None:
    suffix = store_suffix or str(tenant_id)
    seed_tenant(
        pg_session,
        tenant_id=tenant_id,
        name=tenant_name or _GENERIC_TENANT_NAME,
    )
    _set_alembic_revision(pg_session, "0087")
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)
    intg = seed_integration(
        pg_session,
        tenant_id=tenant_id,
        external_store_id=f"STORE-GENERIC-{suffix}",
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


def _watermark_count(pg_session, *, tenant_id: int) -> int:
    row = pg_session.execute(
        text(
            """
            SELECT count(*)::int
            FROM external_customer_profile_order_history_coverage
            WHERE tenant_id = :tenant_id AND watermark_at IS NOT NULL
            """
        ),
        {"tenant_id": int(tenant_id)},
    ).scalar_one()
    internal = pg_session.execute(
        text(
            """
            SELECT count(*)::int
            FROM nahla_internal_customer_order_history_coverage
            WHERE tenant_id = :tenant_id AND watermark_at IS NOT NULL
            """
        ),
        {"tenant_id": int(tenant_id)},
    ).scalar_one()
    return int(row) + int(internal)


def test_dry_run_writes_none_and_reports_subjects(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    before = _watermark_count(pg_session, tenant_id=TEST_TENANT_A)

    result = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.read_only is True
    assert result.outcome == "success"
    assert result.committed is False
    assert result.subjects_attempted == 2
    assert result.subjects_succeeded == 2
    assert _watermark_count(pg_session, tenant_id=TEST_TENANT_A) == before
    _assert_no_pii_in_write(result.to_dict(), known_safe=(str(TEST_TENANT_A),))


def test_write_updates_coverage_and_is_idempotent(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    first = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert first.outcome == "success"
    assert first.committed is True
    assert first.coverage_rows_created >= 1
    first_without_ts = _write_dict_without_timestamp(first.to_dict())

    second = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert second.outcome == "success"
    assert second.committed is True
    assert second.coverage_rows_created == 0
    assert second.coverage_rows_updated >= 1
    second_without_ts = _write_dict_without_timestamp(second.to_dict())
    assert first_without_ts["batch"] == second_without_ts["batch"]
    assert first_without_ts["aggregate"] == second_without_ts["aggregate"]


def test_tenant_isolation_does_not_touch_other_tenant(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session, tenant_id=TEST_TENANT_A)
    _seed_generic_commerce_linked_scope(
        pg_session,
        tenant_id=TEST_TENANT_B,
        tenant_name="متجر تجريبي ب",
    )
    before_b = _watermark_count(pg_session, tenant_id=TEST_TENANT_B)

    result = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    assert result.outcome == "success"
    assert _watermark_count(pg_session, tenant_id=TEST_TENANT_B) == before_b


def test_batch_bounding_fails_closed_when_truncated(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE-EXTRA")
    for idx in range(3):
        seed_external_profile(
            pg_session,
            tenant_id=TEST_TENANT_A,
            integration_connection_id=intg.id,
            external_customer_ref=f"GEN-EXTRA-{idx}",
        )

    result = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
        max_subjects_per_kind=2,
    )

    assert result.enumeration_truncated is True
    assert result.access_status == "enumeration_truncated"
    assert result.outcome == "failed"
    assert result.committed is False


def test_deterministic_subject_ordering(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    intg = seed_integration(pg_session, tenant_id=TEST_TENANT_A, external_store_id="STORE-ORD")
    seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="GEN-ORD-2",
    )
    seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=intg.id,
        external_customer_ref="GEN-ORD-3",
    )

    first = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
        max_subjects_per_kind=10,
    )
    second = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
        max_subjects_per_kind=10,
    )

    assert _write_dict_without_timestamp(first.to_dict()) == _write_dict_without_timestamp(
        second.to_dict()
    )


def test_rejects_validated_capability(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088")

    result = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )

    assert result.outcome == "failed"
    assert result.gate_stage == "capability_state_validated"


def test_rejects_missing_capability(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    clear_capability_state(pg_session)

    result = execute_order_customer_identity_reconciliation_write(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )

    assert result.outcome == "failed"
    assert result.gate_stage == "capability_state_missing"


@pytest.mark.parametrize("revision", ("0088", "0089", "0099"))
def test_rejects_non_0087_revisions(pg_session, revision: str) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    _set_alembic_revision(pg_session, revision)

    failure = validate_capability_and_revision_gates(pg_session)

    assert failure is not None
    assert failure.error_class == "revision_rejected"
    assert failure.stage == "revision_not_exactly_0087"


def test_subject_failure_reports_categories_without_hidden_success(pg_session) -> None:
    _seed_generic_commerce_linked_scope(pg_session)

    with patch(
        "services.order_customer_identity_reconciliation_write.reconcile_external_profile_coverage",
        side_effect=RuntimeError("boom"),
    ):
        result = execute_order_customer_identity_reconciliation_write(
            pg_session,
            TEST_TENANT_A,
            dry_run=False,
        )

    assert result.outcome == "partial"
    assert result.subjects_failed >= 1
    assert result.failure_categories["subject_exception"] >= 1
    assert result.committed is True
    assert result.access_status == "degraded"


def test_log_reconciliation_write_failure_no_pii(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="nahla.order_customer_identity"):
        log_reconciliation_write_failure(exception_class="IntegrityError")
    blob = caplog.text.lower()
    assert "customer_id" not in blob
    assert "external_customer_ref" not in blob
    assert "order_id" not in blob


def _database_url(postgres_engine) -> str:
    return str(postgres_engine.url.render_as_string(hide_password=False))


def _load_cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "reconcile_cli",
        _REPO / "backend" / "scripts" / "reconcile_order_customer_identity_coverage.py",
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def _patch_cli_session(monkeypatch, pg_session) -> None:
    import sqlalchemy.orm

    class _SessionProxy:
        def __init__(self, session: Session) -> None:
            self._session = session

        def close(self) -> None:
            return None

        def __getattr__(self, name: str):
            return getattr(self._session, name)

    class _SessionFactory:
        def __call__(self):
            return _SessionProxy(pg_session)

    monkeypatch.setattr(sqlalchemy.orm, "sessionmaker", lambda bind: _SessionFactory())


def test_cli_dry_run_default_success(pg_session, postgres_engine, monkeypatch, capsys) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    cli = _load_cli_module()
    monkeypatch.setenv("DATABASE_URL", _database_url(postgres_engine))
    _patch_cli_session(monkeypatch, pg_session)
    monkeypatch.setattr(sys, "argv", [_CLI, "--tenant-id", str(TEST_TENANT_A)])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["write_schema_version"] == WRITE_SCHEMA_VERSION
    assert payload["dry_run"] is True
    _assert_no_pii_in_write(payload, known_safe=(str(TEST_TENANT_A),))


def test_cli_write_rejects_without_confirmation(pg_session, postgres_engine) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    env = {
        **os.environ,
        "DATABASE_URL": (
            "postgresql://nahla:nahla_password@"
            "postgres-staging.railway.internal:5432/nahla_saas"
        ),
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
    }
    result = subprocess.run(
        [sys.executable, _CLI, "--tenant-id", str(TEST_TENANT_A), "--write"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["gate_stage"] == "dangerous_action_not_confirmed"


def test_cli_write_requires_staging_database_host(pg_session, postgres_engine) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    env = _staging_env(
        DATABASE_URL="postgresql://user:pass@db-other.example.com:5432/nahla",
    )
    result = subprocess.run(
        [sys.executable, _CLI, "--tenant-id", str(TEST_TENANT_A), "--write"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["gate_stage"] == "database_host_not_allowlisted"


def test_cli_write_success_with_confirmation_in_process(pg_session, postgres_engine, monkeypatch) -> None:
    _seed_generic_commerce_linked_scope(pg_session)
    cli = _load_cli_module()
    monkeypatch.setenv("DATABASE_URL", _database_url(postgres_engine))
    _patch_cli_session(monkeypatch, pg_session)
    monkeypatch.setattr(cli, "_validate_write_gates", lambda _env: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [_CLI, "--tenant-id", str(TEST_TENANT_A), "--write"],
    )
    assert cli.main() == 0
