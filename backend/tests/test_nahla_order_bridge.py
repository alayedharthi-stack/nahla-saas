"""Tests for Nahla internal order bridge (Phase 1 + Phase 2)."""
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
    _build_line_items,
    _build_sync_snapshot,
    _customer_payload,
    _meaningful_delta,
    _resolve_customer_name,
    _resolve_order_amount,
    _resolve_product_title,
    compute_kpi_totals,
    nahla_wa_external_id,
    sync_nahla_wa_order,
    upsert_nahla_paid_order,
)

_DB_KW = {"db": MagicMock(), "tenant_id": 33, "conversation_id": 9063}


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
        "current_product_focus": {"title": "Honey", "price": "320", "id": 9},
        "checkout_url": "",
    }
    base.update(extra)
    return base


def test_nahla_wa_external_id_is_stable() -> None:
    assert nahla_wa_external_id(33, 9063) == "nahla-wa-33-9063"


def test_resolve_amount_prefers_receipt_over_order_prep() -> None:
    amt, needs_review, source = _resolve_order_amount(
        order_prep={"total_price": "250"},
        brain_state={},
        receipt_metadata={"pdf_text_preview": "تم التحويل 299 SAR"},
        line_items=[],
        is_paid_path=True,
        **_DB_KW,
    )
    assert amt == 299.0
    assert needs_review is False
    assert source == "receipt_extraction"


def test_resolve_amount_falls_back_to_order_prep_when_no_receipt() -> None:
    amt, needs_review, source = _resolve_order_amount(
        order_prep={"total_price": "250 ر.س"},
        brain_state={},
        receipt_metadata={},
        line_items=[],
        is_paid_path=False,
        **_DB_KW,
    )
    assert amt == 250.0
    assert needs_review is False
    assert source == "order_prep_total_price"


def test_resolve_amount_unknown_sets_review_flag() -> None:
    amt, needs_review, source = _resolve_order_amount(
        order_prep={},
        brain_state={},
        receipt_metadata={},
        line_items=[{"product_name": "عسل", "quantity": 1}],
        is_paid_path=False,
        **_DB_KW,
    )
    assert amt is None
    assert needs_review is True
    assert source == "unknown"


def test_resolve_customer_name_rejects_phone_like_db_name() -> None:
    conv = SimpleNamespace(
        customer=SimpleNamespace(
            name="0551308005",
            phone="966551308005",
            extra_metadata={},
        ),
        extra_metadata={},
    )
    assert _resolve_customer_name(conv, {"customer_first_name": "سارة"}) == "سارة"
    assert _resolve_customer_name(conv, {}) is None


def test_upsert_skips_without_explicit_verification() -> None:
    db = MagicMock()
    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=_conv(id=1),
        brain_state={},
        order_prep={"payment_receipt_received": True},
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

    order_prep = {
        "payment_receipt_received": True,
        "payment_confirmed": True,
        "payment_receipt_at": "2026-05-28T16:26:23+00:00",
        "total_price": "320",
        "customer_first_name": "Ahmad",
        "customer_last_name": "Ali",
        "city": "Riyadh",
        "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        "payment_receipt_metadata": {"kind": "payment_receipt"},
    }

    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=_conv(),
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
    assert result.extra_metadata["lifecycle"] == "paid"
    assert result.extra_metadata["origin"] == "whatsapp_ai"
    assert result.extra_metadata["created_by"] == "ai_assistant"
    assert result.extra_metadata["counts_in_revenue"] is True
    assert result.extra_metadata["needs_amount_review"] is False
    db.add.assert_called_once()
    db.flush.assert_called_once()


def test_upsert_is_idempotent_on_same_external_id() -> None:
    existing = SimpleNamespace(
        id=55,
        tenant_id=33,
        extra_metadata={
            "created_at": "2026-05-20T00:00:00+00:00",
            "last_sync_snapshot": {},
        },
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

    order_prep = {
        "payment_receipt_received": True,
        "payment_confirmed": True,
        "payment_receipt_at": "2026-05-28T16:26:23+00:00",
        "total_price": "150",
        "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        "customer_first_name": "A",
        "customer_last_name": "B",
        "city": "Riyadh",
        "payment_receipt_metadata": {"pdf_text_preview": "تم التحويل 299 SAR"},
    }

    result = upsert_nahla_paid_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state={},
        order_prep=order_prep,
    )

    assert result is existing
    assert existing.total == "299.00 ر.س"
    assert existing.extra_metadata["amount_source"] == "receipt_extraction"
    db.flush.assert_not_called()


