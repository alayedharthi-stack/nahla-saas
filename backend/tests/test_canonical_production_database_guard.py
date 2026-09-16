"""Tests for the fail-closed canonical production database identity guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO / "scripts" / "operators" / "verify_canonical_production_database.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "verify_canonical_production_database",
    _MODULE_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


def _contract() -> dict:
    return guard.load_contract(_REPO / "config" / "canonical_production_database.json")


def _status(contract: dict) -> dict:
    return {
        "name": contract["project"],
        "environments": {
            "edges": [
                {
                    "node": {
                        "name": contract["environment"],
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceId": contract["postgres_service_id"],
                                        "serviceName": contract["postgres_service"],
                                        "latestDeployment": {
                                            "status": "SUCCESS",
                                            "deploymentStopped": False,
                                        },
                                    }
                                },
                                {
                                    "node": {
                                        "serviceId": "application-id",
                                        "serviceName": contract["application_service"],
                                        "latestDeployment": {
                                            "status": "SUCCESS",
                                            "deploymentStopped": False,
                                        },
                                    }
                                },
                            ]
                        },
                        "volumeInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceId": contract["postgres_service_id"],
                                        "mountPath": contract["volume_mount_path"],
                                        "volume": {
                                            "id": contract["volume_id"],
                                            "name": contract["volume_name"],
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }


def _probe(contract: dict) -> dict:
    return {
        "database_name": contract["database_name"],
        "alembic_version": contract["alembic_version"],
        "tenant_1_products_count": contract["tenant_1_products_count_reference"],
        "sql_health_value": 1,
    }


def test_contract_pins_canonical_service_volume_and_reference() -> None:
    contract = _contract()
    assert contract["postgres_service"] == "nahla-postgres-prod"
    assert contract["postgres_service_id"] == "b77b3d27-47b0-4a3a-83fd-44def66a3a84"
    assert contract["volume_id"] == "009bd0d5-85ed-4de4-99fc-94ea963c9d65"
    assert contract["managed_reference"] == "${{nahla-postgres-prod.DATABASE_URL}}"
    assert contract["legacy_noncanonical_target"]["approved_for_nahla_saas"] is False


def test_status_and_probe_match_canonical_contract() -> None:
    contract = _contract()
    identity = guard.validate_railway_status(_status(contract), contract)
    guard.validate_probe(_probe(contract), contract)
    assert identity["postgres_status"] == "SUCCESS"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("name", "wrong-project", "project_identity_mismatch"),
    ],
)
def test_status_mismatch_fails_closed(field: str, value: str, reason: str) -> None:
    contract = _contract()
    status = _status(contract)
    status[field] = value
    with pytest.raises(guard.GuardFailure, match=reason):
        guard.validate_railway_status(status, contract)


def test_volume_mismatch_fails_closed() -> None:
    contract = _contract()
    status = _status(contract)
    volume = status["environments"]["edges"][0]["node"]["volumeInstances"]["edges"][0]
    volume["node"]["volume"]["id"] = "wrong-volume"
    with pytest.raises(guard.GuardFailure, match="canonical_postgres_volume_mismatch"):
        guard.validate_railway_status(status, contract)


def test_baseline_mismatch_fails_closed() -> None:
    contract = _contract()
    probe = _probe(contract)
    probe["alembic_version"] = "0093"
    with pytest.raises(guard.GuardFailure, match="alembic_version_mismatch"):
        guard.validate_probe(probe, contract)


def test_change_record_requires_authorization_canonical_target_and_rollback() -> None:
    guard.validate_change_record(
        authorization_ref="INC-2026-08-02",
        old_binding="legacy_postgres_reference",
        new_binding="canonical_postgres_reference",
        rollback_plan_id="RUNBOOK-CANONICAL-DB",
    )
    with pytest.raises(
        guard.GuardFailure,
        match="new_binding_must_be_canonical_postgres_reference",
    ):
        guard.validate_change_record(
            authorization_ref="INC-2026-08-02",
            old_binding="historical_literal",
            new_binding="legacy_postgres_reference",
            rollback_plan_id="RUNBOOK-CANONICAL-DB",
        )


def test_probe_parser_accepts_only_sanitized_technical_tuple() -> None:
    assert guard.parse_probe_output("railway|0096|28|1\n") == {
        "database_name": "railway",
        "alembic_version": "0096",
        "tenant_1_products_count": 28,
        "sql_health_value": 1,
    }
    with pytest.raises(guard.GuardFailure, match="technical_probe_output_invalid"):
        guard.parse_probe_output("postgresql://credential-bearing-value")
