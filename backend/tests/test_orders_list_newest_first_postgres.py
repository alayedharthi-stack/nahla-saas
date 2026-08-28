"""PostgreSQL ranking for GET /orders by actual created_at."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Order, Tenant
from routers.orders import ORDERS_LIST_PAGE_SIZE, list_orders
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT = 991_889

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(postgres_engine) -> Session:
    SessionLocal = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _invoke_list(db: Session, tenant_id: int) -> dict:
    request = MagicMock()

    async def _go() -> dict:
        return await list_orders(request, db, lifecycle_filter=None, source=None)

    with patch("routers.orders.resolve_tenant_id", return_value=tenant_id):
        with patch("routers.orders.get_or_create_tenant"):
            return asyncio.run(_go())


def _cleanup(session: Session) -> None:
    session.query(Order).filter(Order.tenant_id == TEST_TENANT).delete()
    tenant = session.get(Tenant, TEST_TENANT)
    if tenant is not None:
        session.delete(tenant)
    session.commit()


def _seed_tenant(session: Session) -> Tenant:
    row = session.get(Tenant, TEST_TENANT)
    if row is None:
        row = Tenant(id=TEST_TENANT, name="Orders list PG tenant")
        session.add(row)
        session.flush()
    return row


def test_postgres_oldest_pk_newest_created_survives_601_rows(pg_session: Session) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    newest = datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)
    older = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    pg_session.add(
        Order(
            tenant_id=tenant.id,
            external_id="oldest-pk-newest-created",
            status="draft",
            source="whatsapp",
            extra_metadata={"created_at": newest.isoformat()},
        )
    )
    pg_session.flush()
    pg_session.add_all(
        [
            Order(
                tenant_id=tenant.id,
                external_id=f"older-created-{idx:04d}",
                status="draft",
                source="salla",
                extra_metadata={"created_at": older.isoformat()},
            )
            for idx in range(600)
        ]
    )
    pg_session.commit()

    previous = (
        pg_session.query(Order)
        .filter(Order.tenant_id == tenant.id)
        .order_by(Order.id.desc())
        .limit(400)
        .all()
    )
    assert "oldest-pk-newest-created" not in {row.external_id for row in previous}

    payload = _invoke_list(pg_session, tenant.id)
    ids = [row["external_id"] for row in payload["orders"]]
    assert ids[0] == "oldest-pk-newest-created"
    assert len(ids) == ORDERS_LIST_PAGE_SIZE
    _cleanup(pg_session)


def test_postgres_compares_timestamptz_not_offset_text(pg_session: Session) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    # 09:00 UTC written as +03:00 would win a naive text DESC sort over 10:00 UTC.
    pg_session.add_all(
        [
            Order(
                tenant_id=tenant.id,
                external_id="offset-later-text",
                status="paid",
                source="salla",
                extra_metadata={"created_at": "2026-08-28T12:00:00+03:00"},
            ),
            Order(
                tenant_id=tenant.id,
                external_id="utc-actually-newer",
                status="paid",
                source="salla",
                extra_metadata={"created_at": "2026-08-28T10:00:00+00:00"},
            ),
        ]
    )
    pg_session.commit()

    payload = _invoke_list(pg_session, tenant.id)
    ids = [row["external_id"] for row in payload["orders"]]
    assert ids[:2] == ["utc-actually-newer", "offset-later-text"]
    _cleanup(pg_session)


def test_postgres_invalid_created_at_does_not_fail_or_outrank(pg_session: Session) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    dated = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    pg_session.add(
        Order(
            tenant_id=tenant.id,
            external_id="dated-order",
            status="paid",
            source="salla",
            extra_metadata={"created_at": dated.isoformat()},
        )
    )
    undated = Order(
        tenant_id=tenant.id,
        external_id="undated-order",
        status="pending_payment",
        source="whatsapp",
        extra_metadata={
            "created_at": "not-a-date",
            "updated_at": "2026-08-28T22:00:00+00:00",
        },
    )
    pg_session.add(undated)
    pg_session.commit()

    payload = _invoke_list(pg_session, tenant.id)
    ids = [row["external_id"] for row in payload["orders"]]
    assert ids[:2] == ["dated-order", "undated-order"]

    undated.status = "paid"
    meta = dict(undated.extra_metadata or {})
    meta["updated_at"] = "2026-08-28T23:00:00+00:00"
    meta["last_updated_at"] = meta["updated_at"]
    undated.extra_metadata = meta
    pg_session.commit()

    after = [row["external_id"] for row in _invoke_list(pg_session, tenant.id)["orders"]]
    assert after[:2] == ["dated-order", "undated-order"]
    _cleanup(pg_session)
