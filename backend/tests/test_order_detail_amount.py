"""Order detail amount — line_items sum vs persisted Order.total."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Order, Tenant  # noqa: E402
from routers.orders import _serialise_order  # noqa: E402
from services.nahla_order_bridge import (  # noqa: E402
    _resolve_order_amount,
    sync_nahla_wa_order,
)
from sqlalchemy import JSON, create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

_DB_KW = {"db": MagicMock(), "tenant_id": 33, "conversation_id": 9063}


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


def _wa_order(
    *,
    total: str,
    line_items: list,
    tenant_id: int = 1,
) -> Order:
    return Order(
        tenant_id=tenant_id,
        external_id="nahla-wa-1-60",
        external_order_number="NHL-1-000060",
        status="pending_customer_info",
        source="whatsapp",
        total=total,
        customer_name="Customer",
        customer_info={"phone": "+966500000060"},
        line_items=line_items,
        extra_metadata={"source_kind": "nahla_order"},
    )


def _enable_draft_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", "33")


def _conv(**kwargs):
    defaults = {
        "id": 9063,
        "tenant_id": 33,
        "customer_id": 1,
        "customer": SimpleNamespace(
            id=1,
            tenant_id=33,
            phone="966551308005",
            name="Customer",
            extra_metadata={},
        ),
        "extra_metadata": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _draft_prep(**extra):
    base = {
        "product_id": "prod-99",
        "quantity": 1,
        "customer_first_name": "Ahmad",
        "city": "Riyadh",
        "payment_receipt_received": False,
        "awaiting_payment_receipt": False,
        "order_status": "",
    }
    base.update(extra)
    return base


def _brain(**extra):
    base = {
        "stage": "ordering",
        "current_product_focus": {"title": "Product B", "price": "239.5", "id": 2},
        "checkout_url": "",
    }
    base.update(extra)
    return base


def test_order_detail_amount_uses_sum_of_multiple_line_items() -> None:
    db, _ = _make_db()
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    order = _wa_order(
        tenant_id=tenant.id,
        total="239.50 ر.س",
        line_items=[
            {"product_name": "Product A", "quantity": 1, "unit_price": 126.0},
            {"product_name": "Product B", "quantity": 1, "unit_price": 239.5},
        ],
    )
    db.add(order)
    db.commit()

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant.id,
    )

    assert payload["amount_sar"] == pytest.approx(365.50)
    assert payload["persisted_amount_sar"] == pytest.approx(239.50)
    assert payload["persisted_amount_stale"] is True


def test_order_detail_amount_divergence_when_persisted_amount_stale() -> None:
    db, _ = _make_db()
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    order = _wa_order(
        tenant_id=tenant.id,
        total="239.50",
        line_items=[
            {"product_name": "A", "quantity": 1, "unit_price": 126.0},
            {"product_name": "B", "quantity": 1, "unit_price": 239.5},
        ],
    )

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant.id,
    )

    assert payload["amount_sar"] == pytest.approx(365.50)
    assert payload["persisted_amount_stale"] is True
    assert payload["persisted_amount_sar"] == pytest.approx(239.50)


def test_single_item_order_amount_unchanged() -> None:
    db, _ = _make_db()
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    order = _wa_order(
        tenant_id=tenant.id,
        total="126.00 ر.س",
        line_items=[{"product_name": "Product A", "quantity": 1, "unit_price": 126.0}],
    )

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=False,
        db=db,
        tenant_id=tenant.id,
    )

    assert payload["amount_sar"] == pytest.approx(126.0)
    assert payload["persisted_amount_stale"] is False


def test_salla_order_keeps_persisted_total_without_line_item_prices() -> None:
    order = Order(
        tenant_id=1,
        external_id="salla-123",
        status="paid",
        source="salla",
        total="500.00 ر.س",
        line_items=[{"product_name": "Store item", "quantity": 1}],
    )

    payload = _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
    )

    assert payload["amount_sar"] == pytest.approx(500.0)
    assert payload["persisted_amount_stale"] is False


def test_resolve_amount_prefers_line_items_sum_for_multi_item_cart() -> None:
    line_items = [
        {"product_name": "A", "quantity": 1, "unit_price": 126.0},
        {"product_name": "B", "quantity": 1, "unit_price": 239.5},
    ]
    amt, needs_review, source = _resolve_order_amount(
        order_prep={"price": "239.5", "total_price": None},
        brain_state={"current_product_focus": {"price": "239.5"}},
        receipt_metadata={},
        line_items=line_items,
        is_paid_path=False,
        **_DB_KW,
    )
    assert amt == pytest.approx(365.50)
    assert needs_review is False
    assert source == "line_items"


def test_catalog_order_bridge_persists_total_sum_not_last_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    class _Order:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.id = 60

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", _Order)
    monkeypatch.setattr(
        "services.nahla_order_bridge._allocate_nhl_number",
        lambda _db, _tid: "NHL-33-000060",
    )

    line_items = [
        {"product_name": "Product A", "quantity": 1, "unit_price": 126.0},
        {"product_name": "Product B", "quantity": 1, "unit_price": 239.5},
    ]

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(
            price="239.5",
            line_items=line_items,
            product_name="Product B",
        ),
        trigger="brain_save",
    )

    assert result is not None
    assert result.total == "365.50 ر.س"
    assert len(result.line_items) == 2
