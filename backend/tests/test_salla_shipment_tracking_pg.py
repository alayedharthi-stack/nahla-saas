"""PostgreSQL proof for the Salla tracking identity constraint.

SQLite unit tests cover normalisation and out-of-order arrival. PostgreSQL is
required here because the tenant/source/shipment uniqueness and transactional
conflict behaviour are database guarantees, not application conventions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for path in (_REPO, _REPO / "backend", _REPO / "database", _REPO / "backend" / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)

_SHIPMENT_REF = "s1"


def _admin_or_skip():
    try:
        return connect_engine()
    except pytest.skip.Exception:
        if os.getenv("LEGACY_MIG_PG_TEST_DATABASE_URL", "").strip():
            pytest.fail("PostgreSQL configured but unavailable for shipment tracking proof")
        pytest.skip("PostgreSQL DSN is not configured")


def test_0112_enforces_salla_shipment_identity_per_tenant():
    admin = _admin_or_skip()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
    )
    try:
        run_alembic(engine, "0110")
        with engine.connect() as conn:
            versions = {
                row["version_num"]
                for row in conn.execute(text("SELECT version_num FROM alembic_version")).mappings()
            }
        assert "0112" not in versions
        run_alembic(engine, "0112")

        table = "order_shipments"
        columns = {item["name"] for item in inspect(engine).get_columns(table)}
        assert {
            "tracking_data_source", "external_shipment_id", "carrier", "tracking_url",
            "latest_event", "source_event_at", "last_verified_at",
        } <= columns
        assert "uq_order_shipments_tenant_tracking_source_ref" in {
            item["name"] for item in inspect(engine).get_unique_constraints(table)
        }

        with engine.begin() as conn:
            tenant_one = conn.execute(text("INSERT INTO tenants (name) VALUES ('tracking-pg-one') RETURNING id")).scalar_one()
            tenant_two = conn.execute(text("INSERT INTO tenants (name) VALUES ('tracking-pg-two') RETURNING id")).scalar_one()
            order_one = conn.execute(text("INSERT INTO orders (tenant_id, status) VALUES (:tenant, 'paid') RETURNING id"), {"tenant": tenant_one}).scalar_one()
            order_two = conn.execute(text("INSERT INTO orders (tenant_id, status) VALUES (:tenant, 'paid') RETURNING id"), {"tenant": tenant_one}).scalar_one()
            order_other_tenant = conn.execute(text("INSERT INTO orders (tenant_id, status) VALUES (:tenant, 'paid') RETURNING id"), {"tenant": tenant_two}).scalar_one()
            conn.execute(text("""
                INSERT INTO order_shipments
                    (tenant_id, order_id, provider, status, tracking_data_source, external_shipment_id)
                VALUES (:tenant, :order, 'salla', 'delivering', 'salla_merchant_api', :shipment_ref)
            """), {"tenant": tenant_one, "order": order_one, "shipment_ref": _SHIPMENT_REF})

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO order_shipments
                        (tenant_id, order_id, provider, status, tracking_data_source, external_shipment_id)
                    VALUES (:tenant, :order, 'salla', 'delivering', 'salla_merchant_api', :shipment_ref)
                """), {"tenant": tenant_one, "order": order_two, "shipment_ref": _SHIPMENT_REF})

        # The same external token in a separate tenant remains isolated rather
        # than globally coupling two stores.
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO order_shipments
                    (tenant_id, order_id, provider, status, tracking_data_source, external_shipment_id)
                VALUES (:tenant, :order, 'salla', 'delivering', 'salla_merchant_api', :shipment_ref)
            """), {"tenant": tenant_two, "order": order_other_tenant, "shipment_ref": _SHIPMENT_REF})
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()
