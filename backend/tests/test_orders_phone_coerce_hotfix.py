"""Regression: GET /orders must survive int phone/mobile in customer_info (Salla legacy)."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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

from core.phone_coerce import coerce_customer_info_phone, coerce_phone_str  # noqa: E402
from core.salla_order_fidelity import enrich_salla_customer_info  # noqa: E402
from models import Base, Order, Tenant  # noqa: E402
from routers.orders import _serialise_order, list_orders  # noqa: E402

_INT_PHONE = 966541690226


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


def _invoke_list(db, tenant_id: int) -> dict:
    request = MagicMock()

    async def _go() -> dict:
        return await list_orders(
            request,
            db,
            lifecycle_filter=None,
            source=None,
        )

    with patch("routers.orders.resolve_tenant_id", return_value=tenant_id):
        with patch("routers.orders.get_or_create_tenant"):
            return asyncio.run(_go())


def _salla_order(**overrides) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    base = {
        "id": 1,
        "tenant_id": 1,
        "external_id": "salla-1",
        "external_order_number": "1001",
        "status": "paid",
        "total": "174.00 SAR",
        "customer_name": "أحمد سالم",
        "customer_info": {"phone": _INT_PHONE},
        "line_items": [{"title": "قميص قطني أزرق", "quantity": 1}],
        "checkout_url": None,
        "source": "salla",
        "is_abandoned": False,
        "extra_metadata": {"created_at": now.isoformat(), "source": "salla"},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCoercePhoneStr:
    def test_none(self) -> None:
        assert coerce_phone_str(None) == ""

    def test_string(self) -> None:
        assert coerce_phone_str(" 966541690226 ") == "966541690226"

    def test_int(self) -> None:
        assert coerce_phone_str(_INT_PHONE) == "966541690226"

    def test_float_whole(self) -> None:
        assert coerce_phone_str(float(_INT_PHONE)) == "966541690226"

    def test_bool_false(self) -> None:
        assert coerce_phone_str(False) == ""

    def test_customer_info_phone_int(self) -> None:
        assert coerce_customer_info_phone({"phone": _INT_PHONE}) == "966541690226"

    def test_customer_info_mobile_int(self) -> None:
        assert coerce_customer_info_phone({"mobile": _INT_PHONE}) == "966541690226"

    def test_customer_info_phone_none_mobile_int(self) -> None:
        assert coerce_customer_info_phone({"phone": None, "mobile": _INT_PHONE}) == "966541690226"

    def test_customer_info_missing(self) -> None:
        assert coerce_customer_info_phone({}) == ""
        assert coerce_customer_info_phone(None) == ""


class TestSerialiseOrderIntPhone:
    def test_phone_as_int(self) -> None:
        order = _salla_order(customer_info={"phone": _INT_PHONE})
        row = _serialise_order(order, customer_lookup={}, now=datetime.now(timezone.utc))
        assert row["phone"] == "966541690226"

    def test_mobile_as_int(self) -> None:
        order = _salla_order(customer_info={"mobile": _INT_PHONE})
        row = _serialise_order(order, customer_lookup={}, now=datetime.now(timezone.utc))
        assert row["phone"] == "966541690226"


class TestListOrdersIntPhoneRegression:
    def test_list_orders_200_when_one_order_has_int_phone(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        db.add(
            Order(
                tenant_id=tenant.id,
                external_id="good-order",
                status="paid",
                source="whatsapp",
                total="100.00 SAR",
                customer_info={"phone": "966551308005"},
                line_items=[{"title": "حذاء رياضي أبيض", "quantity": 1}],
                extra_metadata={"created_at": now.isoformat()},
            )
        )
        db.add(
            Order(
                tenant_id=tenant.id,
                external_id="int-phone-order",
                status="paid",
                source="salla",
                total="174.00 SAR",
                customer_info={"phone": _INT_PHONE},
                line_items=[{"title": "عطر ورد 100ml", "quantity": 1}],
                extra_metadata={"created_at": now.isoformat(), "source": "salla"},
            )
        )
        db.commit()

        payload = _invoke_list(db, tenant.id)
        assert payload["summary"]["total_orders"] == 2
        phones = {o["external_id"]: o["phone"] for o in payload["orders"]}
        assert phones["int-phone-order"] == "966541690226"


class TestEnrichSallaCustomerInfo:
    def test_stores_int_mobile_as_str(self) -> None:
        raw = {"customer": {"mobile": _INT_PHONE, "name": "نورة عبدالله"}}
        out = enrich_salla_customer_info(raw, {})
        assert out["phone"] == "966541690226"
        assert out["mobile"] == "966541690226"
        assert isinstance(out["phone"], str)
        assert isinstance(out["mobile"], str)

    def test_receiver_phone_int(self) -> None:
        raw = {"receiver": {"phone": _INT_PHONE}}
        out = enrich_salla_customer_info(raw, {})
        assert out["phone"] == "966541690226"
        assert isinstance(out["phone"], str)
