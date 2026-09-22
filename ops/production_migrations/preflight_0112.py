#!/usr/bin/env python3
"""Read-only production readiness report for shipment tracking revision 0112.

This program deliberately does not import the application or Alembic.  It
opens one READ ONLY transaction, reports the migration and schema facts needed
before ``alembic upgrade 0112``, and exits without making any data or DDL
change.  The Railway service that runs it is intentionally not an application
server.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

FOUNDATION_COLUMNS: dict[str, tuple[bool, set[str]]] = {
    "id": (False, {"integer"}),
    "tenant_id": (False, {"integer"}),
    "order_id": (False, {"integer"}),
    "provider": (False, {"character varying"}),
    "status": (False, {"character varying"}),
    "tracking_number": (True, {"character varying"}),
    "label_url": (True, {"character varying"}),
    "label_pdf_path": (True, {"character varying"}),
    "recipient_name": (True, {"character varying"}),
    "recipient_phone": (True, {"character varying"}),
    "address_type": (True, {"character varying"}),
    "address_text": (True, {"text"}),
    "address_url": (True, {"character varying"}),
    "latitude": (True, {"character varying"}),
    "longitude": (True, {"character varying"}),
    "cod_amount": (True, {"character varying"}),
    "created_at": (True, {"timestamp without time zone"}),
    "updated_at": (True, {"timestamp without time zone"}),
    "metadata": (True, {"jsonb"}),
}
TRACKING_COLUMNS: dict[str, tuple[bool, set[str]]] = {
    "tracking_data_source": (True, {"character varying"}),
    "external_shipment_id": (True, {"character varying"}),
    "carrier": (True, {"character varying"}),
    "tracking_url": (True, {"character varying"}),
    "latest_event": (True, {"jsonb"}),
    "source_event_at": (True, {"timestamp with time zone"}),
    "last_verified_at": (True, {"timestamp with time zone"}),
}


def _fail(message: str) -> "None":
    print(json.dumps({"status": "failed", "reason": message}, sort_keys=True))
    raise SystemExit(1)


def _constraint_matches(constraints: list[dict[str, str]], *, kind: str, pieces: tuple[str, ...]) -> bool:
    return any(
        constraint["type"] == kind
        and all(piece in constraint["definition"] for piece in pieces)
        for constraint in constraints
    )


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        # Allows the runner service to be built and configured before its
        # database reference is attached.  It has made no connection or change.
        print(json.dumps({"status": "idle", "reason": "DATABASE_URL is not configured"}, sort_keys=True))
        return 0

    # Keep the no-connection configuration state dependency-free.  The
    # container image installs SQLAlchemy before it is ever given a database
    # reference.
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(database_url)
    except Exception as exc:  # pragma: no cover - deployment configuration guard
        _fail(f"DATABASE_URL is not parseable: {exc.__class__.__name__}")
    if (parsed.host or "").lower() in {"localhost", "127.0.0.1", "::1"}:
        _fail("refusing a local DATABASE_URL")

    # AUTOCOMMIT lets us explicitly begin a READ ONLY transaction after the
    # connection dialect's own harmless capability probe.  No query below is
    # issued outside that transaction.
    # The ops runner deliberately installs psycopg 3 rather than inheriting
    # the application's legacy psycopg2 build dependency.  Keep the Railway
    # reference value intact apart from choosing that explicit SQLAlchemy
    # driver.
    driver_url = str(parsed.set(drivername="postgresql+psycopg"))
    engine = create_engine(driver_url, pool_pre_ping=True, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("SET default_transaction_read_only = on"))
            connection.execute(text("BEGIN READ ONLY"))
            postgres_version = connection.execute(text("SELECT version()")).scalar_one()
            database_size_bytes = connection.execute(
                text("SELECT pg_database_size(current_database())")
            ).scalar_one()
            alembic_present = connection.execute(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one()
            alembic_versions = (
                list(
                    connection.execute(
                        text("SELECT version_num FROM alembic_version ORDER BY version_num")
                    ).scalars()
                )
                if alembic_present
                else []
            )
            table_present = connection.execute(
                text("SELECT to_regclass('public.order_shipments') IS NOT NULL")
            ).scalar_one()
            columns = []
            constraints = []
            if table_present:
                columns = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT column_name AS name,
                                   is_nullable = 'YES' AS nullable,
                                   data_type
                              FROM information_schema.columns
                             WHERE table_schema = 'public'
                               AND table_name = 'order_shipments'
                             ORDER BY ordinal_position
                            """
                        )
                    ).mappings()
                ]
                constraints = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT constraint_name AS name,
                                   constraint_type AS type,
                                   constraint_definition AS definition
                              FROM (
                                SELECT con.conname AS constraint_name,
                                       CASE con.contype
                                         WHEN 'p' THEN 'PRIMARY KEY'
                                         WHEN 'u' THEN 'UNIQUE'
                                         WHEN 'f' THEN 'FOREIGN KEY'
                                         ELSE con.contype::text
                                       END AS constraint_type,
                                       pg_get_constraintdef(con.oid) AS constraint_definition
                                  FROM pg_constraint con
                                  JOIN pg_class rel ON rel.oid = con.conrelid
                                  JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                                 WHERE ns.nspname = 'public'
                                   AND rel.relname = 'order_shipments'
                              ) AS shipment_constraints
                             ORDER BY constraint_name
                            """
                        )
                    ).mappings()
                ]
            connection.execute(text("ROLLBACK"))
    finally:
        engine.dispose()

    by_name = {column["name"]: column for column in columns}
    foundation_mismatches: list[str] = []
    for name, (nullable, allowed_types) in FOUNDATION_COLUMNS.items():
        column = by_name.get(name)
        if column is None:
            foundation_mismatches.append(f"missing:{name}")
        elif bool(column["nullable"]) is not nullable or column["data_type"] not in allowed_types:
            foundation_mismatches.append(
                f"shape:{name}:{column['data_type']}:{str(column['nullable']).lower()}"
            )
    tracking_mismatches: list[str] = []
    for name, (nullable, allowed_types) in TRACKING_COLUMNS.items():
        column = by_name.get(name)
        if column is not None and (
            bool(column["nullable"]) is not nullable or column["data_type"] not in allowed_types
        ):
            tracking_mismatches.append(
                f"shape:{name}:{column['data_type']}:{str(column['nullable']).lower()}"
            )

    foundation_constraints_ok = (
        _constraint_matches(constraints, kind="PRIMARY KEY", pieces=("(id)",))
        and _constraint_matches(constraints, kind="UNIQUE", pieces=("(order_id)",))
        and _constraint_matches(constraints, kind="FOREIGN KEY", pieces=("(tenant_id)", "REFERENCES tenants(id)"))
        and _constraint_matches(constraints, kind="FOREIGN KEY", pieces=("(order_id)", "REFERENCES orders(id)"))
    )
    target_unique = next(
        (constraint for constraint in constraints if constraint["name"] == "uq_order_shipments_tenant_tracking_source_ref"),
        None,
    )
    target_unique_ok = target_unique is None or (
        target_unique["type"] == "UNIQUE"
        and "(tenant_id, tracking_data_source, external_shipment_id)" in target_unique["definition"]
    )
    schema_ready = bool(table_present) and not foundation_mismatches and not tracking_mismatches and foundation_constraints_ok and target_unique_ok
    report: dict[str, Any] = {
        "status": "ok",
        "read_only": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "postgres_version": postgres_version,
        "database_size_bytes": int(database_size_bytes),
        "alembic_versions": alembic_versions,
        "order_shipments_present": bool(table_present),
        "order_shipments_columns": columns,
        "order_shipments_constraints": constraints,
        "foundation_mismatches": foundation_mismatches,
        "tracking_mismatches": tracking_mismatches,
        "foundation_constraints_ok": foundation_constraints_ok,
        "target_unique_ok": target_unique_ok,
        "schema_ready_for_explicit_0112": schema_ready,
        "note": "A manual recoverable volume backup must be verified separately before migration; this read-only report is not that backup.",
    }
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
