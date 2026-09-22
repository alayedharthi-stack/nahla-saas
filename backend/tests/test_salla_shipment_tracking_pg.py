"""PostgreSQL proof for the Salla tracking identity constraint.

SQLite unit tests cover normalisation and out-of-order arrival. PostgreSQL is
required here because the tenant/source/shipment uniqueness and transactional
conflict behaviour are database guarantees, not application conventions.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session
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
from database.models import OrderShipment  # noqa: E402

_SHIPMENT_REF = "s1"
_MIGRATION_PATH = _REPO / "database" / "migrations" / "versions" / "0112_salla_shipment_tracking.py"
_SPEC = importlib.util.spec_from_file_location("migration_0112_shipment_tracking", _MIGRATION_PATH)
assert _SPEC and _SPEC.loader
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def _admin_or_skip():
    try:
        return connect_engine()
    except pytest.skip.Exception:
        if os.getenv("LEGACY_MIG_PG_TEST_DATABASE_URL", "").strip():
            pytest.fail("PostgreSQL configured but unavailable for shipment tracking proof")
        pytest.skip("PostgreSQL DSN is not configured")


@contextmanager
def _ephemeral_tracking_database():
    admin = _admin_or_skip()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


def _versions(engine) -> set[str]:
    if "alembic_version" not in inspect(engine).get_table_names():
        return set()
    with engine.connect() as conn:
        return {
            row["version_num"]
            for row in conn.execute(text("SELECT version_num FROM alembic_version")).mappings()
        }


def _run_0112_preflight_direct(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the revision guard without an Alembic command or stamp."""
    with engine.begin() as conn:
        monkeypatch.setattr(_MIGRATION.op, "get_bind", lambda: conn)
        _MIGRATION.upgrade()


def _create_foundation_without_alembic_stamp(
    engine,
    *,
    include_metadata: bool = True,
    tracking_data_source_type: str | None = None,
    wrong_foundation_unique: bool = False,
) -> None:
    metadata = ", metadata JSONB" if include_metadata else ""
    tracking = (
        f", tracking_data_source {tracking_data_source_type} NULL"
        if tracking_data_source_type is not None
        else ""
    )
    unique_columns = "tenant_id, order_id" if wrong_foundation_unique else "order_id"
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id SERIAL PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE orders (id SERIAL PRIMARY KEY)"))
        conn.execute(text(f"""
            CREATE TABLE order_shipments (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                order_id INTEGER NOT NULL REFERENCES orders(id),
                provider VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                tracking_number VARCHAR NULL,
                label_url VARCHAR NULL,
                label_pdf_path VARCHAR NULL,
                recipient_name VARCHAR NULL,
                recipient_phone VARCHAR NULL,
                address_type VARCHAR NULL,
                address_text TEXT NULL,
                address_url VARCHAR NULL,
                latitude VARCHAR NULL,
                longitude VARCHAR NULL,
                cod_amount VARCHAR NULL,
                created_at TIMESTAMP NULL,
                updated_at TIMESTAMP NULL
                {metadata}
                {tracking},
                CONSTRAINT uq_order_shipments_order_id UNIQUE ({unique_columns})
            )
        """))


def test_0112_requires_migration_before_current_orm_tracking_query():
    with _ephemeral_tracking_database() as engine:
        run_alembic(engine, "0110")
        assert "0112" not in _versions(engine)

        with engine.begin() as conn:
            tenant_id = conn.execute(
                text("INSERT INTO tenants (name) VALUES ('tracking-orm') RETURNING id")
            ).scalar_one()
            order_id = conn.execute(
                text("INSERT INTO orders (tenant_id, status) VALUES (:tenant, 'paid') RETURNING id"),
                {"tenant": tenant_id},
            ).scalar_one()
            conn.execute(text("""
                INSERT INTO order_shipments (tenant_id, order_id, provider, status)
                VALUES (:tenant, :order, 'salla', 'delivering')
            """), {"tenant": tenant_id, "order": order_id})

        # A model that selects 0112 tracking evidence cannot safely precede
        # the explicit migration: PostgreSQL rejects the missing column.
        with Session(engine) as session, pytest.raises(ProgrammingError):
            session.execute(select(OrderShipment.tracking_data_source)).all()

        run_alembic(engine, "0112")
        with Session(engine) as session:
            assert session.execute(select(OrderShipment.tracking_data_source)).all() == [(None,)]
            # Legacy shipment fields remain readable after the additive change.
            assert session.execute(
                select(
                    OrderShipment.id,
                    OrderShipment.tenant_id,
                    OrderShipment.order_id,
                    OrderShipment.provider,
                    OrderShipment.status,
                    OrderShipment.tracking_number,
                )
            ).all()


def test_0112_enforces_salla_shipment_identity_per_tenant():
    with _ephemeral_tracking_database() as engine:
        run_alembic(engine, "0110")
        assert "0112" not in _versions(engine)
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


def test_0112_rejects_absent_order_shipments_without_alembic_stamp(monkeypatch: pytest.MonkeyPatch):
    with _ephemeral_tracking_database() as engine:
        assert _versions(engine) == set()
        with pytest.raises(RuntimeError, match="required pre-0112 shipment foundation"):
            _run_0112_preflight_direct(engine, monkeypatch)
        assert _versions(engine) == set()
        assert "order_shipments" not in inspect(engine).get_table_names()


def test_0112_rejects_missing_or_wrong_preexisting_foundation_without_stamp(
    monkeypatch: pytest.MonkeyPatch,
):
    cases = (
        {"include_metadata": False},
        {"wrong_foundation_unique": True},
        {"tracking_data_source_type": "INTEGER"},
    )
    for case in cases:
        with _ephemeral_tracking_database() as engine:
            _create_foundation_without_alembic_stamp(engine, **case)
            assert _versions(engine) == set()
            with pytest.raises(RuntimeError, match="required pre-0112 shipment foundation"):
                _run_0112_preflight_direct(engine, monkeypatch)
            assert _versions(engine) == set()
            columns = {column["name"] for column in inspect(engine).get_columns("order_shipments")}
            if case.get("tracking_data_source_type") is None:
                assert "tracking_data_source" not in columns
            else:
                assert "tracking_data_source" in columns
