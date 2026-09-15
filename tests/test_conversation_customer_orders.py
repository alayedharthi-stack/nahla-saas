"""Read-only customer-order drawer endpoint isolation coverage."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from models import Base, Customer, Order, Tenant  # noqa: E402
from routers import conversations as conversations_router  # noqa: E402


class _Request:
    headers: dict = {}
    cookies: dict = {}
    state = type("State", (), {})()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)(), engine


def _seed_order(db, *, tenant_id: int, customer: Customer, reference: str, total: str):
    order = Order(
        tenant_id=tenant_id,
        customer_id=customer.id,
        customer_link_state="linked",
        external_id=reference,
        external_order_number=reference,
        status="processing",
        total=total,
        customer_name=customer.name,
        customer_info={"phone": customer.phone},
        line_items=[{"name": "Test product", "quantity": 2, "price": "25"}],
        source="manual",
        is_abandoned=False,
        extra_metadata={"created_at": "2026-09-10T10:30:00+00:00"},
    )
    db.add(order)
    db.commit()
    return order


def test_customer_orders_are_tenant_and_customer_isolated_and_read_only():
    db, engine = _make_db()
    try:
        tenant_one = Tenant(id=201, name="merchant-one")
        tenant_two = Tenant(id=202, name="merchant-two")
        db.add_all([tenant_one, tenant_two])
        db.commit()
        first = Customer(
            tenant_id=201,
            name="First customer",
            phone="+966500001111",
            normalized_phone="+966500001111",
        )
        second = Customer(
            tenant_id=202,
            name="Second tenant customer",
            phone="+966500001111",
            normalized_phone="+966500001111",
        )
        unrelated = Customer(
            tenant_id=201,
            name="Unrelated customer",
            phone="+966500002222",
            normalized_phone="+966500002222",
        )
        db.add_all([first, second, unrelated])
        db.commit()
        _seed_order(db, tenant_id=201, customer=first, reference="OWN-1", total="50")
        _seed_order(db, tenant_id=202, customer=second, reference="OTHER-TENANT", total="900")
        _seed_order(db, tenant_id=201, customer=unrelated, reference="OTHER-CUSTOMER", total="80")

        original = conversations_router.resolve_tenant_id
        conversations_router.resolve_tenant_id = lambda _request: 201  # type: ignore[assignment]
        try:
            result = asyncio.run(conversations_router.get_conversation_customer_orders(
                customer_phone=first.phone,
                customer_id=first.id,
                request=_Request(),
                db=db,
                limit=10,
            ))
        finally:
            conversations_router.resolve_tenant_id = original

        assert result["readOnly"] is True
        assert result["count"] == 1
        assert [row["reference"] for row in result["orders"]] == ["#OWN-1"]
        assert result["orders"][0]["itemCount"] == 2
        assert "OTHER-TENANT" not in str(result)
        assert "OTHER-CUSTOMER" not in str(result)

        conversations_router.resolve_tenant_id = lambda _request: 201  # type: ignore[assignment]
        try:
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(conversations_router.get_conversation_customer_orders(
                    customer_phone=first.phone,
                    customer_id=unrelated.id,
                    request=_Request(),
                    db=db,
                    limit=10,
                ))
            assert getattr(exc_info.value, "status_code", None) == 404
        finally:
            conversations_router.resolve_tenant_id = original
    finally:
        db.close()
        engine.dispose()
