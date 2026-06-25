"""Regression: /orders list must not 500 when sorting by last_updated_at alias."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
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

    def test_orders_list_orders_by_updated_at_but_displays_created_at(self) -> None:
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
        assert payload["orders"][0]["external_id"] == "sort-fresh"
        assert payload["orders"][0]["display_created_at"].startswith("2024-03-01")
        assert payload["orders"][0]["createdAt"].startswith("2024-03-01")
        assert payload["orders"][0]["last_updated_at"].startswith(
            fresh_touch.date().isoformat()
        )

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
