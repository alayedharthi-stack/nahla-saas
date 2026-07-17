#!/usr/bin/env python3
"""Staging-only A1 generic-commerce evidence fixture harness.

Creates internal + external authoritative A1 order evidence through existing
service APIs so G4 reconciliation gates can be verified non-vacuously before
any 0088 validation decision. Dry-run by default.

Usage::

    python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py --tenant-id 42
    export NAHLA_A1_EVIDENCE_FIXTURE_WRITE_CONFIRM=RUN_A1_EVIDENCE_FIXTURE_WRITE
    python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \\
        --tenant-id 42 --write
    export NAHLA_A1_EVIDENCE_FIXTURE_CLEANUP_CONFIRM=RUN_A1_EVIDENCE_FIXTURE_CLEANUP
    python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \\
        --tenant-id 42 --cleanup --write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))
sys.path.insert(0, str(ROOT))

from scripts.operators.staging_migration_operator_gates import (  # noqa: E402
    GateFailure,
    validate_confirmation,
    validate_database_binding,
    validate_staging_identity,
)
from services.order_customer_identity_evidence_fixture import (  # noqa: E402
    execute_order_customer_identity_evidence_fixture_cleanup,
    execute_order_customer_identity_evidence_fixture_seed,
)
from services.order_customer_identity_evidence_fixture_contract import (  # noqa: E402
    CONFIRMATION_ENV_CLEANUP,
    CONFIRMATION_ENV_WRITE,
    CONFIRMATION_TOKEN_CLEANUP,
    CONFIRMATION_TOKEN_WRITE,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
)


def _validate_write_gates(env: Mapping[str, str]) -> GateFailure | None:
    failure = validate_staging_identity(
        env,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if failure:
        return failure
    failure = validate_database_binding(env)
    if failure:
        return failure
    return validate_confirmation(
        env,
        confirmation_env=CONFIRMATION_ENV_WRITE,
        confirmation_token=CONFIRMATION_TOKEN_WRITE,
    )


def _validate_cleanup_gates(env: Mapping[str, str]) -> GateFailure | None:
    failure = validate_staging_identity(
        env,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if failure:
        return failure
    failure = validate_database_binding(env)
    if failure:
        return failure
    return validate_confirmation(
        env,
        confirmation_env=CONFIRMATION_ENV_CLEANUP,
        confirmation_token=CONFIRMATION_TOKEN_CLEANUP,
    )


def _gate_payload(failure: GateFailure) -> dict[str, Any]:
    return {
        "outcome": "failed",
        "access_status": "gate_rejected",
        "gate_stage": failure.stage,
        "gate_error_class": failure.error_class,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Staging-only A1 generic-commerce evidence fixture harness "
            "(dry-run default)."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        type=int,
        required=True,
        help="Required tenant scope. Never scans all tenants.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist fixture rows (requires write confirmation token).",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete only fixture-namespace-owned rows (requires cleanup token).",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    if int(args.tenant_id) <= 0:
        parser.error("--tenant-id must be a positive integer")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    dry_run = not bool(args.write)
    if not dry_run:
        if args.cleanup:
            gate_failure = _validate_cleanup_gates(os.environ)
        else:
            gate_failure = _validate_write_gates(os.environ)
        if gate_failure:
            indent = 2 if args.pretty else None
            print(
                json.dumps(_gate_payload(gate_failure), ensure_ascii=False, indent=indent),
            )
            return 1

    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        if args.cleanup:
            result = execute_order_customer_identity_evidence_fixture_cleanup(
                db,
                int(args.tenant_id),
                dry_run=dry_run,
            )
        else:
            result = execute_order_customer_identity_evidence_fixture_seed(
                db,
                int(args.tenant_id),
                dry_run=dry_run,
            )
        indent = 2 if args.pretty else None
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=indent))
        print(result.summary_line(), file=sys.stderr)
        if result.access_status != "ok":
            return 2
        if result.outcome not in ("success", "aborted"):
            return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
