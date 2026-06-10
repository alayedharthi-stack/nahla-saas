"""
Meta catalog reconciliation — P1-G1.

DB-free stubs; extends the in-memory harness from commerce invariants.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_MERCHANT_HIDDEN,
    CATALOG_STATUS_REMOVED_FROM_META,
    SOURCE_MANUAL,
    SOURCE_META,
)
from services.meta_catalog_import import (  # noqa: E402
    ImportReport,
    _maybe_reconcile_meta_missing,
    _process_one_meta_product,
    import_from_meta,
)
from test_meta_catalog_commerce_invariants import (  # noqa: E402
    _Conn,
    _InMemoryCatalogDb,
    _patch_assign_canonical_noop,
    _patch_import_graph,
)


@dataclass
class _VisProduct:
    tenant_id: int
    external_id: Optional[str] = None
    meta_retailer_id: Optional[str] = None
    title: str = ""
    description: Optional[str] = None
    price: Optional[str] = None
    in_stock: bool = True
    source: str = SOURCE_META
    catalog_status: str = CATALOG_STATUS_ACTIVE
    merchant_hidden_at: Optional[datetime] = None
    meta_removed_at: Optional[datetime] = None
    meta_last_seen_at: Optional[datetime] = None
    extra_metadata: Dict[str, Any] = field(default_factory=dict)
    id: int = 0


def _literal(right: Any) -> Any:
    return getattr(right, "value", right)


class _ReconcileProductQuery:
    def __init__(self, db: "_ReconcileDb"):
        self._db = db
        self._tenant_id: Optional[int] = None
        self._source: Optional[str] = None
        self._external_ids: Set[str] = set()
        self._retailer_ids: Set[str] = set()
        self._require_external_id = False

    def filter(self, *criteria: Any) -> "_ReconcileProductQuery":
        for c in criteria:
            if hasattr(c, "clauses"):
                for clause in c.clauses:
                    self._ingest_clause(clause)
                continue
            self._ingest_clause(c)
        return self

    def _ingest_clause(self, c: Any) -> None:
        if hasattr(c, "left") and hasattr(c, "right"):
            key = getattr(c.left, "key", None)
            val = _literal(c.right)
            if key == "tenant_id":
                self._tenant_id = int(val)
            elif key == "source":
                self._source = str(val)
            elif key == "external_id" and val is not None:
                self._external_ids.add(str(val))
            elif key == "meta_retailer_id" and val is not None:
                self._retailer_ids.add(str(val))
            return
        # SQLAlchemy ``column.isnot(None)``
        left = getattr(c, "left", None)
        if getattr(left, "key", None) == "external_id":
            self._require_external_id = True

    def _tenant_ok(self, p: _VisProduct) -> bool:
        return self._tenant_id is None or p.tenant_id == self._tenant_id

    def all(self) -> List[_VisProduct]:
        out: List[_VisProduct] = []
        for p in self._db.products:
            if not self._tenant_ok(p):
                continue
            if self._source is not None and (p.source or "") != self._source:
                continue
            if self._require_external_id and not (p.external_id or "").strip():
                continue
            out.append(p)
        return out

    def first(self) -> Optional[_VisProduct]:
        for p in self._db.products:
            if not self._tenant_ok(p):
                continue
            if self._external_ids or self._retailer_ids:
                if (p.external_id or "") in self._external_ids:
                    return p
                if (p.meta_retailer_id or "") in self._retailer_ids:
                    return p
                if (p.external_id or "") in self._retailer_ids:
                    return p
                continue
            return p
        return None


class _ReconcileDb(_InMemoryCatalogDb):
    products: List[_VisProduct]

    def add(self, obj: Any) -> None:
        if hasattr(obj, "tenant_id"):
            stored = _VisProduct(
                tenant_id=obj.tenant_id,
                external_id=getattr(obj, "external_id", None),
                meta_retailer_id=getattr(obj, "meta_retailer_id", None),
                title=getattr(obj, "title", "") or "",
                description=getattr(obj, "description", None),
                price=getattr(obj, "price", None),
                in_stock=bool(getattr(obj, "in_stock", True)),
                source=getattr(obj, "source", None) or SOURCE_META,
                catalog_status=getattr(obj, "catalog_status", None) or CATALOG_STATUS_ACTIVE,
                merchant_hidden_at=getattr(obj, "merchant_hidden_at", None),
                meta_removed_at=getattr(obj, "meta_removed_at", None),
                meta_last_seen_at=getattr(obj, "meta_last_seen_at", None),
                extra_metadata=dict(getattr(obj, "extra_metadata", None) or {}),
            )
            stored.id = self._next_id
            self._next_id += 1
            self.products.append(stored)
            obj.id = stored.id
        else:
            super().add(obj)

    def query(self, model: Any) -> Any:
        from models import Product, WhatsAppConnection  # noqa: PLC0415

        if model is Product:
            return _ReconcileProductQuery(self)
        return super().query(model)


def _seed_meta(db: _ReconcileDb, *, ext: str | None, title: str, **kw: Any) -> _VisProduct:
    p = _VisProduct(tenant_id=10, external_id=ext, title=title, source=SOURCE_META, **kw)
    p.id = len(db.products) + 1
    db.products.append(p)
    db._next_id = max(db._next_id, p.id + 1)
    return p


class TestMetaCatalogReconciliation:
    def test_missing_remote_products_marked_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10, meta_catalog_id="CAT-10")})
        _seed_meta(db, ext="META-GONE", title="Ghost")
        _patch_assign_canonical_noop(monkeypatch)
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        _patch_import_graph(
            monkeypatch,
            page_payload={"data": [{"id": "META-NEW", "name": "Fresh", "price": "10 SAR"}], "paging": {}},
        )
        report = import_from_meta(db, tenant_id=10)
        ghost = next(p for p in db.products if p.external_id == "META-GONE")
        assert report.reconciled_missing == 1
        assert ghost.catalog_status == CATALOG_STATUS_REMOVED_FROM_META
        assert ghost.in_stock is False
        assert ghost.meta_removed_at is not None

    def test_removed_product_gets_in_stock_false(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        ghost = _seed_meta(db, ext="META-X", title="X", in_stock=True)
        report = ImportReport(seen_meta_external_ids=set(), pages_fetched=1)
        _maybe_reconcile_meta_missing(db, 10, report)
        assert ghost.in_stock is False
        assert ghost.catalog_status == CATALOG_STATUS_REMOVED_FROM_META

    def test_failed_partial_import_skips_reconciliation(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        ghost = _seed_meta(db, ext="META-OLD", title="Old")
        for flag, reason in (
            ({"truncated": True}, "truncated"),
            ({"pagination_incomplete": True}, "pagination_incomplete"),
            ({"pages_fetched": 0}, "no_pages_fetched"),
            ({"discovery_only": True}, "discovery_only"),
        ):
            ghost.catalog_status = CATALOG_STATUS_ACTIVE
            ghost.in_stock = True
            ghost.meta_removed_at = None
            report = ImportReport(**flag)
            _maybe_reconcile_meta_missing(db, 10, report)
            assert ghost.catalog_status == CATALOG_STATUS_ACTIVE
            assert report.reconciliation_skipped is True
            assert report.reconciliation_skip_reason == reason

    def test_manual_products_unaffected(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        manual = _VisProduct(
            tenant_id=10, external_id="MAN-1", title="Manual", source=SOURCE_MANUAL,
        )
        manual.id = 1
        db.products.append(manual)
        report = ImportReport(seen_meta_external_ids=set(), pages_fetched=1)
        _maybe_reconcile_meta_missing(db, 10, report)
        assert manual.catalog_status == CATALOG_STATUS_ACTIVE
        assert manual.in_stock is True

    def test_hidden_stays_hidden_when_meta_sees_again(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        hidden = _seed_meta(
            db,
            ext="META-H",
            title="Hidden Honey",
            catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN,
            merchant_hidden_at=datetime.now(timezone.utc),
        )
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            {"id": "META-H", "name": "Hidden Honey", "price": "50 SAR"},
            report,
        )
        assert hidden.catalog_status == CATALOG_STATUS_MERCHANT_HIDDEN
        assert hidden.merchant_hidden_at is not None

    def test_removed_meta_product_restored_on_reimport(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        row = _seed_meta(
            db,
            ext="META-R",
            title="Back",
            catalog_status=CATALOG_STATUS_REMOVED_FROM_META,
            in_stock=False,
            meta_removed_at=datetime.now(timezone.utc),
        )
        report = ImportReport()
        _process_one_meta_product(
            db, 10,
            {"id": "META-R", "name": "Back", "price": "20 SAR", "availability": "in stock"},
            report,
        )
        assert report.restored_from_meta == 1
        assert row.catalog_status == CATALOG_STATUS_ACTIVE
        assert row.meta_removed_at is None
        assert row.in_stock is True

    def test_reconciliation_keeps_product_matched_by_retailer_id_only(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        row = _VisProduct(
            tenant_id=10,
            external_id=None,
            meta_retailer_id="SKU-ONLY",
            title="Retailer match",
            source=SOURCE_META,
        )
        row.id = 1
        db.products.append(row)
        report = ImportReport(
            seen_meta_external_ids={"META-OTHER"},
            seen_meta_retailer_ids={"SKU-ONLY"},
            pages_fetched=1,
        )
        _maybe_reconcile_meta_missing(db, 10, report)
        assert report.reconciled_missing == 0
        assert row.catalog_status == CATALOG_STATUS_ACTIVE
        assert row.in_stock is True
        assert row.meta_removed_at is None

    def test_reconciliation_keeps_legacy_external_id_as_retailer_id(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        row = _VisProduct(
            tenant_id=10,
            external_id="SKU-LEG",
            meta_retailer_id=None,
            title="Legacy retailer in external_id",
            source=SOURCE_META,
        )
        row.id = 1
        db.products.append(row)
        report = ImportReport(
            seen_meta_external_ids={"META-999"},
            seen_meta_retailer_ids={"SKU-LEG"},
            pages_fetched=1,
        )
        _maybe_reconcile_meta_missing(db, 10, report)
        assert report.reconciled_missing == 0
        assert row.catalog_status == CATALOG_STATUS_ACTIVE

    def test_import_reconciliation_matches_retailer_id_with_mismatched_external_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10, meta_catalog_id="CAT-10")})
        legacy = _VisProduct(
            tenant_id=10,
            external_id="OLD-WRONG-ID",
            meta_retailer_id="SKU-LIVE",
            title="Legacy row",
            source=SOURCE_META,
        )
        legacy.id = 1
        db.products.append(legacy)
        _patch_assign_canonical_noop(monkeypatch)
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        _patch_import_graph(
            monkeypatch,
            page_payload={
                "data": [{
                    "id": "META-NEW-ID",
                    "retailer_id": "SKU-LIVE",
                    "name": "Legacy row",
                    "price": "10 SAR",
                }],
                "paging": {},
            },
        )
        report = import_from_meta(db, tenant_id=10)
        assert report.reconciled_missing == 0
        assert legacy.catalog_status == CATALOG_STATUS_ACTIVE
        assert legacy.in_stock is True
        assert legacy.meta_last_seen_at is not None

    def test_still_tombstones_when_neither_id_nor_retailer_seen(self) -> None:
        db = _ReconcileDb({10: _Conn(tenant_id=10)})
        ghost = _VisProduct(
            tenant_id=10,
            external_id="META-GONE",
            meta_retailer_id="RID-GONE",
            title="Truly gone",
            source=SOURCE_META,
        )
        ghost.id = 1
        db.products.append(ghost)
        report = ImportReport(
            seen_meta_external_ids={"META-OTHER"},
            seen_meta_retailer_ids={"RID-OTHER"},
            pages_fetched=1,
        )
        _maybe_reconcile_meta_missing(db, 10, report)
        assert report.reconciled_missing == 1
        assert ghost.catalog_status == CATALOG_STATUS_REMOVED_FROM_META
        assert ghost.in_stock is False
