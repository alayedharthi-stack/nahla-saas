"""Tests for Meta catalog import upsert guards (mocked, no Graph I/O)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    CONFLICT_POSSIBLE_DUPLICATE,
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_META_EXISTING,
    SOURCE_NAHLA_NATIVE,
    merchant_edit_rejection_detail,
)
from services.meta_catalog_import import (  # noqa: E402
    CatalogDiscovery,
    ImportReport,
    _process_one_meta_product,
)


def _meta_row(
    *,
    meta_id: str = "META-IMPORT-100",
    retailer_id: str = "sku-import-100",
    name: str = "عطر ورد 100ml",
    price: str = "199 SAR",
):
    return {
        "id": meta_id,
        "retailer_id": retailer_id,
        "name": name,
        "description": "وصف عام",
        "price": price,
        "image_url": "https://cdn.example/perfume.jpg",
        "url": "https://store.example/perfume",
        "availability": "in stock",
    }


def _db_for_product(existing):
    db = MagicMock()
    added = []

    def query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q.filter.return_value.filter.return_value.first.return_value = existing
        return q

    db.query.side_effect = query
    db.add.side_effect = lambda obj: added.append(obj)
    db.flush.return_value = None
    return db, added


def _salla_product():
    return SimpleNamespace(
        id=201,
        tenant_id=9,
        title="حذاء سلة",
        price="120",
        external_id="salla-88001",
        meta_retailer_id=None,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        description=None,
        in_stock=True,
        extra_metadata={},
        source_conflict_status=None,
        source_conflict_detail=None,
        catalog_status="active",
        merchant_hidden_at=None,
        meta_item_id=None,
    )


def _native_product():
    return SimpleNamespace(
        id=202,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        price="199",
        external_id=None,
        meta_retailer_id="nahla_p_202",
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        description="وصف",
        in_stock=True,
        extra_metadata={},
        source_conflict_status=None,
        source_conflict_detail=None,
        catalog_status="active",
        merchant_hidden_at=None,
        meta_item_id=None,
    )


def _meta_existing_product():
    return SimpleNamespace(
        id=203,
        tenant_id=9,
        title="عطر قديم",
        price="150 SAR",
        external_id="META-IMPORT-100",
        meta_retailer_id="sku-import-100",
        source=SOURCE_META_EXISTING,
        ownership_mode=OWNERSHIP_META_READONLY,
        description="قديم",
        in_stock=True,
        extra_metadata={"source": SOURCE_META_EXISTING},
        source_conflict_status=None,
        source_conflict_detail=None,
        catalog_status="active",
        merchant_hidden_at=None,
        meta_item_id="META-IMPORT-100",
        meta_last_seen_at=None,
    )


def test_import_creates_meta_existing_readonly():
    db, added = _db_for_product(None)
    report = ImportReport()

    with patch("services.meta_catalog_import.assign_canonical_retailer_id"):
        _process_one_meta_product(db, 9, _meta_row(), report)

    assert report.created == 1
    assert len(added) == 1
    product = added[0]
    assert product.source == SOURCE_META_EXISTING
    assert product.ownership_mode == OWNERSHIP_META_READONLY
    assert product.external_id == "META-IMPORT-100"
    assert product.meta_retailer_id == "sku-import-100"
    assert product.title == "عطر ورد 100ml"


def test_import_skips_salla_without_overwrite():
    existing = _salla_product()
    before_title = existing.title
    before_price = existing.price
    before_external = existing.external_id
    db, added = _db_for_product(existing)
    report = ImportReport()

    _process_one_meta_product(
        db,
        9,
        _meta_row(meta_id="META-OTHER", retailer_id=before_external),
        report,
    )

    assert report.skipped_protected == 1
    assert report.created == 0
    assert not added
    assert existing.title == before_title
    assert existing.price == before_price
    assert existing.source == "salla"
    assert existing.external_id == before_external


def test_import_flags_native_conflict_without_overwrite():
    existing = _native_product()
    before_title = existing.title
    before_price = existing.price
    db, added = _db_for_product(existing)
    report = ImportReport()

    _process_one_meta_product(
        db,
        9,
        _meta_row(retailer_id=existing.meta_retailer_id),
        report,
    )

    assert report.flagged_conflict == 1
    assert report.skipped_manual == 1
    assert report.created == 0
    assert not added
    assert existing.title == before_title
    assert existing.price == before_price
    assert existing.source == SOURCE_NAHLA_NATIVE
    assert existing.source_conflict_status == CONFLICT_POSSIBLE_DUPLICATE
    assert existing.source_conflict_detail is not None


def test_import_refreshes_meta_existing_row():
    existing = _meta_existing_product()
    before_source = existing.source
    before_ownership = existing.ownership_mode
    db, added = _db_for_product(existing)
    report = ImportReport()

    _process_one_meta_product(
        db,
        9,
        _meta_row(name="عطر محدّث", price="249 SAR"),
        report,
    )

    assert report.updated == 1
    assert report.refreshed_meta == 1
    assert not added
    assert existing.title == "عطر محدّث"
    assert existing.price == "249 SAR"
    assert existing.source == before_source
    assert existing.ownership_mode == before_ownership


def test_meta_readonly_product_not_merchant_editable():
    product = _meta_existing_product()
    assert merchant_edit_rejection_detail(product) == "product_not_editable_meta_readonly"


@patch("services.meta_catalog_import._maybe_reconcile_meta_missing")
@patch("services.meta_catalog_import._discovery_only_enabled", return_value=(False, ""))
@patch("services.meta_catalog_import._preflight_catalog_discovery")
@patch("services.meta_catalog_import.httpx.Client")
@patch("services.meta_catalog_import._select_graph_token")
def test_import_from_meta_one_page_creates_product(
    mock_token_pick,
    mock_client_cls,
    mock_preflight,
    _mock_discovery_only,
    _mock_reconcile,
):
    from services.meta_catalog_import import import_from_meta  # noqa: PLC0415

    mock_token_pick.return_value = {
        "token": "test-graph-token",
        "token_source": "platform_system_user",
        "provider": "meta",
        "connection_type": "direct",
        "considered": [],
        "token_tail": "oken",
        "token_len": 16,
    }
    mock_preflight.return_value = CatalogDiscovery(
        catalog_id="CAT-TEST",
        ok=True,
        http_status=200,
        catalog_type="commerce",
        vertical="commerce",
        supported_edges=["products"],
    )

    import httpx  # noqa: PLC0415

    page_resp = httpx.Response(
        200,
        json={"data": [_meta_row(meta_id="META-PAGE-1", retailer_id="sku-page-1")]},
    )
    mock_client = MagicMock()
    mock_client.get.return_value = page_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client_cls.return_value = mock_client

    conn = SimpleNamespace(
        tenant_id=9,
        meta_catalog_id="CAT-TEST",
        provider="meta",
        connection_type="direct",
        access_token="enc1:fake",
        meta_import_status=None,
        meta_import_last_at=None,
        meta_import_last_error=None,
        meta_import_last_report=None,
        meta_import_token_source=None,
    )
    added = []

    def query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        elif name == "Product":
            q.filter.return_value.filter.return_value.first.return_value = None
        return q

    db = MagicMock()
    db.query.side_effect = query
    db.add.side_effect = lambda obj: added.append(obj)
    db.flush.return_value = None
    db.commit.return_value = None

    with patch("services.meta_catalog_import.assign_canonical_retailer_id"):
        report = import_from_meta(db, 9)

    assert report.created == 1
    assert len(added) == 1
    assert added[0].source == SOURCE_META_EXISTING
    assert added[0].ownership_mode == OWNERSHIP_META_READONLY
    mock_client.get.assert_called()
