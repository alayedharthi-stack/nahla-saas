"""PostgreSQL fixtures for product_sale_offer integration tests.

Uses one dedicated connection per test (NullPool + bound Session) so the TEMP
``products`` table is visible to both inserts and repository SQL execution.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Generator, Iterator, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_TEST_TENANT_A = 880_001
_TEST_TENANT_B = 880_002

# Mirrors repository SQL predicates — all columns referenced in the CTE must exist.
_TEMP_PRODUCTS_DDL = """
CREATE TEMP TABLE products (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    metadata JSONB,
    merchant_hidden_at TIMESTAMPTZ,
    catalog_status VARCHAR(32) NOT NULL DEFAULT 'active',
    in_stock BOOLEAN NOT NULL DEFAULT TRUE
) ON COMMIT DROP
"""


def _candidate_database_urls() -> list[str]:
    urls: list[str] = []
    explicit = (os.getenv("PRODUCT_SALE_OFFER_TEST_DATABASE_URL") or "").strip()
    if explicit:
        urls.append(explicit)
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url and db_url not in urls:
        urls.append(db_url)
    default = "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas"
    if default not in urls:
        urls.append(default)
    return urls


def _integration_url_configured() -> bool:
    return bool((os.getenv("PRODUCT_SALE_OFFER_TEST_DATABASE_URL") or "").strip())


def _connect_engine() -> Engine:
    last_error: Optional[Exception] = None
    for url in _candidate_database_urls():
        try:
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = f"PostgreSQL unavailable for integration tests: {last_error}"
    if _integration_url_configured():
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(postgres_engine: Engine) -> Generator[Session, None, None]:
    connection: Connection = postgres_engine.connect()
    transaction = connection.begin()
    connection.execute(text(_TEMP_PRODUCTS_DDL))
    # Prove TEMP table is on this exact connection before any repository call.
    connection.execute(text("SELECT 1 FROM products LIMIT 0"))
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    # Repository + inserts must share this connection (TEMP table scope).
    session.execute(text("SELECT COUNT(*) FROM products"))
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def insert_catalog_product(
    session: Session,
    *,
    tenant_id: int,
    title: str,
    metadata: Dict[str, Any],
    catalog_status: str = "active",
    in_stock: bool = True,
    merchant_hidden_at: Any = None,
) -> int:
    row = session.execute(
        text(
            """
            INSERT INTO products (
                tenant_id, title, metadata, catalog_status, in_stock, merchant_hidden_at
            )
            VALUES (
                :tenant_id, :title, CAST(:metadata AS jsonb),
                :catalog_status, :in_stock, :merchant_hidden_at
            )
            RETURNING id
            """
        ),
        {
            "tenant_id": int(tenant_id),
            "title": title,
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "catalog_status": catalog_status,
            "in_stock": bool(in_stock),
            "merchant_hidden_at": merchant_hidden_at,
        },
    )
    pid = int(row.scalar_one())
    session.flush()
    return pid


__all__ = [
    "_TEST_TENANT_A",
    "_TEST_TENANT_B",
    "insert_catalog_product",
    "pg_session",
    "postgres_engine",
]
