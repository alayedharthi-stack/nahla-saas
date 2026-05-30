"""
tests/test_meta_catalog_import_persistence.py
──────────────────────────────────────────────
End-to-end import persistence + diagnostics coverage (DB-free stubs).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import effective_retailer_id  # noqa: E402
from routers.catalog import _diagnostics_payload  # noqa: E402
from services.meta_catalog_import import (  # noqa: E402
    CatalogDiscovery,
    ImportReport,
    import_from_meta,
)

# Reuse in-memory harness from commerce invariants
from test_meta_catalog_commerce_invariants import (  # noqa: E402
    _Conn,
    _InMemoryCatalogDb,
    _patch_assign_canonical_noop,
    _patch_import_graph,
)


@dataclass
class _TrackingConn(_Conn):
    meta_import_status: Optional[str] = None
    meta_import_last_at: Any = None
    meta_import_last_error: Optional[str] = None
    meta_import_last_report: Optional[Dict[str, Any]] = None
    meta_import_token_source: Optional[str] = None
    status: str = "connected"
    sending_enabled: bool = True
    phone_number_id: str = "1234567890"
    catalog_enabled: bool = True


class _ConnQuery:
    def __init__(self, conn: _TrackingConn):
        self._conn = conn

    def filter(self, *a, **kw):
        return self

    def first(self):
        return self._conn


class _DiagSession:
    """Wraps in-memory catalog db for diagnostics payload."""

    def __init__(self, catalog_db: _InMemoryCatalogDb, tenant_id: int):
        self._catalog_db = catalog_db
        self._tenant_id = tenant_id
        conn = catalog_db.connections.get(tenant_id)
        self._conn = _TrackingConn(
            tenant_id=tenant_id,
            meta_catalog_id=getattr(conn, "meta_catalog_id", "CAT-1"),
            provider=getattr(conn, "provider", "meta"),
            connection_type=getattr(conn, "connection_type", "embedded"),
            access_token=getattr(conn, "access_token", "tok"),
            catalog_enabled=True,
            status="connected",
            sending_enabled=True,
            phone_number_id="123",
        )

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            return _ConnQuery(self._conn)
        from test_meta_catalog_commerce_invariants import _ProductQuery  # noqa: PLC0415
        return _ProductQuery(self._catalog_db)


class TestFullImportPersistenceAndDiagnostics:
    def test_successful_import_persists_products_and_diagnostics_coverage(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({10: _Conn(tenant_id=10, meta_catalog_id="CAT-10")})
        _patch_assign_canonical_noop(monkeypatch)
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)

        payload = {
            "data": [
                {
                    "id": "META-99",
                    "retailer_id": "SKU-99",
                    "name": "Honey Jar",
                    "price": "120 SAR",
                },
            ],
            "paging": {},
        }
        _patch_import_graph(monkeypatch, page_payload=payload)
        report = import_from_meta(db, tenant_id=10)

        assert report.discovery_only is False
        assert report.created == 1
        assert report.scanned == 1
        assert len(db.products) == 1
        p = db.products[0]
        assert p.external_id == "META-99"
        assert p.meta_retailer_id == "SKU-99"
        assert effective_retailer_id(p) == "SKU-99"

        diag = _diagnostics_payload(_DiagSession(db, 10), tenant_id=10)  # type: ignore[arg-type]
        assert diag["products"]["total"] == 1
        assert diag["products"]["with_effective_retailer_id"] == 1
        assert diag["products"]["coverage_pct"] == 100
        assert diag["readiness"]["catalog_ready"] is True

    def test_coverage_not_zero_zero_after_full_import(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({5: _Conn(tenant_id=5, meta_catalog_id="CAT-5")})
        _patch_assign_canonical_noop(monkeypatch)
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        _patch_import_graph(
            monkeypatch,
            page_payload={
                "data": [{"id": "P1", "retailer_id": "R1", "name": "One"}],
                "paging": {},
            },
        )
        import_from_meta(db, tenant_id=5)
        diag = _diagnostics_payload(_DiagSession(db, 5), tenant_id=5)  # type: ignore[arg-type]
        assert diag["products"]["total"] > 0
        assert diag["products"]["with_effective_retailer_id"] > 0
        assert not (
            diag["products"]["total"] == 0
            and diag["products"]["with_effective_retailer_id"] == 0
        )

    def test_discovery_only_leaves_zero_products_and_zero_coverage(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services import meta_catalog_import as mci

        db = _InMemoryCatalogDb({10: _Conn(tenant_id=10)})
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", "true")
        monkeypatch.setattr(
            mci,
            "_preflight_catalog_discovery",
            lambda client, *, tenant_id, catalog_id, token: CatalogDiscovery(
                catalog_id=catalog_id, ok=True, vertical="commerce", product_count=5,
            ),
        )
        report = import_from_meta(db, tenant_id=10)
        assert report.discovery_only is True
        assert len(db.products) == 0
        diag = _diagnostics_payload(_DiagSession(db, 10), tenant_id=10)  # type: ignore[arg-type]
        assert diag["products"]["total"] == 0
        assert diag["products"]["with_effective_retailer_id"] == 0
