"""Tests for read-only Meta Catalog ↔ WABA link status."""

from __future__ import annotations

import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from routers.catalog import merchant_router  # noqa: E402
from services.meta_catalog_linking import (  # noqa: E402
    WABA_ERROR_INACCESSIBLE,
    WABA_ERROR_NOT_FOUND,
    get_waba_catalog_link_status,
)
from services.meta_catalog_import import (  # noqa: E402
    GRAPH_RESULT_META_HTTP_ERROR,
    GRAPH_RESULT_TOKEN_INVALID,
)


def _conn(
    *,
    waba_id: str = "WABA-GENERIC-001",
    catalog_id: str = "CAT-GENERIC-001",
    token: str = "EAAB-generic-test-token",
    provider: str = "meta",
):
    return SimpleNamespace(
        tenant_id=9,
        whatsapp_business_account_id=waba_id,
        meta_catalog_id=catalog_id,
        provider=provider,
        connection_type="embedded",
        access_token=token,
    )


def _db_with(conn):
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = conn
    db.query.return_value = q
    return db


def _db_none():
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = None
    db.query.return_value = q
    return db


def _graph_response(catalogs, status=200):
    return httpx.Response(
        status,
        json={"data": catalogs},
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


def _graph_error(status=403, code=200, message="permission denied"):
    return httpx.Response(
        status,
        json={"error": {"code": code, "type": "OAuthException", "message": message}},
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


def test_link_status_no_connection():
    out = get_waba_catalog_link_status(_db_none(), 9)
    assert out["ok"] is False
    assert out["error"] == "connection_not_found"
    assert "connection" in out["missing"]


def test_link_status_missing_waba_id():
    conn = _conn(waba_id="")
    out = get_waba_catalog_link_status(_db_with(conn), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_waba_id"
    assert "waba_id" in out["missing"]


def test_link_status_missing_catalog_id():
    conn = _conn(catalog_id="")
    out = get_waba_catalog_link_status(_db_with(conn), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_catalog_id"
    assert "meta_catalog_id" in out["missing"]


@patch("services.meta_catalog_linking._select_graph_token", return_value={"token": "", "token_source": "none"})
def test_link_status_missing_token(_mock_token):
    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_graph_token"
    assert "graph_token" in out["missing"]


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_expected_linked_true(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_response([
        {"id": "CAT-GENERIC-001", "name": "متجر تجريبي عام"},
        {"id": "CAT-OTHER", "name": "كتالوج آخر"},
    ])

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["connected"] is True
    assert out["expected_catalog_linked"] is True
    assert out["link_status"] == "linked"
    assert out["http_status"] == 200
    assert out["linked_catalog_ids"] == ["CAT-GENERIC-001", "CAT-OTHER"]
    mock_client.get.assert_called_once()
    call_url = mock_client.get.call_args[0][0]
    assert call_url.endswith("/WABA-GENERIC-001/product_catalogs")
    assert mock_client.post.called is False


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_expected_linked_false(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_response([
        {"id": "CAT-OTHER-ONLY", "name": "عطر ورد 100ml"},
    ])

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["connected"] is True
    assert out["expected_catalog_linked"] is False
    assert out["link_status"] == "mismatch"
    assert out["expected_catalog_id"] == "CAT-GENERIC-001"


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_waba_object_not_found_not_catalog_not_found(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=400,
        code=100,
        message="Unsupported get request. Object with ID 'WABA-BAD' does not exist",
    )

    out = get_waba_catalog_link_status(_db_with(_conn(waba_id="WABA-BAD")), 9)
    assert out["ok"] is False
    assert out["error"] == WABA_ERROR_NOT_FOUND
    assert out["error"] != "catalog_not_found"
    assert out["error_category"] != "catalog_not_found"
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None


@patch("services.meta_catalog_linking._probe_catalog_exists", return_value=True)
@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_waba_missing_permissions_is_inaccessible(mock_client_cls, _mock_probe):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=400,
        code=100,
        message=(
            "Unsupported get request. Object with ID 'WABA-BAD' does not exist, "
            "cannot be loaded due to missing permissions"
        ),
    )

    out = get_waba_catalog_link_status(_db_with(_conn(waba_id="WABA-BAD")), 9)
    assert out["ok"] is False
    assert out["error"] == WABA_ERROR_INACCESSIBLE
    assert out["error_category"] == WABA_ERROR_INACCESSIBLE
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None
    assert out["catalog_exists"] is True


@patch("services.meta_catalog_linking._probe_catalog_exists", return_value=False)
@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_catalog_probe_false_only_when_catalog_missing(mock_client_cls, _mock_probe):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=404,
        code=100,
        message="Unsupported get request. Object with ID 'WABA-BAD' does not exist",
    )

    out = get_waba_catalog_link_status(_db_with(_conn(waba_id="WABA-BAD")), 9)
    assert out["error"] == WABA_ERROR_NOT_FOUND
    assert out["expected_catalog_linked"] is None
    assert out["catalog_exists"] is False


@patch("services.meta_catalog_linking._probe_catalog_exists", return_value=None)
@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_waba_failure_catalog_probe_unknown(mock_client_cls, _mock_probe):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=400,
        code=100,
        message=(
            "Unsupported get request. Object with ID 'WABA-BAD' does not exist, "
            "cannot be loaded due to missing permissions"
        ),
    )

    out = get_waba_catalog_link_status(_db_with(_conn(waba_id="WABA-BAD")), 9)
    assert out["error"] == WABA_ERROR_INACCESSIBLE
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None
    assert out["catalog_exists"] is None


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_token_invalid_not_waba_inaccessible(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=400,
        code=190,
        message="Error validating access token: Session has expired",
    )

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["error"] == GRAPH_RESULT_TOKEN_INVALID
    assert out["error"] != WABA_ERROR_INACCESSIBLE
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_rate_limit_not_permanent_waba_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=400,
        code=4,
        message="Application request limit reached",
    )

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["error"] == GRAPH_RESULT_META_HTTP_ERROR
    assert out["error"] not in (WABA_ERROR_INACCESSIBLE, WABA_ERROR_NOT_FOUND)
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_graph_5xx_not_permanent_waba_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(
        status=503,
        code=2,
        message="Service temporarily unavailable",
    )

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["error"] == GRAPH_RESULT_META_HTTP_ERROR
    assert out["error"] not in (WABA_ERROR_INACCESSIBLE, WABA_ERROR_NOT_FOUND)
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_not_linked_empty_catalogs(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_response([])

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["connected"] is False
    assert out["expected_catalog_linked"] is False
    assert out["link_status"] == "not_linked"


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_graph_permission_error(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_error(status=403, code=200)

    out = get_waba_catalog_link_status(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["http_status"] == 403
    assert out["error"] == WABA_ERROR_INACCESSIBLE
    assert out["error"] != "catalog_not_found"
    assert out["link_status"] == "unknown"
    assert out["expected_catalog_linked"] is None


def test_link_status_no_graph_post_in_service():
    import services.meta_catalog_linking as mod

    source = inspect.getsource(mod)
    assert "product_catalogs" in source
    assert ".post(" not in source
    assert 'method="POST"' not in source


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_does_not_leak_token(mock_client_cls):
    secret = "EAAB-super-secret-token-value"
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_response([{"id": "CAT-GENERIC-001", "name": "حذاء"}])

    out = get_waba_catalog_link_status(_db_with(_conn(token=secret)), 9)
    dumped = repr(out)
    assert secret not in dumped
    assert "token_tail" not in dumped


@patch("services.meta_catalog_linking.httpx.Client")
def test_link_status_generic_neutral_tenant(mock_client_cls):
    """Platform-wide neutral merchant — not honey-store specific."""
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client.get.return_value = _graph_response([
        {"id": "CAT-SHOES-100", "name": "حذاء رياضي أبيض"},
    ])

    conn = SimpleNamespace(
        tenant_id=42,
        whatsapp_business_account_id="WABA-SHOES-42",
        meta_catalog_id="CAT-SHOES-100",
        provider="meta",
        connection_type="embedded",
        access_token="EAAB-neutral",
    )
    out = get_waba_catalog_link_status(_db_with(conn), 42)
    assert out["ok"] is True
    assert out["waba_id"] == "WABA-SHOES-42"
    assert out["expected_catalog_linked"] is True


def _dep_callable_names(route) -> set[str]:
    names: set[str] = set()
    deps = list(getattr(route, "dependant", None).dependencies) if getattr(
        route, "dependant", None,
    ) else []
    for dep in deps:
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
    return names


def _waba_link_route():
    return next(
        r for r in merchant_router.routes
        if "waba-link-status" in getattr(r, "path", "")
    )


def test_waba_link_status_route_requires_get_current_user():
    route = _waba_link_route()
    names = _dep_callable_names(route)
    assert "get_current_user" in names


def test_waba_link_status_route_uses_resolve_tenant_id():
    route = _waba_link_route()
    source = inspect.getsource(route.endpoint)
    assert "resolve_tenant_id" in source
    assert "Query" not in source or "tenant_id" not in source.split("Query", 1)[-1]

def test_catalog_graph_reads_use_bearer_not_query_token():
    """Regression: catalog relink reads must not put access_token in query params."""
    captured: list[dict] = []
    token = "EAAB-catalog-bearer-regression-token"

    def _fake_get(url, **kwargs):
        captured.append({"url": url, **kwargs})
        class _Resp:
            status_code = 200
            def json(self):
                return {"data": [{"id": "CAT-GENERIC-001", "name": "Generic"}]}
        return _Resp()

    mock_client = MagicMock()
    mock_client.get.side_effect = _fake_get
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    from services.meta_catalog_linking import _fetch_waba_product_catalogs

    catalogs, status, err = _fetch_waba_product_catalogs(
        "WABA-GENERIC-001", token, client=mock_client,
    )
    assert status == 200
    assert err is None
    assert len(catalogs) == 1
    assert len(captured) == 1
    call = captured[0]
    params = call.get("params") or {}
    assert "access_token" not in params
    assert token not in str(call.get("url"))
    headers = call.get("headers") or {}
    assert headers.get("Authorization") == f"Bearer {token}"