def test_draft_creates_pending_customer_info_without_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    class _Order:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.id = 201

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", _Order)
    monkeypatch.setattr(
        "services.nahla_order_bridge._allocate_nhl_number",
        lambda _db, _tid: "NHL-33-000002",
    )

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(),
        trigger="brain_save",
    )

    assert result is not None
    assert result.status == "pending_customer_info"
    assert "delivery_address" in result.extra_metadata["missing_fields"]
    assert result.extra_metadata["lifecycle"] == "whatsapp_draft"
    assert result.extra_metadata["counts_in_revenue"] is False
    assert result.customer_info["phone"] == "966551308005"
    assert result.total == "320.00 ر.س"


def test_draft_creates_pending_payment_with_valid_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    class _Order:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.id = 202

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", _Order)
    monkeypatch.setattr(
        "services.nahla_order_bridge._allocate_nhl_number",
        lambda _db, _tid: "NHL-33-000003",
    )

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(
            customer_last_name="Ali",
            google_maps_url="https://maps.google.com/?q=24.7,46.6",
        ),
        trigger="brain_save",
    )

    assert result is not None
    assert result.status == "pending_payment"
    assert result.extra_metadata["missing_fields"] == []
    assert result.extra_metadata["delivery_address_status"] == "accepted"


def test_draft_skipped_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", raising=False)
    db = MagicMock()
    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(),
    )
    assert result is None
    db.query.assert_not_called()


def test_draft_skipped_for_non_allowlisted_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    result = sync_nahla_wa_order(
        db,
        tenant_id=34,
        conversation=_conv(id=100, tenant_id=34, customer_id=2, customer=SimpleNamespace(
            id=2, tenant_id=34, phone="966500000000", name="X", extra_metadata={},
        )),
        brain_state=_brain(),
        order_prep=_draft_prep(),
    )
    assert result is None
    db.query.assert_not_called()


def test_tenant_ownership_mismatch_blocks_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(tenant_id=34),
        brain_state=_brain(),
        order_prep=_draft_prep(),
    )
    assert result is None
    db.query.assert_not_called()


def test_spam_guard_skips_when_no_material_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    prep = _draft_prep()
    brain = _brain()
    items, _, _ = _build_line_items(
        db=MagicMock(),
        tenant_id=33,
        order_prep=prep,
        brain_state=brain,
    )
    from core.wa_cart_line_items import line_items_fingerprint  # noqa: WPS433

    snap = _build_sync_snapshot(
        prep,
        brain,
        lifecycle="whatsapp_draft",
        line_items_fingerprint=line_items_fingerprint(items),
    )
    existing = SimpleNamespace(
        id=77,
        tenant_id=33,
        status="pending_payment",
        total="320.00 ر.س",
        source="whatsapp",
        is_abandoned=False,
        line_items=items,
        customer_name="Ahmad",
        customer_info={},
        extra_metadata={"last_sync_snapshot": snap, "lifecycle": "whatsapp_draft"},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(),
        trigger="brain_save",
    )

    assert result is existing
    db.add.assert_not_called()


def test_spam_guard_allows_update_when_city_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    snap = _build_sync_snapshot(_draft_prep(city=""), _brain(), lifecycle="whatsapp_draft")
    existing = SimpleNamespace(
        id=77,
        tenant_id=33,
        status="pending_payment",
        total="320.00 ر.س",
        source="whatsapp",
        is_abandoned=False,
        line_items=[],
        customer_name="Ahmad",
        customer_info={},
        extra_metadata={"last_sync_snapshot": snap, "lifecycle": "whatsapp_draft"},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(city="Jeddah"),
        trigger="brain_save",
    )

    assert result is existing
    db.add.assert_called_once()


