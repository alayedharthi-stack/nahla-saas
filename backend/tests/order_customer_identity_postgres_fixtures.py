"""PostgreSQL fixtures for A1-v3.7 order-customer identity integration tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"

for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import (  # noqa: E402
    Customer,
    ExternalCustomerProfile,
    Integration,
    Order,
    Tenant,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EXTERNAL_PROVIDER_SALLA_V1,
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_MANUAL,
    ORDER_SOURCE_NAHL_INTERNAL,
    ORDER_SOURCE_OTHER,
    ORDER_SOURCE_WHATSAPP,
)

TEST_TENANT_A = 990_001
TEST_TENANT_B = 990_002


def _candidate_database_urls() -> list[str]:
    urls: list[str] = []
    explicit = (os.getenv("A1_PG_TEST_DATABASE_URL") or "").strip()
    if explicit:
        urls.append(explicit)
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url and db_url not in urls:
        urls.append(db_url)
    default = "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas"
    if default not in urls:
        urls.append(default)
    return urls


def _integration_required() -> bool:
    return bool((os.getenv("A1_PG_INTEGRATION_REQUIRED") or "").strip() == "1")


def _connect_engine() -> Engine:
    from sqlalchemy import create_engine as sa_create_engine

    last_error: Optional[Exception] = None
    for url in _candidate_database_urls():
        try:
            engine = sa_create_engine(url, poolclass=NullPool, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = f"PostgreSQL unavailable for A1 integration tests: {last_error}"
    if _integration_required():
        pytest.fail(message)
    pytest.skip(message)


def _ensure_a1_schema(engine: Engine) -> None:
    from alembic import command
    from alembic.config import Config

    import os

    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        cfg = Config("alembic.ini")
        cfg.set_main_option(
            "sqlalchemy.url",
            str(engine.url.render_as_string(hide_password=False)),
        )
        os.environ["DATABASE_URL"] = str(engine.url.render_as_string(hide_password=False))
        # Parallel heads 0088 (Validate) and 0089 (bindings). Integration
        # fixtures pin to 0089 so capability remains expand until Validate.
        command.upgrade(cfg, "0093")
    finally:
        os.chdir(prev_cwd)
    _ensure_capability_state_row(engine)


def _ensure_capability_state_row(engine: Engine) -> None:
    insp = inspect(engine)
    if "external_customer_profiles" not in insp.get_table_names():
        return
    if "order_customer_identity_capability_state" not in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE order_customer_identity_capability_state (
                        capability_key VARCHAR PRIMARY KEY,
                        state VARCHAR NOT NULL CHECK (state IN ('expand', 'validated')),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        validation_revision VARCHAR
                    )
                    """
                )
            )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO order_customer_identity_capability_state
                    (capability_key, state, validation_revision, updated_at)
                VALUES ('order_customer_identity', 'expand', NULL, now())
                ON CONFLICT (capability_key) DO NOTHING
                """
            )
        )


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(postgres_engine: Engine) -> Generator[Session, None, None]:
    connection: Connection = postgres_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        yield session
    finally:
        try:
            session.rollback()
        except Exception:  # noqa: silent-ok — best-effort cleanup of ephemeral PostgreSQL test database
            pass
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def seed_tenant(session: Session, *, tenant_id: int, name: str | None = None) -> Tenant:
    label = name or f"A1 Test Tenant {tenant_id}"
    row = session.get(Tenant, tenant_id)
    if row is None:
        row = Tenant(id=int(tenant_id), name=label)
        session.add(row)
        session.flush()
    return row


def seed_integration(
    session: Session,
    *,
    tenant_id: int,
    external_store_id: Optional[str],
    config: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    integration_id: Optional[int] = None,
) -> Integration:
    cfg = dict(config or {})
    if external_store_id and "store_id" not in cfg:
        cfg["store_id"] = external_store_id
    if "api_key" not in cfg:
        cfg["api_key"] = "test-api-key"
    kwargs: Dict[str, Any] = dict(
        tenant_id=int(tenant_id),
        provider="salla",
        external_store_id=external_store_id,
        config=cfg,
        enabled=enabled,
    )
    if integration_id is not None:
        kwargs["id"] = int(integration_id)
    row = Integration(**kwargs)
    session.add(row)
    session.flush()
    return row


def seed_customer(
    session: Session,
    *,
    tenant_id: int,
    salla_customer_id: Optional[str] = None,
    name: str = "عميل تجريبي",
) -> Customer:
    row = Customer(
        tenant_id=int(tenant_id),
        name=name,
        salla_customer_id=salla_customer_id,
    )
    session.add(row)
    session.flush()
    return row


def seed_external_profile(
    session: Session,
    *,
    tenant_id: int,
    integration_connection_id: int,
    external_customer_ref: str,
) -> ExternalCustomerProfile:
    row = ExternalCustomerProfile(
        tenant_id=int(tenant_id),
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        integration_connection_id=int(integration_connection_id),
        external_customer_ref=str(external_customer_ref),
        profile_state="active",
        profile_source="test",
    )
    session.add(row)
    session.flush()
    return row


def seed_external_order(
    session: Session,
    *,
    tenant_id: int,
    external_id: str,
    integration_connection_id: Optional[int] = None,
    external_customer_ref: Optional[str] = None,
    external_customer_profile_id: Any = None,
    external_identity_link_state: Optional[str] = None,
    external_identity_evidence_class: Optional[str] = None,
    customer_id: Optional[int] = None,
    customer_link_state: str = LINK_STATE_UNLINKED,
    customer_link_evidence_class: Optional[str] = None,
) -> Order:
    row = Order(
        tenant_id=int(tenant_id),
        external_id=str(external_id),
        status="pending",
        total="100",
        source="salla",
        order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1 if external_customer_ref else None,
        integration_connection_id=integration_connection_id,
        external_customer_ref=external_customer_ref,
        external_customer_profile_id=external_customer_profile_id,
        external_identity_link_state=external_identity_link_state,
        external_identity_evidence_class=external_identity_evidence_class,
        customer_id=customer_id,
        customer_link_state=customer_link_state,
        customer_link_evidence_class=customer_link_evidence_class,
    )
    session.add(row)
    session.flush()
    return row


def seed_internal_order(
    session: Session,
    *,
    tenant_id: int,
    external_id: str,
    customer_id: int,
) -> Order:
    row = Order(
        tenant_id=int(tenant_id),
        external_id=str(external_id),
        status="pending",
        total="50",
        source="whatsapp",
        order_source_kind=ORDER_SOURCE_NAHL_INTERNAL,
        identity_namespace="nahla_internal_order_v1",
        customer_id=int(customer_id),
        customer_link_state=LINK_STATE_VERIFIED,
        customer_link_evidence_class=EVIDENCE_AUTHORITATIVE,
        customer_link_source="nahla_order_bridge_conversation_customer",
    )
    session.add(row)
    session.flush()
    return row


def seed_untrusted_order(session: Session, *, tenant_id: int, kind: str, external_id: str) -> Order:
    row = Order(
        tenant_id=int(tenant_id),
        external_id=str(external_id),
        status="pending",
        total="10",
        source=kind,
        order_source_kind=kind,
        customer_link_state=LINK_STATE_UNLINKED,
    )
    session.add(row)
    session.flush()
    return row


def seed_capability_state(
    session: Session,
    *,
    state: str,
    validation_revision: str | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO order_customer_identity_capability_state
                (capability_key, state, validation_revision, updated_at)
            VALUES ('order_customer_identity', :state, :validation_revision, now())
            ON CONFLICT (capability_key) DO UPDATE
            SET state = EXCLUDED.state,
                validation_revision = EXCLUDED.validation_revision,
                updated_at = now()
            """
        ),
        {"state": state, "validation_revision": validation_revision},
    )
    session.flush()


def clear_capability_state(session: Session) -> None:
    session.execute(
        text("DELETE FROM order_customer_identity_capability_state WHERE capability_key = 'order_customer_identity'")
    )
    session.flush()


__all__ = [
    "TEST_TENANT_A",
    "TEST_TENANT_B",
    "clear_capability_state",
    "pg_session",
    "postgres_engine",
    "seed_capability_state",
    "seed_customer",
    "seed_external_order",
    "seed_external_profile",
    "seed_integration",
    "seed_internal_order",
    "seed_tenant",
    "seed_untrusted_order",
]
