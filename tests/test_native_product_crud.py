"""
tests/test_native_product_crud.py
──────────────────────────────────
Phase 3A — Nahla-native product CRUD ownership guards.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    OWNERSHIP_META_READONLY,
    SOURCE_MANUAL,
    SOURCE_META_EXISTING,
    SOURCE_NAHLA_NATIVE,
    SOURCE_SALLA,
    is_merchant_editable_product,
    merchant_edit_rejection_detail,
)
from fastapi import HTTPException  # noqa: E402
from routers.catalog import (  # noqa: E402
    SOURCE_NAHLA_NATIVE as ROUTER_SOURCE_NAHLA_NATIVE,
    OWNERSHIP_NAHLA_MANAGED as ROUTER_OWNERSHIP_NAHLA_MANAGED,
    _assert_merchant_editable_or_409,
)
from services.store_sync import StoreSyncService  # noqa: E402


def _product(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "tenant_id": 10,
        "source": None,
        "ownership_mode": None,
        "external_id": None,
        "title": "Test",
        "description": None,
        "price": "10",
        "sku": None,
        "in_stock": True,
        "stock_quantity": None,
        "extra_metadata": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestIsMerchantEditableProduct:
    def test_nahla_native_with_ownership(self) -> None:
        p = _product(source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED)
        assert is_merchant_editable_product(p) is True

    def test_legacy_manual_without_ownership(self) -> None:
        p = _product(source=SOURCE_MANUAL, ownership_mode=None)
        assert is_merchant_editable_product(p) is True

    def test_salla_blocked(self) -> None:
        p = _product(source=SOURCE_SALLA, external_id="123")
        assert is_merchant_editable_product(p) is False
        assert merchant_edit_rejection_detail(p) == "product_not_editable_external_managed"

    def test_meta_existing_blocked(self) -> None:
        p = _product(source=SOURCE_META_EXISTING, ownership_mode=OWNERSHIP_META_READONLY)
        assert is_merchant_editable_product(p) is False
        assert merchant_edit_rejection_detail(p) == "product_not_editable_meta_readonly"

    def test_external_managed_ownership_blocks(self) -> None:
        p = _product(source=SOURCE_MANUAL, ownership_mode=OWNERSHIP_EXTERNAL_MANAGED)
        assert is_merchant_editable_product(p) is False


class TestAssertMerchantEditableOr409:
    def test_allows_native(self) -> None:
        _assert_merchant_editable_or_409(
            _product(source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED)
        )

    def test_blocks_salla(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _assert_merchant_editable_or_409(_product(source=SOURCE_SALLA, external_id="99"))
        assert exc.value.status_code == 409
        assert exc.value.detail == "product_not_editable_external_managed"


class TestNativeCreateContract:
    def test_router_uses_canonical_constants(self) -> None:
        assert ROUTER_SOURCE_NAHLA_NATIVE == SOURCE_NAHLA_NATIVE
        assert ROUTER_OWNERSHIP_NAHLA_MANAGED == OWNERSHIP_NAHLA_MANAGED


class TestStoreSyncNativeProtection:
    def test_skips_overwrite_on_external_id_collision(self) -> None:
        native = _product(
            id=55,
            source=SOURCE_NAHLA_NATIVE,
            ownership_mode=OWNERSHIP_NAHLA_MANAGED,
            external_id="COLLIDE-1",
            title="حذاء رياضي أبيض",
            price="199 SAR",
        )
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = native

        svc = StoreSyncService.__new__(StoreSyncService)
        svc.db = db
        svc.tenant_id = 10

        normalised = {
            "external_id": "COLLIDE-1",
            "title": "Salla Overwrite",
            "description": "from salla",
            "price": "1 SAR",
            "sku": "S-1",
            "in_stock": True,
            "stock_qty": 5,
            "source": SOURCE_SALLA,
        }
        result = svc._apply_normalised_product(normalised, SOURCE_SALLA)

        assert result["action"] == "skipped_protected"
        assert result["product_id"] == 55
        assert native.title == "حذاء رياضي أبيض"
        assert native.price == "199 SAR"
        assert native.source == SOURCE_NAHLA_NATIVE

    def test_salla_row_still_updates_normally(self) -> None:
        existing = _product(
            id=77,
            source=SOURCE_SALLA,
            external_id="S-77",
            title="Old",
            price="10",
            extra_metadata={"source": SOURCE_SALLA},
        )
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = existing

        svc = StoreSyncService.__new__(StoreSyncService)
        svc.db = db
        svc.tenant_id = 10

        normalised = {
            "external_id": "S-77",
            "title": "Updated Salla",
            "description": None,
            "price": "20 SAR",
            "sku": None,
            "in_stock": True,
            "stock_qty": 3,
            "source": SOURCE_SALLA,
        }
        result = svc._apply_normalised_product(normalised, SOURCE_SALLA)

        assert result["action"] == "updated"
        assert existing.title == "Updated Salla"
        assert existing.price == "20 SAR"


class TestLegacyManualStillEditable:
    def test_legacy_manual_passes_guard(self) -> None:
        p = _product(source=SOURCE_MANUAL, ownership_mode=None)
        _assert_merchant_editable_or_409(p)

    def test_legacy_manual_in_list_source_normalizes(self) -> None:
        from core.catalog import product_source  # noqa: PLC0415

        p = _product(source=SOURCE_MANUAL)
        assert product_source(p) == SOURCE_NAHLA_NATIVE
