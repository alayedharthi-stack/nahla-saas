"""
tests/test_meta_catalog_commerce_invariants.py
──────────────────────────────────────────────
PR1 — Phase A commerce invariants (tests + docs only).

Pins the production contracts that must NOT regress while catalog work
continues:

  * Tenant isolation — Meta import and AI resolution never cross tenants.
  * retailer_id integrity — Meta ``retailer_id`` lands on ``meta_retailer_id``;
    Meta ``id`` lands on ``external_id`` (no separate ``meta_product_id`` column).
  * Re-import idempotency — second import updates, does not duplicate.
  * Catalog eligibility matrix — official WhatsApp product card vs fallback.
  * Coexistence token path — D360 keys never selected for Graph; platform
    ``WA_TOKEN`` fallback when provider != meta.

No DB, no HTTP, no production behaviour changes — DB-free stubs only.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    effective_retailer_id,
    is_catalog_eligible,
)
from services.meta_catalog_import import (  # noqa: E402
    CatalogDiscovery,
    ImportReport,
    _process_one_meta_product,
    _select_graph_token,
    import_from_meta,
)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory DB stub for import upsert tests
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _StoredProduct:
    tenant_id: int
    external_id: Optional[str] = None
    meta_retailer_id: Optional[str] = None
    title: str = ""
    description: Optional[str] = None
    price: Optional[str] = None
    in_stock: bool = True
    source: str = "meta"
    extra_metadata: Dict[str, Any] = field(default_factory=dict)
    id: int = 0


@dataclass
class _Conn:
    tenant_id: int
    meta_catalog_id: str = "CAT-A"
    provider: str = "meta"
    connection_type: str = "embedded"
    access_token: str = "fixture_merchant_graph_token"


def _literal(right: Any) -> Any:
    return getattr(right, "value", right)


class _ProductQuery:
    """Minimal SQLAlchemy filter interpreter for import upsert queries."""

    def __init__(self, db: "_InMemoryCatalogDb"):
        self._db = db
        self._tenant_id: Optional[int] = None
        self._external_ids: Set[str] = set()
        self._retailer_ids: Set[str] = set()

    def filter(self, *criteria: Any) -> "_ProductQuery":
        for c in criteria:
            if hasattr(c, "clauses"):
                for clause in c.clauses:
                    self.filter(clause)
                continue
            if not (hasattr(c, "left") and hasattr(c, "right")):
                continue
            key = getattr(c.left, "key", None)
            val = _literal(c.right)
            if key == "tenant_id":
                self._tenant_id = int(val)
            elif key == "external_id" and val is not None:
                self._external_ids.add(str(val))
            elif key == "meta_retailer_id" and val is not None:
                self._retailer_ids.add(str(val))
        return self

    def first(self) -> Optional[_StoredProduct]:
        for p in self._db.products:
            if self._tenant_id is not None and p.tenant_id != self._tenant_id:
                continue
            if not self._external_ids and not self._retailer_ids:
                return p
            if p.external_id in self._external_ids:
                return p
            if p.meta_retailer_id in self._retailer_ids:
                return p
            if p.external_id in self._retailer_ids:
                return p
        return None


class _ConnQuery:
    def __init__(self, db: "_InMemoryCatalogDb"):
        self._db = db
        self._tenant_id: Optional[int] = None

    def filter(self, *criteria: Any) -> "_ConnQuery":
        for c in criteria:
            if hasattr(c, "left") and hasattr(c, "right"):
                if getattr(c.left, "key", None) == "tenant_id":
                    self._tenant_id = int(_literal(c.right))
        return self

    def first(self) -> Optional[_Conn]:
        if self._tenant_id is None:
            return None
        return self._db.connections.get(self._tenant_id)


class _InMemoryCatalogDb:
    def __init__(self, connections: Dict[int, _Conn]):
        self.connections = connections
        self.products: List[_StoredProduct] = []
        self._next_id = 1

    def query(self, model: Any) -> Any:
        from models import Product, WhatsAppConnection  # noqa: PLC0415

        if model is Product:
            return _ProductQuery(self)
        if model is WhatsAppConnection:
            return _ConnQuery(self)
        raise AssertionError(f"unexpected query model: {getattr(model, '__name__', model)!r}")

    def add(self, obj: Any) -> None:
        if hasattr(obj, "tenant_id"):
            stored = _StoredProduct(
                tenant_id=obj.tenant_id,
                external_id=getattr(obj, "external_id", None),
                meta_retailer_id=getattr(obj, "meta_retailer_id", None),
                title=getattr(obj, "title", "") or "",
                description=getattr(obj, "description", None),
                price=getattr(obj, "price", None),
                in_stock=bool(getattr(obj, "in_stock", True)),
                source=getattr(obj, "source", None) or "meta",
                extra_metadata=dict(getattr(obj, "extra_metadata", None) or {}),
            )
            stored.id = self._next_id
            self._next_id += 1
            self.products.append(stored)
            obj.id = stored.id
        else:
            raise AssertionError(f"unexpected add() type: {type(obj)!r}")

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _patch_assign_canonical_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import meta_catalog_import as mci

    monkeypatch.setattr(mci, "assign_canonical_retailer_id", lambda p: False)


# ─────────────────────────────────────────────────────────────────────────────
# httpx helpers (import end-to-end, tenant-scoped)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload or {}
        import json as _json
        self.text = _json.dumps(self._payload)
        self.content = self.text.encode("utf-8")

    def json(self) -> Dict[str, Any]:
        return self._payload


class _ScriptedClient:
    def __init__(self, script: List[tuple]):
        self._script = list(script)
        self.calls: List[str] = []

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> _FakeResponse:
        full = url
        if params:
            try:
                full = str(httpx.URL(url, params=params))
            except Exception:
                full = url
        self.calls.append(full)
        for i, (needle, response) in enumerate(self._script):
            if needle in full:
                self._script.pop(i)
                return response
        raise AssertionError(f"unexpected URL {full!r}")

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _patch_import_graph(monkeypatch: pytest.MonkeyPatch, *, page_payload: Dict[str, Any]) -> None:
    from services import meta_catalog_import as mci

    discovery = CatalogDiscovery(
        catalog_id="CAT-A",
        ok=True,
        http_status=200,
        name="Test Catalog",
        vertical="commerce",
        catalog_type="PRODUCTS",
        supported_edges=["products"],
    )
    monkeypatch.setattr(
        mci,
        "_preflight_catalog_discovery",
        lambda client, *, tenant_id, catalog_id, token: discovery,
    )

    scripted = _ScriptedClient([("/products", _FakeResponse(200, page_payload))])

    class _Factory:
        def __init__(self, *a: Any, **kw: Any):
            self._client = scripted

        def __enter__(self):
            return self._client

        def __exit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr(mci.httpx, "Client", _Factory)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Meta product id mapping (external_id = Meta Graph id)
# ─────────────────────────────────────────────────────────────────────────────


class TestMetaProductIdMapping:
    def test_import_stores_meta_id_on_external_id_and_retailer_on_meta_retailer_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({10: _Conn(tenant_id=10)})
        _patch_assign_canonical_noop(monkeypatch)
        report = ImportReport()

        _process_one_meta_product(
            db, 10,
            {
                "id": "META-PROD-99",
                "retailer_id": "SKU-ABC",
                "name": "Test Abaya",
                "price": "199 SAR",
                "image_url": "https://cdn.example/a.jpg",
            },
            report,
        )

        assert report.created == 1
        assert len(db.products) == 1
        p = db.products[0]
        assert p.external_id == "META-PROD-99"
        assert p.meta_retailer_id == "SKU-ABC"
        assert p.tenant_id == 10
        assert effective_retailer_id(p) == "SKU-ABC"

    def test_reimport_updates_same_row_not_duplicate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({10: _Conn(tenant_id=10)})
        _patch_assign_canonical_noop(monkeypatch)
        report1 = ImportReport()
        row = {
            "id": "META-1",
            "retailer_id": "R-1",
            "name": "Original Title",
            "price": "10 SAR",
        }
        _process_one_meta_product(db, 10, row, report1)
        assert report1.created == 1

        report2 = ImportReport()
        row["name"] = "Updated Title"
        row["price"] = "12 SAR"
        _process_one_meta_product(db, 10, row, report2)

        assert report2.updated == 1
        assert report2.created == 0
        assert len(db.products) == 1
        assert db.products[0].title == "Updated Title"
        assert db.products[0].meta_retailer_id == "R-1"
        assert db.products[0].external_id == "META-1"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tenant isolation — import
# ─────────────────────────────────────────────────────────────────────────────


class TestImportTenantIsolation:
    def test_import_for_tenant_a_does_not_create_rows_for_tenant_b(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _InMemoryCatalogDb({
            10: _Conn(tenant_id=10, meta_catalog_id="CAT-10"),
            20: _Conn(tenant_id=20, meta_catalog_id="CAT-20"),
        })
        _patch_assign_canonical_noop(monkeypatch)
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)

        payload_a = {
            "data": [
                {"id": "A1", "retailer_id": "RA1", "name": "Tenant A Product",
                 "price": "50 SAR"},
            ],
            "paging": {},
        }
        _patch_import_graph(monkeypatch, page_payload=payload_a)
        report_a = import_from_meta(db, tenant_id=10)
        assert report_a.created == 1
        assert all(p.tenant_id == 10 for p in db.products)

        payload_b = {
            "data": [
                {"id": "B1", "retailer_id": "RB1", "name": "Tenant B Product",
                 "price": "60 SAR"},
            ],
            "paging": {},
        }
        _patch_import_graph(monkeypatch, page_payload=payload_b)
        report_b = import_from_meta(db, tenant_id=20)
        assert report_b.created == 1

        assert len(db.products) == 2
        tenant_ids = {p.tenant_id for p in db.products}
        assert tenant_ids == {10, 20}
        assert not any(
            p.external_id == "A1" and p.tenant_id == 20 for p in db.products
        )
        assert not any(
            p.external_id == "B1" and p.tenant_id == 10 for p in db.products
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Coexistence / Graph token selection
# ─────────────────────────────────────────────────────────────────────────────


class TestCoexistenceTokenPath:
    def test_meta_provider_uses_merchant_oauth_token(self) -> None:
        conn = SimpleNamespace(
            provider="meta",
            connection_type="embedded",
            access_token="fixture_merchant_graph_token",
        )
        pick = _select_graph_token(conn)
        assert pick["token"] == "fixture_merchant_graph_token"
        assert pick["token_source"] == "merchant_meta_oauth"

    def test_dialog360_does_not_use_d360_key_for_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = SimpleNamespace(
            provider="dialog360",
            connection_type="coexistence",
            access_token="coexistence_credential_not_used_on_graph",
        )
        monkeypatch.setenv("WHATSAPP_TOKEN", "dummy_graph_token_for_tests_only")
        from services import meta_catalog_import as mci

        monkeypatch.setattr(mci, "WA_TOKEN", "dummy_graph_token_for_tests_only")
        pick = _select_graph_token(conn)
        assert pick["token"] == "dummy_graph_token_for_tests_only"
        assert pick["token_source"] == "platform_system_user"
        assert pick["token"] != conn.access_token
        skipped = pick["considered"][0]
        assert skipped["source"] == "merchant_meta_oauth"
        assert "dialog360" in skipped["reason"] or "not 'meta'" in skipped["reason"]

    def test_dialog360_without_platform_token_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = SimpleNamespace(
            provider="dialog360",
            connection_type="coexistence",
            access_token="coexistence_credential_not_used_on_graph",
        )
        from services import meta_catalog_import as mci

        monkeypatch.setattr(mci, "WA_TOKEN", "")
        pick = _select_graph_token(conn)
        assert pick["token"] is None
        assert pick["token_source"] == "none"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Catalog eligibility matrix (official card vs fallback)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _EligConn:
    meta_catalog_id: Optional[str] = "CAT1"
    catalog_enabled: bool = True


@dataclass
class _EligProduct:
    external_id: Optional[str] = "ext-1"
    meta_retailer_id: Optional[str] = None


class TestCatalogEligibilityMatrix:
    @pytest.mark.parametrize(
        "conn,products,expected_ok,expected_reason",
        [
            (None, None, False, "connection_missing"),
            (_EligConn(catalog_enabled=False), None, False, "catalog_disabled"),
            (_EligConn(meta_catalog_id=""), None, False, "catalog_id_missing"),
            (_EligConn(), None, True, "ok"),
            (_EligConn(), [], False, "empty_products"),
            (
                _EligConn(),
                [_EligProduct(external_id=None, meta_retailer_id=None)],
                False,
                "no_retailer_id",
            ),
            (
                _EligConn(),
                [_EligProduct(external_id="SKU-1")],
                True,
                "ok",
            ),
            (
                _EligConn(),
                [_EligProduct(external_id=None, meta_retailer_id="META-RID-1")],
                True,
                "ok",
            ),
        ],
        ids=[
            "no_connection",
            "catalog_disabled",
            "catalog_id_missing",
            "connection_ok_no_products",
            "empty_product_list",
            "product_missing_retailer_id",
            "product_external_id_only",
            "product_meta_retailer_id",
        ],
    )
    def test_eligibility_decision_table(
        self,
        conn: Optional[_EligConn],
        products: Optional[List[_EligProduct]],
        expected_ok: bool,
        expected_reason: str,
    ) -> None:
        result = is_catalog_eligible(conn, products=products)
        assert result.ok is expected_ok
        assert result.reason == expected_reason


# ─────────────────────────────────────────────────────────────────────────────
# 5. AI product resolution — tenant scope
# ─────────────────────────────────────────────────────────────────────────────


class TestAiProductResolutionTenantScope:
    def test_resolve_by_query_passes_tenant_id_to_catalog_builder(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.product_resolver import resolve_by_query

        captured: Dict[str, int] = {}

        class _FakeBuilder:
            def __init__(self, db: Any, tenant_id: int):
                captured["tenant_id"] = tenant_id

            def search_products(self, q: str, limit: int = 5) -> List[Dict[str, Any]]:
                return []

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder",
            _FakeBuilder,
        )
        resolve_by_query(MagicMock(), tenant_id=55, query="abaya dress")
        assert captured["tenant_id"] == 55

    def test_resolve_by_query_relaxed_scopes_tenant_in_sql_and_builder(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.product_resolver import resolve_by_query_relaxed

        captured: Dict[str, Any] = {"builder_tenants": [], "sql_tenants": []}

        class _FakeBuilder:
            def __init__(self, db: Any, tenant_id: int):
                captured["builder_tenants"].append(tenant_id)

            def _format(self, product: Any) -> Dict[str, Any]:
                return {
                    "id": product.id,
                    "title": product.title,
                    "external_id": product.external_id,
                }

        class _ProductQueryChain:
            def __init__(self, db: Any, columns: Any):
                self._db = db
                self._columns = columns
                self._tenant_id: Optional[int] = None
                self._product_id: Optional[int] = None

            def filter(self, *criteria: Any) -> "_ProductQueryChain":
                for c in criteria:
                    if hasattr(c, "left") and hasattr(c, "right"):
                        key = getattr(c.left, "key", None)
                        val = _literal(c.right)
                        if key == "tenant_id":
                            self._tenant_id = int(val)
                            captured["sql_tenants"].append(int(val))
                        elif key == "id":
                            self._product_id = int(val)
                return self

            def limit(self, n: int) -> "_ProductQueryChain":
                return self

            def all(self) -> List[tuple]:
                if self._columns and self._tenant_id == 10:
                    return [(101, "Blue Abaya")]
                return []

            def first(self) -> Any:
                if self._tenant_id == 10 and self._product_id == 101:
                    return SimpleNamespace(
                        id=101, title="Blue Abaya", external_id="X1",
                        tenant_id=10,
                    )
                return None

        class _FakeDb:
            def query(self, *args: Any) -> _ProductQueryChain:
                return _ProductQueryChain(self, args)

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder",
            _FakeBuilder,
        )

        db = _FakeDb()
        hit = resolve_by_query_relaxed(db, tenant_id=10, query="abaya")
        assert hit is not None
        assert hit.id == 101
        assert 10 in captured["sql_tenants"]
        assert captured["builder_tenants"] == [10]

        miss = resolve_by_query_relaxed(db, tenant_id=20, query="abaya")
        assert miss is None

    def test_resolve_by_query_returns_none_for_other_tenant_catalog(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.product_resolver import resolve_by_query

        class _FakeBuilder:
            def __init__(self, db: Any, tenant_id: int):
                self.tenant_id = tenant_id

            def search_products(self, q: str, limit: int = 5) -> List[Dict[str, Any]]:
                if self.tenant_id == 10:
                    return [{"id": 1, "title": "Shared Name", "external_id": "X1"}]
                return []

        monkeypatch.setattr(
            "core.store_knowledge.CatalogContextBuilder",
            _FakeBuilder,
        )
        res_a = resolve_by_query(MagicMock(), tenant_id=10, query="Shared Name")
        res_b = resolve_by_query(MagicMock(), tenant_id=20, query="Shared Name")
        assert res_a is not None
        assert res_a.id == 1
        assert res_b is None
