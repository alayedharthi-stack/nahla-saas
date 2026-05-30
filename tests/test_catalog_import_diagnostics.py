"""
tests/test_catalog_import_diagnostics.py
────────────────────────────────────────
PR2 — catalog import metadata persistence + diagnostics payloads.

DB-free stubs only. No send-path / AI / webhook behaviour changes.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from routers.catalog import (  # noqa: E402
    _diagnostics_payload,
    _import_metadata_block,
    _whatsapp_commerce_readiness,
)
from services.meta_catalog_import import (  # noqa: E402
    CatalogDiscovery,
    GRAPH_RESULT_TOKEN_INVALID,
    GRAPH_RESULT_TOKEN_MISSING,
    GRAPH_RESULT_TOKEN_NO_CATALOG_ACCESS,
    ImportReport,
    MetaCatalogImportError,
    _persist_import_failed,
    _persist_import_running,
    _persist_import_success,
    _looks_like_meta_graph_token,
    _select_graph_token,
    build_graph_import_diagnostics,
    classify_meta_graph_error,
    describe_graph_token_selection,
    import_from_meta,
    sanitize_token_pick,
)


@dataclass
class _TrackingConn:
    meta_catalog_id: str = "CAT-1"
    provider: str = "meta"
    connection_type: str = "embedded"
    access_token: str = "fixture_merchant_graph_token"
    status: str = "connected"
    sending_enabled: bool = True
    phone_number_id: str = "1234567890"
    catalog_enabled: bool = True
    meta_import_status: Optional[str] = None
    meta_import_last_at: Optional[datetime] = None
    meta_import_last_error: Optional[str] = None
    meta_import_last_report: Optional[Dict[str, Any]] = None
    meta_import_token_source: Optional[str] = None
    commits: int = 0


class _ConnQuery:
    def __init__(self, conn: _TrackingConn):
        self._conn = conn

    def filter(self, *a, **kw):
        return self

    def first(self):
        return self._conn


class _TrackingDb:
    def __init__(self, conn: _TrackingConn):
        self.conn = conn

    def query(self, *a, **kw):
        return _ConnQuery(self.conn)

    def commit(self):
        self.conn.commits += 1


class TestImportMetadataPersistence:
    def test_running_success_failed_lifecycle(self):
        conn = _TrackingConn()
        db = _TrackingDb(conn)
        report = ImportReport(scanned=5, created=2, updated=1)

        _persist_import_running(db, conn, "merchant_oauth")
        assert conn.meta_import_status == "running"
        assert conn.meta_import_token_source == "merchant_oauth"
        assert conn.commits == 1

        _persist_import_success(db, conn, report, "merchant_oauth")
        assert conn.meta_import_status == "success"
        assert conn.meta_import_last_error is None
        assert conn.meta_import_last_report == report.to_dict()
        assert conn.meta_import_last_at is not None
        assert conn.meta_import_last_at.tzinfo is not None

        _persist_import_failed(db, conn, "catalog_not_found", token_source="merchant_oauth")
        assert conn.meta_import_status == "failed"
        assert conn.meta_import_last_error == "catalog_not_found"

    def test_catalog_id_missing_persists_failed_before_raise(self):
        conn = _TrackingConn(meta_catalog_id="")
        db = _TrackingDb(conn)

        with pytest.raises(MetaCatalogImportError) as exc_info:
            import_from_meta(db, tenant_id=1)

        assert exc_info.value.code == "catalog_id_missing"
        assert conn.meta_import_status == "failed"
        assert conn.meta_import_last_error == "catalog_id_missing"


def _patch_preflight(monkeypatch, discovery):
    from services import meta_catalog_import as mci

    def _fake(client, *, tenant_id, catalog_id, token):
        return discovery

    monkeypatch.setattr(mci, "_preflight_catalog_discovery", _fake)
    monkeypatch.setattr(mci, "_discovery_only_enabled", lambda: (True, "true"))


class TestImportFromMetaPersistsSuccess:
    def test_discovery_only_run_persists_discovery_only_status(self, monkeypatch):
        conn = _TrackingConn()
        db = _TrackingDb(conn)
        ok = CatalogDiscovery(catalog_id="CAT-1", ok=True, vertical="commerce")
        _patch_preflight(monkeypatch, ok)

        report = import_from_meta(db, tenant_id=1)

        assert report.discovery_only is True
        assert conn.meta_import_status == "discovery_only"
        assert conn.meta_import_last_report["discovery_only"] is True
        assert conn.meta_import_token_source is not None


class _Product:
    def __init__(self, *, tenant_id: int, external_id: str = "E1", meta_retailer_id=None):
        self.tenant_id = tenant_id
        self.external_id = external_id
        self.meta_retailer_id = meta_retailer_id
        self.source = "meta"


class _ProductQuery:
    def __init__(self, products: List[_Product]):
        self._products = products

    def filter(self, *a, **kw):
        return self

    def all(self):
        return list(self._products)


class _DiagDb:
    def __init__(self, conn: Optional[_TrackingConn], products: List[_Product]):
        self.conn = conn
        self.products = products

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            return _ConnQuery(self.conn) if self.conn else _ConnQuery(_TrackingConn())
        return _ProductQuery(self.products)


class TestDiagnosticsPayload:
    def test_import_metadata_block_empty_connection(self):
        block = _import_metadata_block(None)
        assert block["status"] is None
        assert block["last_report"] is None
        assert block["discovery_only"] is False
        assert block["products_imported"] is False

    def test_import_metadata_block_discovery_only_flags(self):
        conn = _TrackingConn()
        conn.meta_import_status = "discovery_only"
        conn.meta_import_last_report = {
            "discovery_only": True,
            "scanned": 0,
            "created": 0,
            "updated": 0,
        }
        block = _import_metadata_block(conn)
        assert block["discovery_only"] is True
        assert block["products_imported"] is False

    def test_whatsapp_readiness_missing_requirements(self, monkeypatch):
        conn = _TrackingConn(catalog_enabled=False, meta_catalog_id="")
        conn.access_token = ""
        monkeypatch.setattr(
            "services.meta_catalog_import.WA_TOKEN",
            "",
        )
        readiness = _whatsapp_commerce_readiness(
            conn=conn,
            catalog_id="",
            catalog_enabled=False,
            wa_connected=False,
            with_rid=0,
        )
        assert readiness["ready"] is False
        assert "meta_catalog_id" in readiness["missing_requirements"]
        assert "products_with_retailer_id" in readiness["missing_requirements"]

    def test_diagnostics_payload_includes_import_and_readiness(self):
        conn = _TrackingConn()
        conn.meta_import_status = "success"
        conn.meta_import_last_at = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
        conn.meta_import_last_report = {"scanned": 10, "created": 3, "updated": 2}
        conn.meta_import_token_source = "merchant_oauth"
        db = _DiagDb(conn, [_Product(tenant_id=1, meta_retailer_id="R1")])

        payload = _diagnostics_payload(db, tenant_id=1)  # type: ignore[arg-type]

        assert payload["import"]["status"] == "success"
        assert payload["import"]["last_report"]["scanned"] == 10
        assert "whatsapp_readiness" in payload
        assert payload["readiness"]["whatsapp_commerce_ready"] is True
        assert payload["whatsapp_readiness"]["ready"] is True
        assert "graph_import" in payload
        assert payload["graph_import"]["token_selection"]["token_source"] == "merchant_meta_oauth"


class TestGraphTokenSelection:
    def test_dialog360_short_key_skipped_for_graph(self, monkeypatch):
        monkeypatch.setattr(
            "services.meta_catalog_import.WA_TOKEN",
            "platform_graph_token_for_tests_only_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        )
        conn = _TrackingConn(
            provider="dialog360",
            connection_type="coexistence",
            access_token="short-d360-like-key-xx",
        )
        pick = _select_graph_token(conn)
        assert pick["token_source"] == "platform_system_user"
        assert "token" not in sanitize_token_pick(pick)

    def test_coexistence_long_eaa_token_preferred(self, monkeypatch):
        monkeypatch.setattr("services.meta_catalog_import.WA_TOKEN", "platform_fallback")
        merchant = "EAA" + ("x" * 120)
        conn = _TrackingConn(
            provider="dialog360",
            connection_type="coexistence",
            access_token=merchant,
        )
        pick = _select_graph_token(conn)
        assert pick["token_source"] == "merchant_meta_oauth"
        assert pick["token"] == merchant

    def test_describe_never_includes_raw_token(self):
        conn = _TrackingConn(access_token="secret_graph_token_value_xxxxxxxx")
        desc = describe_graph_token_selection(conn)
        assert "secret_graph_token_value" not in str(desc)
        assert desc["token_present"] is True
        assert "token" not in desc


class TestGraphErrorClassification:
    def test_missing_token(self):
        c = classify_meta_graph_error(token_source="none")
        assert c["result_code"] == GRAPH_RESULT_TOKEN_MISSING

    def test_invalid_oauth_190(self):
        c = classify_meta_graph_error(
            {"meta_code": 190, "meta_type": "OAuthException", "meta_message": "Invalid OAuth access token"},
            token_source="platform_system_user",
        )
        assert c["result_code"] == GRAPH_RESULT_TOKEN_INVALID

    def test_permission_denied(self):
        c = classify_meta_graph_error(
            {"meta_code": 10, "meta_type": "OAuthException", "meta_message": "Permission denied"},
            http_status=403,
            token_source="platform_system_user",
        )
        assert c["result_code"] == GRAPH_RESULT_TOKEN_NO_CATALOG_ACCESS


class TestGraphImportDiagnosticsBuilder:
    def test_no_connection(self):
        d = build_graph_import_diagnostics(None)
        assert d["result_code"] == "connection_missing"

    def test_no_catalog_id(self):
        conn = _TrackingConn(meta_catalog_id="")
        d = build_graph_import_diagnostics(conn)
        assert d["result_code"] == "catalog_id_missing"

    def test_token_missing_when_no_sources(self, monkeypatch):
        monkeypatch.setattr("services.meta_catalog_import.WA_TOKEN", "")
        conn = _TrackingConn(
            provider="dialog360",
            connection_type="coexistence",
            access_token="short-d360-key",
        )
        d = build_graph_import_diagnostics(conn, run_preflight=False)
        assert d["result_code"] == GRAPH_RESULT_TOKEN_MISSING
        assert d["action_required"]

    def test_diagnostics_payload_includes_graph_import(self):
        conn = _TrackingConn()
        db = _DiagDb(conn, [])
        payload = _diagnostics_payload(db, tenant_id=1)  # type: ignore[arg-type]
        assert payload["graph_import"]["provider"] == "meta"
        assert payload["graph_import"]["token_selection"]["token_present"] is True


class TestLooksLikeMetaGraphToken:
    def test_short_is_false(self):
        assert _looks_like_meta_graph_token("D360-abc") is False
        assert _looks_like_meta_graph_token("short") is False

    def test_long_eaa_is_true(self):
        assert _looks_like_meta_graph_token("EAA" + ("a" * 100)) is True
