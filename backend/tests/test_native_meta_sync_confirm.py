"""Tests for Nahla-native Meta sync confirm push (Phase 3B-2)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    OWNERSHIP_META_READONLY,
    SOURCE_NAHLA_NATIVE,
)
from services.meta_catalog_sync_confirm import (  # noqa: E402
    confirm_native_meta_sync,
    ensure_native_default_variant,
)


def _conn():
    return SimpleNamespace(
        tenant_id=9,
        meta_catalog_id="CAT-GENERIC-001",
        provider="meta",
        connection_type="direct",
        access_token="EAAB-test-token",
    )


def _native_parent(
    *,
    pid: int = 101,
    price: str = "199",
    with_image: bool = True,
    with_url: bool = True,
):
    meta = {"currency": "SAR"}
    if with_image:
        meta["image_url"] = "https://cdn.example/shoe.jpg"
    if with_url:
        meta["product_url"] = "https://store.example/shoe"
    return SimpleNamespace(
        id=pid,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        description="وصف",
        price=price,
        sku=None,
        meta_retailer_id=None,
        in_stock=True,
        stock_quantity=5,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        extra_metadata=meta,
        sync_status=None,
        sync_error=None,
        last_synced_at=None,
    )


def _salla_parent():
    return SimpleNamespace(
        id=202,
        tenant_id=9,
        title="عسل",
        price="120",
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        extra_metadata={},
        sync_status=None,
        sync_error=None,
        last_synced_at=None,
    )


def _meta_parent():
    return SimpleNamespace(
        id=303,
        tenant_id=9,
        title="Meta product",
        price="50",
        source="meta",
        ownership_mode=OWNERSHIP_META_READONLY,
        extra_metadata={},
        sync_status=None,
        sync_error=None,
        last_synced_at=None,
    )


def _variant(parent, *, vid: int = 501):
    return SimpleNamespace(
        id=vid,
        tenant_id=9,
        product_id=parent.id,
        retailer_id="nahla_p_101",
        price=parent.price,
        currency="SAR",
        stock_quantity=5,
        in_stock=True,
        image_url=parent.extra_metadata.get("image_url"),
        options={},
        extra_metadata=parent.extra_metadata,
        is_default=True,
    )


def _preview_ok():
    return {
        "eligible": True,
        "dry_run": True,
        "would_sync": False,
        "product_id": 101,
        "retailer_id": "nahla_p_101",
        "payload": {"price": 19900},
        "fatal_errors": [],
        "warnings": [],
    }


def _preview_fatal():
    body = _preview_ok()
    body["fatal_errors"] = [{"code": "missing_image_url", "message_ar": "صورة"}]
    return body


def _push_ok():
    return {
        "ok": True,
        "action": "create",
        "retailer_id": "nahla_p_101",
        "meta_product_id": "META-999",
        "meta": {"http_status": 200, "response": {"id": "META-999"}},
    }


def _mock_db(parent=None, variant=None):
    db = MagicMock()
    parent = parent or _native_parent()
    variants = [variant] if variant else []

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q.filter.return_value.first.return_value = parent
        elif name == "ProductVariant":
            q.filter.return_value.order_by.return_value.first.side_effect = (
                lambda: variants[0] if variants else None
            )
        return q

    db.query.side_effect = _query
    return db, parent, variants


def test_confirm_required_when_false():
    db = MagicMock()
    result = confirm_native_meta_sync(db, 9, 101, confirm=False)
    assert result["error_code"] == "confirm_required"
    db.commit.assert_not_called()


def test_confirm_required_when_missing_flag():
    db = MagicMock()
    result = confirm_native_meta_sync(db, 9, 101, confirm=False)
    assert result["eligible"] is False


def test_salla_product_rejected_before_graph():
    parent = _salla_parent()
    db, _, _ = _mock_db(parent=parent)
    with patch("services.meta_catalog_sync_confirm.push_one_meta_catalog_item") as push_mock:
        result = confirm_native_meta_sync(db, 9, 202, confirm=True)
    assert result["error_code"] == "product_not_meta_export_eligible"
    push_mock.assert_not_called()


def test_meta_existing_rejected_before_graph():
    parent = _meta_parent()
    db, _, _ = _mock_db(parent=parent)
    with patch("services.meta_catalog_sync_confirm.push_one_meta_catalog_item") as push_mock:
        result = confirm_native_meta_sync(db, 9, 303, confirm=True)
    assert result["error_code"] == "product_not_meta_export_eligible"
    push_mock.assert_not_called()


def test_preview_fatal_rejected_before_graph_and_variant():
    parent = _native_parent(with_image=False)
    db, _, variants = _mock_db(parent=parent, variant=None)
    with patch(
        "services.meta_catalog_sync_confirm.preview_native_meta_sync",
        return_value=_preview_fatal(),
    ):
        with patch(
            "services.meta_catalog_sync_confirm.ensure_native_default_variant",
        ) as ensure_mock:
            with patch("services.meta_catalog_sync_confirm.push_one_meta_catalog_item") as push_mock:
                result = confirm_native_meta_sync(db, 9, 101, confirm=True)
    assert result["error_code"] == "preview_fatal"
    ensure_mock.assert_not_called()
    push_mock.assert_not_called()
    db.commit.assert_not_called()


def test_creates_default_variant_only_at_confirm():
    parent = _native_parent()
    db = MagicMock()
    added = []

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q.filter.return_value.first.return_value = parent
        elif name == "ProductVariant":
            q.filter.return_value.order_by.return_value.first.return_value = None
        return q

    db.query.side_effect = _query

    def _add(obj):
        added.append(obj)
        obj.id = 777

    db.add.side_effect = _add

    variant, created = ensure_native_default_variant(db, parent)
    assert created is True
    assert len(added) == 1
    assert added[0].is_default is True
    assert added[0].retailer_id == "nahla_p_101"
    db.flush.assert_called_once()


def test_does_not_duplicate_existing_variant():
    parent = _native_parent()
    existing = _variant(parent)
    db, _, _ = _mock_db(parent=parent, variant=existing)
    variant, created = ensure_native_default_variant(db, parent)
    assert created is False
    assert variant is existing
    db.add.assert_not_called()


def test_valid_confirm_calls_push_once_and_syncs():
    parent = _native_parent()
    db, parent, variants = _mock_db(parent=parent, variant=None)
    new_variant = _variant(parent, vid=777)

    with patch(
        "services.meta_catalog_sync_confirm.preview_native_meta_sync",
        return_value=_preview_ok(),
    ):
        with patch(
            "services.meta_catalog_sync_confirm.ensure_native_default_variant",
            return_value=(new_variant, True),
        ):
            with patch(
                "services.meta_catalog_sync_confirm.push_one_meta_catalog_item",
                return_value=_push_ok(),
            ) as push_mock:
                result = confirm_native_meta_sync(db, 9, 101, confirm=True)

    push_mock.assert_called_once()
    assert push_mock.call_args.kwargs["confirm"] is True
    assert result["ok"] is True
    assert result["sync_status"] == "synced"
    assert parent.sync_error is None
    assert parent.last_synced_at is not None
    assert parent.source == SOURCE_NAHLA_NATIVE
    assert parent.ownership_mode == OWNERSHIP_NAHLA_MANAGED
    db.commit.assert_called()


def test_graph_failure_sets_sync_failed_without_changing_ownership():
    parent = _native_parent()
    db, parent, _ = _mock_db(parent=parent, variant=_variant(parent))
    before_synced = parent.last_synced_at

    with patch(
        "services.meta_catalog_sync_confirm.preview_native_meta_sync",
        return_value=_preview_ok(),
    ):
        with patch(
            "services.meta_catalog_sync_confirm.ensure_native_default_variant",
            return_value=(_variant(parent), False),
        ):
            with patch(
                "services.meta_catalog_sync_confirm.push_one_meta_catalog_item",
                return_value={
                    "ok": False,
                    "error": "meta_http_error",
                    "meta": {
                        "http_status": 400,
                        "response": {"error": {"message": "bad item"}},
                    },
                },
            ):
                result = confirm_native_meta_sync(db, 9, 101, confirm=True)

    assert result["ok"] is False
    assert parent.sync_status == "sync_failed"
    assert parent.sync_error
    assert "access_token" not in (parent.sync_error or "").lower()
    assert parent.last_synced_at == before_synced
    assert parent.ownership_mode == OWNERSHIP_NAHLA_MANAGED


def test_router_confirm_required_422():
    import asyncio

    from fastapi import HTTPException  # noqa: PLC0415

    from routers.catalog import merchant_catalog_meta_sync_confirm  # noqa: E402

    with patch("routers.catalog.resolve_tenant_id", return_value=9):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                merchant_catalog_meta_sync_confirm(
                    101,
                    body=type("B", (), {"confirm": False})(),
                    request=MagicMock(),
                    db=MagicMock(),
                    _user={"sub": "u1"},
                )
            )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "confirm_required"