def test_draft_promotes_to_payment_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    snap = _build_sync_snapshot(_draft_prep(), _brain(), lifecycle="whatsapp_draft")
    existing = SimpleNamespace(
        id=88,
        tenant_id=33,
        status="pending_payment",
        total="320.00 ر.س",
        external_id="nahla-wa-33-9063",
        external_order_number="NHL-33-000003",
        source="whatsapp",
        is_abandoned=False,
        line_items=[],
        customer_name="Ahmad",
        customer_info={},
        extra_metadata={"last_sync_snapshot": snap, "lifecycle": "whatsapp_draft"},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(),
        order_prep=_draft_prep(
            customer_last_name="Ali",
            google_maps_url="https://maps.google.com/?q=24.7,46.6",
            payment_receipt_received=True,
            payment_receipt_at="2026-05-29T12:00:00+00:00",
            payment_receipt_metadata={"pdf_text_preview": "تم التحويل 320 SAR"},
        ),
        trigger="state_patch",
    )

    assert result is existing
    assert existing.status == "payment_submitted"
    assert existing.extra_metadata["lifecycle"] == "whatsapp_draft"
    assert existing.extra_metadata["counts_in_revenue"] is False


def test_paid_order_not_downgraded_by_draft_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    snap = _build_sync_snapshot(
        _draft_prep(payment_receipt_received=True),
        _brain(),
        lifecycle="paid",
    )
    existing = SimpleNamespace(
        id=99,
        tenant_id=33,
        status="paid",
        total="320.00 ر.س",
        source="whatsapp",
        is_abandoned=False,
        line_items=[],
        customer_name="Ahmad",
        customer_info={},
        extra_metadata={"last_sync_snapshot": snap, "lifecycle": "paid"},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(stage="ordering"),
        order_prep=_draft_prep(),
        trigger="brain_save",
    )

    assert result is existing
    assert existing.status == "paid"
    db.add.assert_not_called()


def test_draft_order_counts_in_orders_but_not_revenue() -> None:
    """Draft pending_payment must not inflate sales / ai_revenue KPIs."""
    rows = [
        SimpleNamespace(status="pending_payment", source="whatsapp", total="500.00 ر.س"),
        SimpleNamespace(status="paid", source="whatsapp", total="100.00 ر.س"),
        SimpleNamespace(status="paid", source="salla", total="50.00 ر.س"),
    ]
    totals = compute_kpi_totals(rows)

    assert totals["orders_count"] == 3.0
    assert totals["revenue"] == 150.0
    assert totals["ai_revenue"] == 100.0


def test_meaningful_delta_detects_awaiting_payment_flip() -> None:
    prev = _build_sync_snapshot(_draft_prep(), _brain(), lifecycle="whatsapp_draft")
    curr = _build_sync_snapshot(
        _draft_prep(awaiting_payment_receipt=True, order_status="awaiting_receipt"),
        _brain(),
        lifecycle="whatsapp_draft",
    )
    ok, reason = _meaningful_delta(prev, curr)
    assert ok is True
    assert reason.startswith("changed:")


def test_resolve_customer_name_prefers_wa_profile_over_phone_db_name() -> None:
    conv = SimpleNamespace(
        customer=SimpleNamespace(
            name="0551308005",
            phone="966551308005",
            extra_metadata={"wa_profile_name": "سارة"},
        ),
        extra_metadata={},
    )
    assert _resolve_customer_name(conv, {}) == "سارة"


def test_build_line_items_uses_product_title_from_order_prep() -> None:
    items, title, _ = _build_line_items(
        db=MagicMock(),
        tenant_id=33,
        order_prep={"product_name": "عسل طلح ربع كيلو", "quantity": 1, "price": "99"},
        brain_state={},
    )
    assert title == "عسل طلح ربع كيلو"
    assert items[0]["product_name"] == "عسل طلح ربع كيلو"
    assert items[0]["title"] == "عسل طلح ربع كيلو"
    assert items[0]["unit_price"] == 99.0


def test_build_line_items_uses_focus_title_not_generic() -> None:
    items, title, _ = _build_line_items(
        db=MagicMock(),
        tenant_id=33,
        order_prep={"quantity": 1},
        brain_state={
            "current_product_focus": {
                "title": "عسل طلح ربع كيلو",
                "id": 9,
                "price": "320",
            },
        },
    )
    assert title == "عسل طلح ربع كيلو"
    assert items[0]["product_name"] == "عسل طلح ربع كيلو"
    assert items[0]["title"] == "عسل طلح ربع كيلو"
    assert title != "منتج"


