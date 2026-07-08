"""Tests for WhatsApp commerce settings enable action."""

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
    enable_whatsapp_catalog_visibility,
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


def _response(method: str, status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request(method, "https://graph.facebook.com/test"),
    )


def _install_graph_mock(mock_client_cls, *, get_responses=None, post_response=None):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_client_cls.return_value.__exit__.return_value = False
    if get_responses is not None:
        mock_client.get.side_effect = get_responses
    if post_response is not None:
        mock_client.post.return_value = post_response
    return mock_client


def _linked_catalogs():
    return ([{"id": "CAT-GENERIC-001", "name": "حذاء رياضي أبيض"}], 200, None)


def _waba_health_response():
    return _response(
        "GET",
        200,
        {
            "business_verification_status": "pending",
            "account_review_status": "APPROVED",
            "id": "WABA-GENERIC-001",
        },
    )


def test_enable_missing_phone_number_id_no_post():
    conn = _conn(phone_number_id="")
    db = _db_with(conn)
    with patch("services.meta_commerce_settings.httpx.Client") as mock_client_cls:
        out = enable_whatsapp_catalog_visibility(db, 9)
        mock_client_cls.assert_not_called()
    assert out["ok"] is False
    assert out["error"] == "missing_phone_number_id"
    assert out["action"] == "enable_catalog_visibility"


def test_enable_missing_waba_id_no_post():
    conn = _conn(waba_id="")
    with patch("services.meta_commerce_settings.httpx.Client") as mock_client_cls:
        out = enable_whatsapp_catalog_visibility(_db_with(conn), 9)
        mock_client_cls.assert_not_called()
    assert out["ok"] is False
    assert out["error"] == "missing_waba_id"


def test_enable_missing_catalog_id_no_post():
    conn = _conn(catalog_id="")
    with patch("services.meta_commerce_settings.httpx.Client") as mock_client_cls:
        out = enable_whatsapp_catalog_visibility(_db_with(conn), 9)
        mock_client_cls.assert_not_called()
    assert out["ok"] is False
    assert out["error"] == "missing_catalog_id"


@patch("services.meta_commerce_settings._select_graph_token", return_value={"token": "", "token_source": "none"})
def test_enable_missing_token_no_post(_mock_token):
    with patch("services.meta_commerce_settings.httpx.Client") as mock_client_cls:
        out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
        mock_client_cls.assert_not_called()
    assert out["ok"] is False
    assert out["error"] == "missing_graph_token"


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_rejects_when_catalog_not_linked_no_post(mock_client_cls, mock_link_fetch):
    mock_client = _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
        ],
    )
    mock_link_fetch.return_value = ([{"id": "CAT-OTHER-999", "name": "other"}], 200, None)

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["error"] == "catalog_not_linked"
    assert mock_client.post.called is False


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_idempotent_when_already_visible_and_cart_enabled(mock_client_cls, mock_link_fetch):
    mock_client = _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {
                "data": [{
                    "id": "COMMERCE-001",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
            _waba_health_response(),
        ],
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["meta_update"] == {"skipped": True, "reason": "already_enabled"}
    assert out["after"]["commerce_settings"]["is_catalog_visible"] is True
    assert mock_client.post.called is False


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_posts_query_params_visible_true_cart_true(mock_client_cls, mock_link_fetch):
    mock_client = _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {
                "data": [{
                    "id": "COMMERCE-NEW",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert mock_client.post.call_count == 1
    post_call = mock_client.post.call_args
    assert post_call[0][0].endswith("/PHONE-GENERIC-001/whatsapp_commerce_settings")
    params = post_call[1]["params"]
    assert params["is_catalog_visible"] == "true"
    assert params["is_cart_enabled"] == "true"
    assert "access_token" in params
    assert out["meta_update"] == {"http_status": 200, "success": True}


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_handles_empty_settings_before_then_readback_success(
    mock_client_cls,
    mock_link_fetch,
):
    mock_client = _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {
                "data": [{
                    "id": "COMMERCE-INIT",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["before"]["commerce_settings_found"] is False
    assert out["after"]["commerce_settings_found"] is True
    assert out["after"]["commerce_settings"]["is_catalog_visible"] is True


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_post_success_readback_empty_returns_warning(mock_client_cls, mock_link_fetch):
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {"data": []}),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is True
    assert out["meta_update"]["success"] is True
    assert out["after"]["commerce_settings_found"] is False
    assert "not returned yet" in (out.get("warning") or "")


@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_graph_permission_error(mock_client_cls):
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 403, {
                "error": {
                    "code": 200,
                    "type": "OAuthException",
                    "message": "permission denied",
                },
            }),
        ],
    )

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["ok"] is False
    assert out["error_code"] == 200
    assert mock_client_cls.return_value.__enter__.return_value.post.called is False


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_no_db_commit(mock_client_cls, mock_link_fetch):
    conn = _conn()
    db = _db_with(conn)
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {
                "data": [{
                    "id": "C1",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    enable_whatsapp_catalog_visibility(db, 9)
    db.commit.assert_not_called()


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_does_not_change_catalog_enabled(mock_client_cls, mock_link_fetch):
    conn = _conn()
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {
                "data": [{
                    "id": "C1",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    enable_whatsapp_catalog_visibility(_db_with(conn), 9)
    assert conn.catalog_enabled is True


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_does_not_leak_token(mock_client_cls, mock_link_fetch):
    secret = "EAAB-super-secret-token-value"
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {"data": []}),
            _waba_health_response(),
            _response("GET", 200, {
                "data": [{
                    "id": "C1",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
        ],
        post_response=_response("POST", 200, {"success": True}),
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn(token=secret)), 9)
    dumped = repr(out)
    assert secret not in dumped


@patch("services.meta_commerce_settings._fetch_waba_product_catalogs")
@patch("services.meta_commerce_settings.httpx.Client")
def test_enable_business_verification_pending_warning(mock_client_cls, mock_link_fetch):
    _install_graph_mock(
        mock_client_cls,
        get_responses=[
            _response("GET", 200, {
                "data": [{
                    "id": "COMMERCE-001",
                    "is_catalog_visible": True,
                    "is_cart_enabled": True,
                }],
            }),
            _waba_health_response(),
        ],
    )
    mock_link_fetch.return_value = _linked_catalogs()

    out = enable_whatsapp_catalog_visibility(_db_with(_conn()), 9)
    assert out["warning"] == "business_verification_pending_may_affect_customer_visibility"


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


def _commerce_settings_enable_route():
    return next(
        r for r in merchant_router.routes
        if getattr(r, "path", "").endswith("/commerce-settings")
        and "POST" in getattr(r, "methods", set())
    )


def test_enable_route_uses_jwt_tenant_only():
    route = _commerce_settings_enable_route()
    names = _dep_callable_names(route)
    assert "get_current_user" in names
    source = inspect.getsource(route.endpoint)
    assert "resolve_tenant_id" in source
    assert "Query" not in source or "tenant_id" not in source.split("Query", 1)[-1]


def test_enable_no_patch():
    source = inspect.getsource(enable_whatsapp_catalog_visibility)
    assert ".patch(" not in source
