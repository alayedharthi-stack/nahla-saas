#!/usr/bin/env python3
"""Fail-closed, read-only guard for production database binding changes.

The guard never reads or prints a resolved connection URL. It verifies the
Railway service/volume identity from metadata, then runs a fixed read-only
technical probe either directly in the canonical Postgres service (pre-change)
or through ``nahla-saas`` (post-change).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT_PATH = REPO_ROOT / "config" / "canonical_production_database.json"
SAFE_BINDING_LABELS = frozenset(
    {
        "historical_literal",
        "legacy_postgres_reference",
        "canonical_postgres_reference",
        "unknown",
    }
)
SAFE_AUDIT_TOKEN = re.compile(r"^[A-Za-z0-9._:/-]+$")


class GuardFailure(RuntimeError):
    """A sanitized, operator-actionable guard failure."""


def load_contract(path: Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_change_record(
    *,
    authorization_ref: str,
    old_binding: str,
    new_binding: str,
    rollback_plan_id: str,
) -> None:
    if not authorization_ref or not SAFE_AUDIT_TOKEN.fullmatch(authorization_ref):
        raise GuardFailure("authorization_ref_missing_or_unsafe")
    if old_binding not in SAFE_BINDING_LABELS:
        raise GuardFailure("old_binding_label_invalid")
    if new_binding != "canonical_postgres_reference":
        raise GuardFailure("new_binding_must_be_canonical_postgres_reference")
    if not rollback_plan_id or not SAFE_AUDIT_TOKEN.fullmatch(rollback_plan_id):
        raise GuardFailure("rollback_plan_id_missing_or_unsafe")


def _nodes(edge_container: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge["node"] for edge in edge_container.get("edges", [])]


def validate_railway_status(
    status: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if status.get("name") != contract["project"]:
        raise GuardFailure("project_identity_mismatch")

    environments = _nodes(status.get("environments", {}))
    environment = next(
        (item for item in environments if item.get("name") == contract["environment"]),
        None,
    )
    if environment is None:
        raise GuardFailure("environment_identity_mismatch")

    services = _nodes(environment.get("serviceInstances", {}))
    postgres = next(
        (
            item
            for item in services
            if item.get("serviceId") == contract["postgres_service_id"]
        ),
        None,
    )
    if postgres is None or postgres.get("serviceName") != contract["postgres_service"]:
        raise GuardFailure("canonical_postgres_service_mismatch")
    latest = postgres.get("latestDeployment") or {}
    if latest.get("status") != "SUCCESS" or latest.get("deploymentStopped"):
        raise GuardFailure("canonical_postgres_not_healthy")

    application = next(
        (
            item
            for item in services
            if item.get("serviceName") == contract["application_service"]
        ),
        None,
    )
    if application is None:
        raise GuardFailure("application_service_missing")

    volumes = _nodes(environment.get("volumeInstances", {}))
    volume = next(
        (
            item
            for item in volumes
            if item.get("serviceId") == contract["postgres_service_id"]
        ),
        None,
    )
    if (
        volume is None
        or (volume.get("volume") or {}).get("id") != contract["volume_id"]
        or (volume.get("volume") or {}).get("name") != contract["volume_name"]
        or volume.get("mountPath") != contract["volume_mount_path"]
    ):
        raise GuardFailure("canonical_postgres_volume_mismatch")

    return {
        "project": contract["project"],
        "environment": contract["environment"],
        "application_service": contract["application_service"],
        "postgres_service": contract["postgres_service"],
        "postgres_service_id": contract["postgres_service_id"],
        "volume_id": contract["volume_id"],
        "postgres_status": "SUCCESS",
    }


def parse_probe_output(raw: str) -> dict[str, Any]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise GuardFailure("technical_probe_output_invalid")
    fields = lines[0].split("|")
    if len(fields) != 4:
        raise GuardFailure("technical_probe_output_invalid")
    database_name, alembic_version, product_count, health_value = fields
    try:
        count = int(product_count)
        health = int(health_value)
    except ValueError as exc:
        raise GuardFailure("technical_probe_output_invalid") from exc
    return {
        "database_name": database_name,
        "alembic_version": alembic_version,
        "tenant_1_products_count": count,
        "sql_health_value": health,
    }


def validate_probe(probe: dict[str, Any], contract: dict[str, Any]) -> None:
    if probe["database_name"] != contract["database_name"]:
        raise GuardFailure("database_name_mismatch")
    if probe["alembic_version"] != contract["alembic_version"]:
        raise GuardFailure("alembic_version_mismatch")
    if (
        probe["tenant_1_products_count"]
        != contract["tenant_1_products_count_reference"]
    ):
        raise GuardFailure("tenant_1_product_count_reference_mismatch")
    if probe["sql_health_value"] != 1:
        raise GuardFailure("sql_health_query_failed")


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def railway_executable() -> str:
    executable = shutil.which("railway.cmd" if os.name == "nt" else "railway")
    if not executable:
        raise GuardFailure("railway_cli_not_found")
    return executable


def read_railway_status() -> dict[str, Any]:
    return _run_json([railway_executable(), "status", "--json"])


def run_technical_probe(*, service: str, environment: str) -> dict[str, Any]:
    sql = (
        "SELECT current_database(), "
        "(SELECT version_num FROM alembic_version), "
        "(SELECT COUNT(*) FROM products WHERE tenant_id=1), 1;"
    )
    if service == "nahla-saas":
        source = f"""
