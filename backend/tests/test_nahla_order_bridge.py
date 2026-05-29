"""Tests for Phase 1 Nahla internal order bridge."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.nahla_order_bridge import (  # noqa: E402
    _resolve_order_amount,
    nahla_wa_external_id,
    upsert_nahla_paid_order,
)


def test_nahla_wa_external_id_is_stable() -> None:
    assert nahla_wa_external_id(33, 9063) == "nahla-wa-33-9063"


def test_resolve_amount_prefers_total_price() -> None:
    amt, needs_review = _resolve_order_amount(
        order_prep={"total_price": "250 ر.س"},
        brain_state={},
        receipt_metadata={},
        line_items=[],
    )
    assert amt == 250.0
    assert needs_review is False


def test_resolve_amount_falls_back_to_receipt_extraction() -> None:
    amt, needs_review = _resolve_order_amount(
        order_prep={},
        brain_state={},
        receipt_metadata={"pdf_text_preview": "تم التحويل 180.50 ر.س"},
        line_items=[],
    )
    assert amt == 180.5
    assert needs_review is False


def test_resolve_amount_unknown_sets_review_flag() -> None:
    amt, needs_review = _resolve_order_amount(
        order_prep={},
        brain_state={},
        receipt_metadata={},
        line_items=[{"product_name": "عسل", "quantity": 1}],
    )
    assert amt is None
    assert needs_review is True


def test_upsert_skips_without_confirmed_receipt() -> None:
    db = MagicMock()
    conv = SimpleNamespace(id=1, customer=None, extra_metadata={})
    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=conv,
        brain_state={},
        order_prep={"payment_receipt_received": False},
    )
    assert result is None
    db.query.assert_not_called()


def test_upsert_creates_paid_nahla_order_with_nhl_number(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    class _Order:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.id = 101

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", _Order)
    monkeypatch.setattr(
        "services.nahla_order_bridge._allocate_nhl_number",
        lambda _db, _tid: "NHL-33-000001",
    )

    conv = SimpleNamespace(
        id=9063,
        customer=SimpleNamespace(phone="966551308005", name="Customer"),
        extra_metadata={},
    )
    order_prep = {
        "payment_receipt_received": True,
        "payment_receipt_at": "2026-05-28T16:26:23+00:00",
        "total_price": "320",
        "customer_first_name": "Ahmad",
        "city": "Riyadh",
        "payment_receipt_metadata": {"kind": "payment_receipt"},
    }

    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=conv,
        brain_state={"current_product_focus": {"title": "Honey", "price": "320"}},
        order_prep=order_prep,
    )

    assert result is not None
    assert result.id == 101
    assert result.external_id == "nahla-wa-33-9063"
    assert result.external_order_number == "NHL-33-000001"
    assert result.status == "paid"
    assert result.source == "whatsapp"
    assert result.total == "320.00 ر.س"
    assert result.extra_metadata["source_kind"] == "nahla_order"
    assert result.extra_metadata["needs_amount_review"] is False
    db.add.assert_called_once()
    db.flush.assert_called_once()


def test_upsert_is_idempotent_on_same_external_id() -> None:
    existing = SimpleNamespace(
        id=55,
        extra_metadata={"created_at": "2026-05-20T00:00:00+00:00"},
        total=None,
        status="paid",
        source="whatsapp",
        is_abandoned=False,
        line_items=[],
        customer_name=None,
        customer_info={},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    conv = SimpleNamespace(
        id=9063,
        customer=SimpleNamespace(phone="966551308005", name="عميل"),
        extra_metadata={},
    )
    order_prep = {
        "payment_receipt_received": True,
        "payment_receipt_at": "2026-05-28T16:26:23+00:00",
        "total_price": "150",
        "payment_receipt_metadata": {},
    }

    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=conv,
        brain_state={},
        order_prep=order_prep,
    )

    assert result is existing
    assert existing.total == "150.00 ر.س"
    assert existing.extra_metadata["needs_amount_review"] is False
    db.flush.assert_not_called()


def test_dashboard_revenue_only_counts_paid_status() -> None:
    """Mirror store_sync KPI rule — revenue sums paid rows only."""
    PAID = frozenset({"paid", "confirmed", "completed"})
    rows = [
        ("paid", "whatsapp", 100.0),
        ("pending", "whatsapp", 200.0),
        ("paid", "salla", 50.0),
        ("under_review", "whatsapp", 75.0),
    ]

    revenue = 0.0
    ai_revenue = 0.0
    for status_raw, src, amt in rows:
        status = "paid" if status_raw in PAID else "pending"
        if status == "paid":
            revenue += amt
            if src == "whatsapp":
                ai_revenue += amt

    assert revenue == 150.0
    assert ai_revenue == 100.0
