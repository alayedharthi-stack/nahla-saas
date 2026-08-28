"""Regression: /orders list must not 500 when sorting by last_updated_at alias."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.wa_order_lifecycle import STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO  # noqa: E402
from models import Base, Order, Tenant  # noqa: E402
from routers.orders import (  # noqa: E402
    ORDERS_LIST_PAGE_SIZE,
    ORDERS_LIST_SORT_SQL_FAILED_EVENT,
    OrdersListSortSqlError,
    _apply_created_at_order,
    _load_orders_list_page,
    _read_created_at,
    _read_last_updated_at,
    _serialise_order,
    list_orders,
)


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _wa_draft(
    *,
    tenant_id: int,
    external_id: str,
    created_at: datetime,
    last_updated_at: datetime,
    status: str = STATUS_DRAFT,
) -> Order:
    return Order(
        tenant_id=tenant_id,
        external_id=external_id,
        status=status,
        source="whatsapp",
        total="100.00 SAR",
        extra_metadata={
            "created_at": created_at.isoformat(),
            "draft_created_at": created_at.isoformat(),
            "last_updated_at": last_updated_at.isoformat(),
            "updated_at": last_updated_at.isoformat(),
            "lifecycle": "whatsapp_draft",
        },
    )


def _invoke_list(db, tenant_id: int, **kwargs: Any) -> dict:
    request = MagicMock()

    async def _go() -> dict:
        return await list_orders(
            request,
            db,
            lifecycle_filter=kwargs.get("lifecycle_filter"),
            source=kwargs.get("source"),
        )

    with patch("routers.orders.resolve_tenant_id", return_value=tenant_id):
        with patch("routers.orders.get_or_create_tenant"):
            return asyncio.run(_go())


class TestOrdersListEmptyRegression:
    def test_orders_list_returns_existing_orders_after_date_fields(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        order = _wa_draft(
            tenant_id=tenant.id,
            external_id="list-regression-1",
            created_at=now - timedelta(days=3),
            last_updated_at=now,
        )
        db.add(order)
        db.commit()

        payload = _invoke_list(db, tenant.id)
        assert payload["summary"]["total_orders"] == 1
        assert len(payload["orders"]) == 1
        assert payload["orders"][0]["external_id"] == "list-regression-1"

    def test_orders_list_orders_by_created_at_not_updated_at(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        created = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
        stale_touch = now - timedelta(hours=5)
        fresh_touch = now

        db.add_all(
            [
                _wa_draft(
                    tenant_id=tenant.id,
                    external_id="sort-stale",
                    created_at=created,
                    last_updated_at=stale_touch,
                ),
                _wa_draft(
                    tenant_id=tenant.id,
                    external_id="sort-fresh",
                    created_at=created,
                    last_updated_at=fresh_touch,
                ),
            ]
        )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        assert len(payload["orders"]) == 2
        # Same created_at: stable secondary is higher id first (sort-fresh inserted last).
        assert payload["orders"][0]["external_id"] == "sort-fresh"
        assert payload["orders"][0]["display_created_at"].startswith("2024-03-01")
        assert payload["orders"][0]["createdAt"].startswith("2024-03-01")
        assert payload["orders"][0]["last_updated_at"].startswith(
            fresh_touch.date().isoformat()
        )


class TestOrdersListNewestCreatedFirst:
    def test_newest_created_first_regardless_of_insert_order(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        older = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="inserted-first-but-newer",
                created_at=newer,
                last_updated_at=older,
            )
        )
        db.commit()
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="inserted-second-but-older",
                created_at=older,
                last_updated_at=newer,
            )
        )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        ids = [row["external_id"] for row in payload["orders"]]
        assert ids[:2] == ["inserted-first-but-newer", "inserted-second-but-older"]

    def test_refresh_places_newer_order_first_without_duplicate(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        t0 = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="existing",
                created_at=t0,
                last_updated_at=t0,
            )
        )
        db.commit()
        first = _invoke_list(db, tenant.id)
        assert [row["external_id"] for row in first["orders"]] == ["existing"]

        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="arrived-later",
                created_at=t1,
                last_updated_at=t1,
            )
        )
        db.commit()
        second = _invoke_list(db, tenant.id)
        ids = [row["external_id"] for row in second["orders"]]
        assert ids == ["arrived-later", "existing"]

    def test_equal_created_at_uses_stable_id_desc(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        created = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="tie-a",
                created_at=created,
                last_updated_at=created,
            )
        )
        db.commit()
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="tie-b",
                created_at=created,
                last_updated_at=created,
            )
        )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        ids = [row["external_id"] for row in payload["orders"]]
        assert ids == ["tie-b", "tie-a"]

    def _seed_oldest_pk_newest_created(self, db, tenant_id: int, older_count: int = 600):
        newest = datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)
        older = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant_id,
                external_id="oldest-pk-newest-created",
                created_at=newest,
                last_updated_at=older,
            )
        )
        db.commit()
        batch = [
            _wa_draft(
                tenant_id=tenant_id,
                external_id=f"older-created-{idx:04d}",
                created_at=older,
                last_updated_at=older,
            )
            for idx in range(older_count)
        ]
        db.add_all(batch)
        db.commit()
        return older_count + 1

    def test_id_limit_400_before_sort_hides_oldest_pk_newest_created(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        total = self._seed_oldest_pk_newest_created(db, tenant.id, older_count=600)
        assert total == 601

        previous = (
            db.query(Order)
            .filter(Order.tenant_id == tenant.id)
            .order_by(Order.id.desc())
            .limit(400)
            .all()
        )
        previous_ids = {row.external_id for row in previous}
        assert "oldest-pk-newest-created" not in previous_ids
        assert len(previous) == 400

        payload = _invoke_list(db, tenant.id)
        ids = [row["external_id"] for row in payload["orders"]]
        assert ids[0] == "oldest-pk-newest-created"
        assert len(ids) == ORDERS_LIST_PAGE_SIZE

    def test_missing_created_at_not_promoted_by_status_or_updated_at(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        dated = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        touched = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="dated-order",
                created_at=dated,
                last_updated_at=dated,
            )
        )
        undated = Order(
            tenant_id=tenant.id,
            external_id="undated-order",
            status="pending_payment",
            source="whatsapp",
            extra_metadata={
                "created_at": "not-a-date",
                "updated_at": dated.isoformat(),
                "last_updated_at": dated.isoformat(),
            },
        )
        db.add(undated)
        db.commit()

        before = [row["external_id"] for row in _invoke_list(db, tenant.id)["orders"]]
        assert before[:2] == ["dated-order", "undated-order"]

        undated.status = "paid"
        meta = dict(undated.extra_metadata or {})
        meta["updated_at"] = touched.isoformat()
        meta["last_updated_at"] = touched.isoformat()
        meta["status_changed_at"] = touched.isoformat()
        undated.extra_metadata = meta
        db.commit()

        after = [row["external_id"] for row in _invoke_list(db, tenant.id)["orders"]]
        assert after[:2] == ["dated-order", "undated-order"]

    def test_invalid_calendar_created_at_uses_valid_draft(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        older = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        draft = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
        db.add(
            Order(
                tenant_id=tenant.id,
                external_id="feb30-with-valid-draft",
                status="draft",
                source="whatsapp",
                extra_metadata={
                    "created_at": "2026-02-30T12:00:00+00:00",
                    "draft_created_at": draft.isoformat(),
                    "updated_at": datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc).isoformat(),
                },
            )
        )
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="plain-older",
                created_at=older,
                last_updated_at=older,
            )
        )
        db.commit()
        payload = _invoke_list(db, tenant.id)
        assert [row["external_id"] for row in payload["orders"]][:2] == [
            "feb30-with-valid-draft",
            "plain-older",
        ]

    def test_sort_sql_failure_uses_savepoint_and_safe_log(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="survives-sql-failure",
                created_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
                last_updated_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            )
        )
        db.commit()

        calls = {"n": 0}

        def _failing_order(query):
            calls["n"] += 1
            return query.filter(text("(SELECT 1 FROM __nahla_orders_sort_canary_table__) = 1"))

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        orders_log = logging.getLogger("nahla.orders")
        previous_level = orders_log.level
        orders_log.addHandler(handler)
        orders_log.setLevel(logging.ERROR)
        try:
            with patch("routers.orders._apply_created_at_order", side_effect=_failing_order):
                q = db.query(Order).filter(Order.tenant_id == tenant.id)
                rows = _load_orders_list_page(q)
        finally:
            orders_log.removeHandler(handler)
            orders_log.setLevel(previous_level)

        assert calls["n"] == 1
        assert [row.external_id for row in rows] == ["survives-sql-failure"]
        assert db.execute(text("SELECT 1")).scalar() == 1

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
        assert getattr(rec, "event") == ORDERS_LIST_SORT_SQL_FAILED_EVENT
        assert getattr(rec, "error_class") == OrdersListSortSqlError.__name__
        formatted = logging.Formatter("%(message)s").format(rec)
        assert formatted == ORDERS_LIST_SORT_SQL_FAILED_EVENT
        blob = formatted + str(rec.args) + str(rec.exc_info)
        assert "Traceback" not in blob
        assert "__nahla_orders_sort_canary_table__" not in blob
        assert "CAST" not in blob

    def test_list_orders_sql_is_limited_after_created_at_rank(self) -> None:
        db, engine = _make_db()
        tenant = _seed_tenant(db)
        self._seed_oldest_pk_newest_created(db, tenant.id, older_count=20)
        captured: list[str] = []

        def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            captured.append(str(statement))

        event.listen(engine, "before_cursor_execute", _before)
        try:
            payload = _invoke_list(db, tenant.id)
        finally:
            event.remove(engine, "before_cursor_execute", _before)

        assert payload["orders"][0]["external_id"] == "oldest-pk-newest-created"
        order_selects = [
            stmt for stmt in captured
            if "from orders" in stmt.lower() and "select" in stmt.lower()
        ]
        assert order_selects, captured
        assert any("limit" in stmt.lower() for stmt in order_selects)

        q = db.query(Order).filter(Order.tenant_id == tenant.id)
        compiled = str(
            _apply_created_at_order(q).limit(ORDERS_LIST_PAGE_SIZE).statement.compile(
                dialect=engine.dialect
            )
        )
        assert "LIMIT" in compiled.upper()

    def test_status_update_does_not_promote_old_order(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        old_created = datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc)
        new_created = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
        old = _wa_draft(
            tenant_id=tenant.id,
            external_id="old-order",
            created_at=old_created,
            last_updated_at=old_created,
            status="pending_payment",
        )
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="new-order",
                created_at=new_created,
                last_updated_at=new_created,
            )
        )
        db.add(old)
        db.commit()

        before = [row["external_id"] for row in _invoke_list(db, tenant.id)["orders"]]
        assert before[:2] == ["new-order", "old-order"]

        old.status = "paid"
        meta = dict(old.extra_metadata or {})
        touched = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc)
        meta["last_updated_at"] = touched.isoformat()
        meta["updated_at"] = touched.isoformat()
        meta["status_changed_at"] = touched.isoformat()
        old.extra_metadata = meta
        db.commit()

        after = [row["external_id"] for row in _invoke_list(db, tenant.id)["orders"]]
        assert after[:2] == ["new-order", "old-order"]

    def test_filters_and_tenant_isolation_preserved(self) -> None:
        db, _ = _make_db()
        tenant_a = _seed_tenant(db)
        tenant_b = Tenant(name="T-B", is_active=True)
        db.add(tenant_b)
        db.commit()
        db.refresh(tenant_b)
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        db.add_all(
            [
                _wa_draft(
                    tenant_id=tenant_a.id,
                    external_id="a-wa",
                    created_at=now,
                    last_updated_at=now,
                ),
                Order(
                    tenant_id=tenant_a.id,
                    external_id="a-salla",
                    status="paid",
                    source="salla",
                    total="50.00 SAR",
                    extra_metadata={
                        "created_at": (now - timedelta(hours=1)).isoformat(),
                    },
                ),
                _wa_draft(
                    tenant_id=tenant_b.id,
                    external_id="b-wa",
                    created_at=now + timedelta(hours=2),
                    last_updated_at=now,
                ),
            ]
        )
        db.commit()

        all_a = _invoke_list(db, tenant_a.id)
        ids_a = {row["external_id"] for row in all_a["orders"]}
        assert ids_a == {"a-wa", "a-salla"}
        assert all_a["orders"][0]["external_id"] == "a-wa"

        wa_only = _invoke_list(db, tenant_a.id, source="whatsapp")
        assert [row["external_id"] for row in wa_only["orders"]] == ["a-wa"]

        paid = _invoke_list(db, tenant_a.id, lifecycle_filter="paid")
        assert [row["external_id"] for row in paid["orders"]] == ["a-salla"]

    def test_orders_list_includes_whatsapp_drafts(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        for idx, status in enumerate((STATUS_DRAFT, STATUS_PENDING_CUSTOMER_INFO)):
            db.add(
                _wa_draft(
                    tenant_id=tenant.id,
                    external_id=f"draft-{idx}",
                    created_at=now - timedelta(days=1),
                    last_updated_at=now,
                    status=status,
                )
            )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        assert payload["summary"]["total_orders"] == 2
        ids = {o["external_id"] for o in payload["orders"]}
        assert "draft-0" in ids
        assert "draft-1" in ids

    def test_orders_counts_include_existing_whatsapp_orders(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        today = datetime.now(timezone.utc)
        db.add(
            _wa_draft(
                tenant_id=tenant.id,
                external_id="count-wa-today",
                created_at=today,
                last_updated_at=today,
            )
        )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        assert payload["summary"]["total_orders"] == 1
        assert payload["summary"]["whatsapp_orders_today"] >= 1
        assert payload["summary"]["today_revenue_sar"] > 0

    def test_orders_frontend_mapping_keeps_orders_with_optional_date_fields(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        order = Order(
            tenant_id=tenant.id,
            external_id="minimal-dates",
            status=STATUS_DRAFT,
            source="whatsapp",
            extra_metadata={"created_at": now.isoformat()},
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        item = _serialise_order(order, customer_lookup={}, now=now)
        assert item["createdAt"]
        assert "display_created_at" in item
        assert "last_updated_at" in item

        payload = _invoke_list(db, tenant.id)
        assert len(payload["orders"]) == 1
        row = payload["orders"][0]
        assert row["createdAt"]
        assert row.get("display_created_at") or row["createdAt"]
