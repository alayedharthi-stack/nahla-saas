"""Explicit, scoped deployment step for the 0114 index only (no Alembic stamp).

Read-only audit happens first. Duplicate historical responses block deployment.
No other migration, data mutation, model call, or tenant-setting write is made.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import psycopg2
import sqlalchemy as sa
from datetime import datetime, timezone
from database.ai_usage_dedup_helpers import install_index
from scripts.audit_ai_cost_counters_readonly import audit, encode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--tenant", type=int, action="append", required=True)
    args = parser.parse_args()
    expected = {"RAILWAY_PROJECT_ID": args.project, "RAILWAY_ENVIRONMENT_ID": args.environment,
                "RAILWAY_SERVICE_ID": args.service}
    if any(os.environ.get(key) != value for key, value in expected.items()):
        print(json.dumps({"status": "blocked", "reason": "deployment_scope_mismatch"}))
        return 2
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        print(json.dumps({"status": "blocked", "reason": "postgres_database_required"}))
        return 2
    connection = None
    engine = None
    try:
        connection = psycopg2.connect(dsn, connect_timeout=10,
            options="-c statement_timeout=10000 -c default_transaction_read_only=on",
            application_name="nahla-ai-cost-predeploy-readonly")
        before = audit(connection, args.tenant, as_of=datetime.now(timezone.utc), days=7, recent=5)
        print("AI_COST_AUDIT_BEFORE " + json.dumps(before, default=encode), flush=True)
        if before["duplicate_request_groups"]:
            print(json.dumps({"status": "blocked", "reason": "historical_duplicates_require_review"}))
            return 3
        engine = sa.create_engine(dsn.replace("postgres://", "postgresql://", 1),
            connect_args={"connect_timeout": 10})
        with engine.begin() as bind:
            bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            bind.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
            result = install_index(bind)
        print("AI_COST_INDEX " + json.dumps({"status": result, "historical_rows_changed": 0,
            "alembic_version_changed": False}), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — never log DSNs, SQL params, or customer content
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}), flush=True)
        return 1
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
