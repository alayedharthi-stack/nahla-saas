"""PostgreSQL ranking for GET /orders by actual created_at."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session, noload, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Order, Tenant
from routers.orders import (
    ORDERS_LIST_PAGE_SIZE,
    ORDERS_LIST_SORT_SQL_FAILED_EVENT,
    OrdersListSortSqlError,
    _load_orders_list_page,
)
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


def _ranked_page(db: Session, tenant_id: int):
    q = (
        db.query(Order)
        .options(noload("*"))
        .filter(Order.tenant_id == tenant_id)
    )
    return _load_orders_list_page(q)


def _cleanup(session: Session) -> None:
    session.query(Order).filter(Order.tenant_id == TEST_TENANT).delete(synchronize_session=False)
    session.query(Tenant).filter(Tenant.id == TEST_TENANT).delete(synchronize_session=False)
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
        .options(noload("*"))
        .filter(Order.tenant_id == tenant.id)
        .order_by(Order.id.desc())
        .limit(400)
        .all()
    )
    assert "oldest-pk-newest-created" not in {row.external_id for row in previous}

    rows = _ranked_page(pg_session, tenant.id)
    ids = [row.external_id for row in rows]
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

    rows = _ranked_page(pg_session, tenant.id)
    ids = [row.external_id for row in rows]
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

    rows = _ranked_page(pg_session, tenant.id)
    ids = [row.external_id for row in rows]
    assert ids[:2] == ["dated-order", "undated-order"]

    undated.status = "paid"
    meta = dict(undated.extra_metadata or {})
    meta["updated_at"] = "2026-08-28T23:00:00+00:00"
    meta["last_updated_at"] = meta["updated_at"]
    undated.extra_metadata = meta
    pg_session.commit()

    after = [row.external_id for row in _ranked_page(pg_session, tenant.id)]
    assert after[:2] == ["dated-order", "undated-order"]
    _cleanup(pg_session)

def test_postgres_feb30_created_at_uses_valid_draft_and_session_stays_alive(pg_session: Session) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    older = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    draft = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
    pg_session.add(
        Order(
            tenant_id=tenant.id,
            external_id="feb30-with-valid-draft",
            status="draft",
            source="whatsapp",
            extra_metadata={
                "created_at": "2026-02-30T12:00:00+00:00",
                "draft_created_at": draft.isoformat(),
                "updated_at": "2026-08-28T23:00:00+00:00",
            },
        )
    )
    pg_session.add(
        Order(
            tenant_id=tenant.id,
            external_id="plain-older",
            status="paid",
            source="salla",
            extra_metadata={"created_at": older.isoformat()},
        )
    )
    pg_session.commit()

    rows = _ranked_page(pg_session, tenant.id)
    assert [row.external_id for row in rows][:2] == [
        "feb30-with-valid-draft",
        "plain-older",
    ]
    assert pg_session.execute(text("SELECT 1")).scalar() == 1
    _cleanup(pg_session)


def test_postgres_naive_created_at_rank_stable_across_session_time_zone(pg_session: Session) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    pg_session.add_all(
        [
            Order(
                tenant_id=tenant.id,
                external_id="naive-utc-wall",
                status="paid",
                source="salla",
                extra_metadata={"created_at": "2026-08-28T10:00:00"},
            ),
            Order(
                tenant_id=tenant.id,
                external_id="offset-earlier-instant",
                status="paid",
                source="salla",
                extra_metadata={"created_at": "2026-08-28T12:00:00+03:00"},
            ),
        ]
    )
    pg_session.commit()

    ids_by_zone = {}
    for zone in ("UTC", "Asia/Riyadh"):
        if zone not in ("UTC", "Asia/Riyadh"):
            raise AssertionError("unexpected test time zone")
        pg_session.execute(text("SET TIME ZONE '" + zone + "'"))
        ids_by_zone[zone] = [row.external_id for row in _ranked_page(pg_session, tenant.id)]
    pg_session.execute(text("SET TIME ZONE 'UTC'"))

    assert ids_by_zone["UTC"][:2] == ["naive-utc-wall", "offset-earlier-instant"]
    assert ids_by_zone["UTC"] == ids_by_zone["Asia/Riyadh"]
    _cleanup(pg_session)


def test_postgres_sort_sql_failure_uses_savepoint_and_safe_log(
    pg_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _cleanup(pg_session)
    tenant = _seed_tenant(pg_session)
    pg_session.add(
        Order(
            tenant_id=tenant.id,
            external_id="survives-sql-failure",
            status="paid",
            source="salla",
            extra_metadata={"created_at": "2026-08-28T10:00:00+00:00"},
        )
    )
    pg_session.commit()

    calls = {"n": 0}
    sql_seen: list[str] = []

    def _failing_order(query):
        calls["n"] += 1
        # Missing relation plus CAST so PostgreSQL must fail at execute, not
        # by dropping a constant ORDER BY / WHERE at plan time.
        return query.filter(
            text(
                "(SELECT CAST('2026-02-30' AS date)"
                " FROM __nahla_orders_sort_canary_table__) IS NOT NULL"
            )
        )

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        sql_seen.append(str(statement))

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    orders_log = logging.getLogger("nahla.orders")
    previous_level = orders_log.level
    orders_log.addHandler(handler)
    orders_log.setLevel(logging.ERROR)
    caplog.set_level(logging.ERROR, logger="nahla.orders")
    bind = pg_session.get_bind()
    event.listen(bind, "before_cursor_execute", _before)
    try:
        with patch("routers.orders._apply_created_at_order", side_effect=_failing_order):
            rows = _ranked_page(pg_session, tenant.id)
    finally:
        event.remove(bind, "before_cursor_execute", _before)
        orders_log.removeHandler(handler)
        orders_log.setLevel(previous_level)

    assert calls["n"] == 1
    assert any(
        "__nahla_orders_sort_canary_table__" in stmt or "2026-02-30" in stmt
        for stmt in sql_seen
    ), sql_seen
    assert [row.external_id for row in rows] == ["survives-sql-failure"]
    assert pg_session.execute(text("SELECT 1")).scalar() == 1

    records = [
        rec
        for rec in captured
        if rec.getMessage() == ORDERS_LIST_SORT_SQL_FAILED_EVENT
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR
    assert rec.args in ((), None, {})
    assert rec.exc_info is None
    assert not rec.exc_text
    assert getattr(rec, "orders_list_event") == ORDERS_LIST_SORT_SQL_FAILED_EVENT
    assert getattr(rec, "orders_list_error_class") == OrdersListSortSqlError.__name__
    formatted = logging.Formatter("%(message)s").format(rec)
    assert formatted == ORDERS_LIST_SORT_SQL_FAILED_EVENT
    blob = formatted + str(rec.args) + str(rec.exc_info)
    assert "2026-02-30" not in blob
    assert "Traceback" not in blob
    assert "CAST" not in blob
    _cleanup(pg_session)