import os
try:
    import psycopg
    connection = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=8)
except ImportError:
    import psycopg2
    connection = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8)
cursor = connection.cursor()
cursor.execute({sql!r})
row = cursor.fetchone()
print("|".join(str(value) for value in row))
connection.close()
""".strip()
        payload = base64.b64encode(source.encode("utf-8")).decode("ascii")
        remote_command = f"echo {payload} | base64 -d | python3"
    else:
        shell = (
            "set -eu\n"
            f"psql -v ON_ERROR_STOP=1 -tA -F '|' -c \"{sql}\" 2>/dev/null\n"
        )
        payload = base64.b64encode(shell.encode("utf-8")).decode("ascii")
        remote_command = f"echo {payload} | base64 -d | sh"

    completed = subprocess.run(
        [
            railway_executable(),
            "ssh",
            "-s",
            service,
            "-e",
            environment,
            "--",
            remote_command,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return parse_probe_output(completed.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("pre-change", "post-change"), required=True)
    parser.add_argument("--authorization-ref", required=True)
    parser.add_argument("--old-binding", choices=sorted(SAFE_BINDING_LABELS), required=True)
    parser.add_argument(
        "--new-binding",
        choices=sorted(SAFE_BINDING_LABELS),
        required=True,
    )
    parser.add_argument("--rollback-plan-id", required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT_PATH)
    parser.add_argument("--status-json", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--probe-json", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_change_record(
            authorization_ref=args.authorization_ref,
            old_binding=args.old_binding,
            new_binding=args.new_binding,
            rollback_plan_id=args.rollback_plan_id,
        )
        contract = load_contract(args.contract)
        status = (
            json.loads(args.status_json.read_text(encoding="utf-8"))
            if args.status_json
            else read_railway_status()
        )
        identity = validate_railway_status(status, contract)
        probe_service = (
            contract["postgres_service"]
            if args.phase == "pre-change"
            else contract["application_service"]
        )
        probe = (
            json.loads(args.probe_json.read_text(encoding="utf-8"))
            if args.probe_json
            else run_technical_probe(
                service=probe_service,
                environment=contract["environment"],
            )
        )
        validate_probe(probe, contract)
    except (GuardFailure, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        reason = exc.args[0] if isinstance(exc, GuardFailure) else type(exc).__name__
        print(json.dumps({"guard": "FAIL", "reason": reason}, sort_keys=True))
        return 1

    report = {
        "guard": "PASS",
        "phase": args.phase,
        "authorization_ref": args.authorization_ref,
        "old_binding": args.old_binding,
        "new_binding": args.new_binding,
        "rollback_plan_id": args.rollback_plan_id,
        **identity,
        "database_name": probe["database_name"],
        "alembic_version": probe["alembic_version"],
        "tenant_1_products_count": probe["tenant_1_products_count"],
        "sql_health_query_succeeds": probe["sql_health_value"] == 1,
        "credentials_or_urls_printed": False,
    }
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
