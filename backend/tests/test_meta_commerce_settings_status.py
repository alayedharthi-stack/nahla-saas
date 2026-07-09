"""Tests for read-only WhatsApp commerce settings status."""

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
from services.meta_commerce_settings import (  # noqa: E402
    get_whatsapp_commerce_settings_status,
)


def _conn(
    *,
    phone_number_id: str = "PHONE-GENERIC-001",
    waba_id: str = "WABA-GENERIC-001",
    catalog_id: str = "CAT-GENERIC-001",
    token: str = "EAAB-generic-test-token",
    provider: str = "meta",
):
    return SimpleNamespace(
        tenant_id=9,
        phone_number_id=phone_number_id,
        whatsapp_business_account_id=waba_id,
        meta_catalog_id=catalog_id,
        provider=provider,
        connection_type="embedded",
        access_token=token,
        catalog_enabled=True,
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


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


def _install_graph_mock(mock_client_cls, responses: list[httpx.Response]):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client_cls.return_value.__exit__.return_value = False
    mock_client.get.side_effect = responses
    return mock_client


def test_commerce_status_no_connection():
    out = get_whatsapp_commerce_settings_status(_db_none(), 9)
    assert out["ok"] is False
    assert out["error"] == "connection_not_found"
    assert "connection" in out["missing"]
    assert out["commerce_settings"] is None


def test_commerce_status_missing_phone_number_id():
    conn = _conn(phone_number_id="")
    out = get_whatsapp_commerce_settings_status(_db_with(conn), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_phone_number_id"
    assert "phone_number_id" in out["missing"]
    assert out["commerce_settings"] is None


def test_commerce_status_missing_waba_id():
    conn = _conn(waba_id="")
    out = get_whatsapp_commerce_settings_status(_db_with(conn), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_waba_id"
    assert "waba_id" in out["missing"]


def test_commerce_status_missing_catalog_id():
    conn = _conn(catalog_id="")
    out = get_whatsapp_commerce_settings_status(_db_with(conn), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_catalog_id"
    assert "meta_catalog_id" in out["missing"]


@patch("services.meta_commerce_settings._select_graph_token", return_value={"token": "", "token_source": "none"})
def test_commerce_status_missing_token(_mock_token):
    out = get_whatsapp_commerce_settings_status(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["error"] == "missing_graph_token"
    assert "graph_token" in out["missing"]


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_reads_settings_success(mock_client_cls, mock_link_fetch):
    mock_client = _install_graph_mock(mock_client_cls, [
        _response(200, {
            "data": [{
                "id": "COMMERCE-001",
                "is_catalog_visible": True,
                "is_cart_enabled": True,
            }],
        }),
        _response(200, {
            "display_phone_number": "+966500000000",
            "verified_name": "متجر تجريبي عام",
            "id": "PHONE-GENERIC-001",
        }),
        _response(200, {
            "business_verification_status": "verified",
            "account_review_status": "APPROVED",
            "id": "WABA-GENERIC-001",
        }),
    ])
    mock_link_fetch.return_value = (
        [{"id": "CAT-GENERIC-001", "name": "حذاء رياضي أبيض"}],
        200,
        None,
    )

    out = get_whatsapp_commerce_settings_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["commerce_settings_found"] is True
    assert out["commerce_settings"] == {
        "id": "COMMERCE-001",
        "is_catalog_visible": True,
        "is_cart_enabled": True,
    }
    assert out["display_phone_number"] == "+966500000000"
    assert out["verified_name"] == "متجر تجريبي عام"
    assert out["waba_health"] == {
        "business_verification_status": "verified",
        "account_review_status": "APPROVED",
    }
    assert out["expected_catalog_linked"] is True
    assert out["http_status"] == 200
    assert mock_client.get.call_count == 3
    first_url = mock_client.get.call_args_list[0][0][0]
    assert first_url.endswith("/PHONE-GENERIC-001/whatsapp_commerce_settings")
    assert mock_client.post.called is False
    assert mock_client.patch.called is False


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_catalog_visible_false_cart_true(mock_client_cls, mock_link_fetch):
    _install_graph_mock(mock_client_cls, [
        _response(200, {
            "data": [{
                "id": "COMMERCE-002",
                "is_catalog_visible": False,
                "is_cart_enabled": True,
            }],
        }),
        _response(200, {"display_phone_number": "+966511111111", "id": "PHONE-GENERIC-001"}),
        _response(200, {"business_verification_status": "pending", "id": "WABA-GENERIC-001"}),
    ])
    mock_link_fetch.return_value = ([{"id": "CAT-GENERIC-001", "name": "قميص"}], 200, None)

    out = get_whatsapp_commerce_settings_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["commerce_settings"]["is_catalog_visible"] is False
    assert out["commerce_settings"]["is_cart_enabled"] is True


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_empty_data(mock_client_cls, mock_link_fetch):
    _install_graph_mock(mock_client_cls, [
        _response(200, {"data": []}),
        _response(200, {"id": "PHONE-GENERIC-001"}),
        _response(200, {"id": "WABA-GENERIC-001"}),
    ])
    mock_link_fetch.return_value = ([{"id": "CAT-GENERIC-001", "name": "عطر"}], 200, None)

    out = get_whatsapp_commerce_settings_status(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["commerce_settings_found"] is False
    assert out["commerce_settings"] is None
    assert out["error"] is None


@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_graph_permission_error(mock_client_cls):
    _install_graph_mock(mock_client_cls, [
        _response(403, {
            "error": {
                "code": 200,
                "type": "OAuthException",
                "message": "permission denied",
            },
        }),
    ])

    out = get_whatsapp_commerce_settings_status(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["http_status"] == 403
    assert out["error_code"] == 200
    assert out["error_category"] is not None


def test_commerce_status_no_graph_post_or_patch():
    source = inspect.getsource(get_whatsapp_commerce_settings_status)
    assert "whatsapp_commerce_settings" in source
    assert ".post(" not in source
    assert ".patch(" not in source


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_does_not_leak_token(mock_client_cls, mock_link_fetch):
    secret = "EAAB-super-secret-token-value"
    _install_graph_mock(mock_client_cls, [
        _response(200, {"data": [{"id": "C1", "is_catalog_visible": False, "is_cart_enabled": True}]}),
        _response(200, {"id": "PHONE-GENERIC-001"}),
        _response(200, {"id": "WABA-GENERIC-001"}),
    ])
    mock_link_fetch.return_value = ([{"id": "CAT-GENERIC-001", "name": "هدية"}], 200, None)

    out = get_whatsapp_commerce_settings_status(_db_with(_conn(token=secret)), 9)
    dumped = repr(out)
    assert secret not in dumped
    assert "token_tail" not in dumped


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


def _commerce_settings_route():
    return next(
        r for r in merchant_router.routes
        if "commerce-settings-status" in getattr(r, "path", "")
    )


def test_commerce_status_route_uses_jwt_tenant_only():
    route = _commerce_settings_route()
    names = _dep_callable_names(route)
    assert "get_current_user" in names
    source = inspect.getsource(route.endpoint)
    assert "resolve_tenant_id" in source
    assert "Query" not in source or "tenant_id" not in source.split("Query", 1)[-1]


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_neutral_tenant(mock_client_cls, mock_link_fetch):
    """Platform-wide neutral merchant — not category-specific."""
    _install_graph_mock(mock_client_cls, [
        _response(200, {
            "data": [{
                "id": "COMMERCE-SHOES",
                "is_catalog_visible": False,
                "is_cart_enabled": True,
            }],
        }),
        _response(200, {
            "display_phone_number": "+966522222222",
            "verified_name": "متجر تجريبي عام",
            "id": "PHONE-SHOES-42",
        }),
        _response(200, {
            "business_verification_status": "pending",
            "account_review_status": "APPROVED",
            "id": "WABA-SHOES-42",
        }),
    ])
    mock_link_fetch.return_value = (
        [{"id": "CAT-SHOES-100", "name": "حذاء رياضي أبيض"}],
        200,
        None,
    )

    conn = SimpleNamespace(
        tenant_id=42,
        phone_number_id="PHONE-SHOES-42",
        whatsapp_business_account_id="WABA-SHOES-42",
        meta_catalog_id="CAT-SHOES-100",
        provider="meta",
        connection_type="embedded",
        access_token="EAAB-neutral",
        catalog_enabled=True,
    )
    out = get_whatsapp_commerce_settings_status(_db_with(conn), 42)
    assert out["ok"] is True
    assert out["phone_number_id"] == "PHONE-SHOES-42"
    assert out["expected_catalog_linked"] is True
    assert out["commerce_settings"]["is_catalog_visible"] is False


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_commerce_status_does_not_change_catalog_enabled(mock_client_cls, mock_link_fetch):
    conn = _conn()
    _install_graph_mock(mock_client_cls, [
        _response(200, {"data": [{"id": "C1", "is_catalog_visible": True, "is_cart_enabled": True}]}),
        _response(200, {"id": "PHONE-GENERIC-001"}),
        _response(200, {"id": "WABA-GENERIC-001"}),
    ])
    mock_link_fetch.return_value = ([{"id": "CAT-GENERIC-001", "name": "x"}], 200, None)

    db = _db_with(conn)
    get_whatsapp_commerce_settings_status(db, 9)
    assert conn.catalog_enabled is True
    db.commit.assert_not_called()