def test_build_line_items_prefers_order_prep_name_over_focus() -> None:
    items, title, _ = _build_line_items(
        db=MagicMock(),
        tenant_id=33,
        order_prep={"product_name": "من prep", "quantity": 1},
        brain_state={"current_product_focus": {"title": "من focus"}},
    )
    assert title == "من prep"
    assert items[0]["title"] == "من prep"


def test_build_line_items_uses_cart_line_item_title() -> None:
    items, title, _ = _build_line_items(
        db=MagicMock(),
        tenant_id=33,
        order_prep={
            "quantity": 1,
            "line_items": [{"title": "عسل سدر", "quantity": 1}],
        },
        brain_state={},
    )
    assert title == "عسل سدر"
    assert items[0]["product_name"] == "عسل سدر"


def test_customer_payload_auto_fills_whatsapp_phone() -> None:
    conv = SimpleNamespace(
        customer=SimpleNamespace(
            name="0551308005",
            phone="966551308005",
            extra_metadata={"wa_profile_name": "سارة"},
        ),
        extra_metadata={},
    )
    name, info = _customer_payload(conv, {"customer_phone": "966551308005"})
    assert name == "سارة"
    assert info["phone"] == "966551308005"
    assert info["shipping_phone"] == "966551308005"


def test_resolve_product_title_from_focus() -> None:
    title = _resolve_product_title(
        db=MagicMock(),
        tenant_id=33,
        order_prep={},
        brain_state={"current_product_focus": {"title": "Honey Jar", "price": "120"}},
    )
    assert title == "Honey Jar"


def test_dashboard_revenue_only_counts_paid_status() -> None:
    rows = [
        SimpleNamespace(status="paid", source="whatsapp", total="100.00 ر.س"),
        SimpleNamespace(status="pending_payment", source="whatsapp", total="200.00 ر.س"),
        SimpleNamespace(status="paid", source="salla", total="50.00 ر.س"),
        SimpleNamespace(status="under_review", source="whatsapp", total="75.00 ر.س"),
    ]
    totals = compute_kpi_totals(rows)
    assert totals["orders_count"] == 4.0
    assert totals["revenue"] == 150.0
    assert totals["ai_revenue"] == 100.0


def test_sync_multi_item_cart_updates_same_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    existing = SimpleNamespace(
        id=301,
        tenant_id=33,
        external_id="nahla-wa-33-9063",
        status="pending_customer_info",
        line_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1, "unit_price": 100}],
        extra_metadata={"last_sync_snapshot": {"product_id": "p1", "quantity": 1, "line_items_fingerprint": "x"}},
        customer_info={},
        total="100.00 ر.س",
    )
    db.query.return_value.filter_by.return_value.first.return_value = existing

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", type(existing))

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(
            current_product_focus={"title": "عسل سمر", "price": "80", "id": "p2", "variant": "500g"},
        ),
        order_prep=_draft_prep(
            product_id="p2",
            product_name="عسل سمر",
            line_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1, "unit_price": 100}],
        ),
        trigger="brain_save",
    )

    assert result is existing
    assert len(result.line_items) == 2
    names = {i["product_name"] for i in result.line_items}
    assert names == {"عسل طلح", "عسل سمر"}


def test_sync_empty_cart_after_remove_stays_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_draft_bridge(monkeypatch)
    db = MagicMock()
    existing = SimpleNamespace(
        id=302,
        tenant_id=33,
        external_id="nahla-wa-33-9063",
        status="pending_customer_info",
        line_items=[{"product_name": "عسل سمر", "variant": "500g", "quantity": 1}],
        extra_metadata={"last_sync_snapshot": {"product_id": "", "quantity": 1, "line_items_fingerprint": "y"}},
        customer_info={},
        total="80.00 ر.س",
    )
    db.query.return_value.filter_by.return_value.first.return_value = existing

    import models  # noqa: WPS433

    monkeypatch.setattr(models, "Order", type(existing))

    result = sync_nahla_wa_order(
        db,
        tenant_id=33,
        conversation=_conv(),
        brain_state=_brain(stage="ordering", current_product_focus={}),
        order_prep=_draft_prep(
            product_id="",
            cart_deltas=[{"op": "remove", "match": {"product_name_contains": "سمر"}}],
            line_items=[],
        ),
        trigger="brain_save",
    )

    assert result is existing
    assert result.line_items == []
    assert result.status == "draft"
    assert "product" in result.extra_metadata["missing_fields"]

