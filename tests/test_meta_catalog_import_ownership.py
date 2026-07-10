"""
tests/test_meta_catalog_import_ownership.py
────────────────────────────────────────────
Phase 2 — Meta import must not overwrite external-platform products.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    CONFLICT_POSSIBLE_DUPLICATE,
    OWNERSHIP_META_READONLY,
    SOURCE_MANUAL,
    SOURCE_META,
    SOURCE_META_EXISTING,
    SOURCE_SALLA,
)
from services.meta_catalog_import import (  # noqa: E402
    ImportReport,
    _process_one_meta_product,
)
from test_meta_catalog_commerce_invariants import (  # noqa: E402
    _InMemoryCatalogDb,
    _StoredProduct,
    _patch_assign_canonical_noop,
)


def _row(
    *,
    meta_id: str = "META-100",
    retailer_id: str = "1847291",
    name: str = "Imported Title",
    price: str = "99 SAR",
) -> dict:
    return {
        "id": meta_id,
        "retailer_id": retailer_id,
        "name": name,
        "price": price,
    }


class TestMetaImportOwnershipGuards:
    def test_skips_salla_row_matched_by_meta_retailer_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="1847291",
            meta_retailer_id="1847291",
            title="Salla Product",
            price="50 SAR",
            source=SOURCE_SALLA,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(db, 10, _row(retailer_id="1847291", name="Meta Title", price="1 SAR"), report)

        p = db.products[0]
        assert report.skipped_protected == 1
        assert report.updated == 0
        assert p.title == "Salla Product"
        assert p.price == "50 SAR"
        assert p.source == SOURCE_SALLA
        assert p.external_id == "1847291"

    def test_does_not_restamp_salla_source_to_meta(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="S-42",
            meta_retailer_id="S-42",
            title="Keep",
            source=SOURCE_SALLA,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(db, 10, _row(meta_id="META-999", retailer_id="S-42"), report)

        assert db.products[0].source == SOURCE_SALLA
        assert report.skipped_protected == 1

    def test_does_not_overwrite_salla_external_id_with_meta_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="SALLA-KEY-77",
            meta_retailer_id="SKU-77",
            title="Salla",
            source=SOURCE_SALLA,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            _row(meta_id="META-GRAPH-77", retailer_id="SKU-77"),
            report,
        )

        assert db.products[0].external_id == "SALLA-KEY-77"
        assert report.skipped_protected == 1

    def test_refreshes_legacy_meta_row(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="META-OLD",
            meta_retailer_id="R-OLD",
            title="Old",
            price="10 SAR",
            source=SOURCE_META,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            _row(meta_id="META-OLD", retailer_id="R-OLD", name="Fresh", price="20 SAR"),
            report,
        )

        p = db.products[0]
        assert report.updated == 1
        assert report.refreshed_meta == 1
        assert p.title == "Fresh"
        assert p.price == "20 SAR"
        assert p.source == SOURCE_META

    def test_manual_row_not_overwritten_conflict_flagged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="MAN-1",
            meta_retailer_id="SHARED-RID",
            title="Manual Product",
            price="30 SAR",
            source=SOURCE_MANUAL,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            _row(meta_id="META-M", retailer_id="SHARED-RID", name="Meta Win", price="1 SAR"),
            report,
        )

        p = db.products[0]
        assert report.flagged_conflict == 1
        assert report.skipped_manual == 1
        assert p.title == "Manual Product"
        assert p.price == "30 SAR"
        assert p.source == SOURCE_MANUAL
        assert p.source_conflict_status == CONFLICT_POSSIBLE_DUPLICATE
        assert p.source_conflict_detail is not None

    def test_reimport_same_meta_item_no_duplicate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        _patch_assign_canonical_noop(monkeypatch)
        row = _row(meta_id="META-DUP", retailer_id="R-DUP")
        report1 = ImportReport()
        _process_one_meta_product(db, 10, row, report1)
        assert report1.created == 1
        assert len(db.products) == 1
        assert db.products[0].source == SOURCE_META_EXISTING
        assert db.products[0].ownership_mode == OWNERSHIP_META_READONLY

        report2 = ImportReport()
        row["name"] = "Updated"
        _process_one_meta_product(db, 10, row, report2)
        assert report2.created == 0
        assert report2.updated == 1
        assert len(db.products) == 1
        assert db.products[0].title == "Updated"

    def test_cross_source_retailer_id_conflict_not_merged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({})
        db.products.append(_StoredProduct(
            tenant_id=10,
            external_id="999",
            meta_retailer_id="999",
            title="Native",
            source=SOURCE_MANUAL,
        ))
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            _row(meta_id="META-OTHER", retailer_id="999"),
            report,
        )

        assert len(db.products) == 1
        assert report.flagged_conflict == 1
        assert db.products[0].source == SOURCE_MANUAL
        assert db.products[0].external_id == "999"
