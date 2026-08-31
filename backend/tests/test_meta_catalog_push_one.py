"""Tests for guarded one-item Meta catalog push."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.meta_catalog_push import (  # noqa: E402
    MetaCatalogPushError,
    existing_identity_retailer_id,
    parent_would_create_in_meta,
    push_one_meta_catalog_item,
)


def _conn():
    return SimpleNamespace(
        tenant_id=9,
        meta_catalog_id="CAT-GENERIC-001",
        provider="meta",
        connection_type="direct",
        access_token="EAAB-test-token",
    )


def _parent():
    return SimpleNamespace(
        id=101,
        tenant_id=9,
        title="قميص قطني أزرق",
        external_id="88001",
        description="وصف عام للمنتج",
        extra_metadata={
            "image_url": "https://cdn.example/parent.jpg",
            "product_url": "https://store.example/p/88001",
            "currency": "SAR",
        },
    )


def _variant(retailer_id: str = "88001-591001"):
    return SimpleNamespace(
        id=501,
        tenant_id=9,
        product_id=101,
        salla_variant_id="591001",
        retailer_id=retailer_id,
        price="120",
        currency="SAR",
        stock_quantity=3,
        in_stock=True,
        option_summary="M",
        options={"option_value_ids": ["90001"]},
        image_url=None,
        extra_metadata={"sale_price": "120.0", "regular_price": "150.0"},
    )


def _preview_ok():
    return {
        "payload": {
            "retailer_id": "88001-591001",
            "name": "قميص قطني أزرق - M",
            "description": "وصف عام للمنتج",
            "image_url": "https://cdn.example/parent.jpg",
            "url": "https://store.example/p/88001",
            "price": 12000,
            "currency": "SAR",
            "availability": "in stock",
        },
        "warnings": [],
        "fatal": False,
    }


def _preview_fatal():
    report = _preview_ok()
    report["fatal"] = True
    report["warnings"] = ["missing_image_url"]
    report["payload"]["image_url"] = None
    return report


def _mock_db(parent=None, variant=None, conn=None):
    db = MagicMock()
    parent = parent or _parent()
    variant = variant or _variant()
    conn = conn or _conn()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "ProductVariant":
            q.filter.return_value.first.return_value = variant
        elif name == "Product":
            q.filter.return_value.first.return_value = parent
        elif name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        return q

    db.query.side_effect = _query
    return db


def test_dry_run_builds_payload_without_httpx():
    db = _mock_db()
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.httpx.Client") as client_cls:
            result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=False)
    assert result["action"] == "dry_run"
    assert result["dry_run"] is True
    assert result["ok"] is True
    assert result["payload"]["price"] == 12000
    client_cls.assert_not_called()


def test_confirm_get_empty_then_create():
    db = _mock_db()
    lookup_resp = httpx.Response(200, json={"data": []})
    create_resp = httpx.Response(200, json={"id": "META-ITEM-NEW"})

    mock_client = MagicMock()
    mock_client.get.return_value = lookup_resp
    mock_client.post.return_value = create_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)

    assert result["action"] == "create"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-ITEM-NEW"
    assert mock_client.get.call_count >= 1
    assert mock_client.post.call_count == 1
    get_params = mock_client.get.call_args.kwargs.get("params") or {}
    get_headers = mock_client.get.call_args.kwargs.get("headers") or {}
    assert "access_token" not in get_params
    assert get_headers.get("Authorization") == "Bearer tok"
    post_url = mock_client.post.call_args.args[0]
    assert "/CAT-GENERIC-001/products" in post_url
    post_body = mock_client.post.call_args.kwargs.get("data") or mock_client.post.call_args.args[1]
    post_headers = mock_client.post.call_args.kwargs.get("headers") or {}
    assert post_body["currency"] == "SAR"
    assert post_body["price"] == 12000
    assert "access_token" not in post_body
    assert post_headers.get("Authorization") == "Bearer tok"
    assert "sale_price" not in post_body
    assert "regular_price" not in post_body


def test_confirm_get_existing_then_update():
    db = _mock_db()
    lookup_resp = httpx.Response(
        200,
        json={"data": [{"id": "META-ITEM-EXISTING", "retailer_id": "88001-591001", "name": "old"}]},
    )
    update_resp = httpx.Response(200, json={"id": "META-ITEM-EXISTING"})

    mock_client = MagicMock()
    mock_client.get.return_value = lookup_resp
    mock_client.post.return_value = update_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)

    assert result["action"] == "update"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-ITEM-EXISTING"
    assert mock_client.get.call_count == 1
    assert mock_client.post.call_count == 1
    get_params = mock_client.get.call_args.kwargs.get("params") or {}
    get_headers = mock_client.get.call_args.kwargs.get("headers") or {}
    assert "access_token" not in get_params
    assert get_headers.get("Authorization") == "Bearer tok"
    post_url = mock_client.post.call_args.args[0]
    assert post_url.endswith("/META-ITEM-EXISTING")
    post_body = mock_client.post.call_args.kwargs.get("data") or mock_client.post.call_args.args[1]
    post_headers = mock_client.post.call_args.kwargs.get("headers") or {}
    assert post_body["currency"] == "SAR"
    assert post_body["price"] == 12000
    assert "access_token" not in post_body
    assert post_headers.get("Authorization") == "Bearer tok"


def test_fatal_preview_does_not_call_graph():
    db = _mock_db()
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_fatal()):
        with patch("services.meta_catalog_push.httpx.Client") as client_cls:
            result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
    assert result["error"] == "preview_fatal"
    assert result["ok"] is False
    client_cls.assert_not_called()


def test_missing_catalog_id_fails_before_post():
    conn = _conn()
    conn.meta_catalog_id = ""
    db = _mock_db(conn=conn)
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.httpx.Client") as client_cls:
            with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
                try:
                    push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
                    assert False, "expected MetaCatalogPushError"
                except MetaCatalogPushError as exc:
                    assert exc.code == "catalog_id_missing"
    client_cls.assert_not_called()


def test_missing_token_fails_before_post():
    db = _mock_db()
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.httpx.Client") as client_cls:
            with patch(
                "services.meta_catalog_push.select_catalog_graph_token",
                return_value={"token": "", "error": "missing_graph_token"},
            ):
                try:
                    push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
                    assert False, "expected MetaCatalogPushError"
                except MetaCatalogPushError as exc:
                    assert exc.code == "access_token_missing"
    client_cls.assert_not_called()


def test_catalog_unreadable_is_permission_denied_not_success():
    db = _mock_db()
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.httpx.Client") as client_cls:
            with patch(
                "services.meta_catalog_push.select_catalog_graph_token",
                return_value={"token": None, "error": "catalog_not_readable", "probes": []},
            ):
                try:
                    push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
                    assert False, "expected MetaCatalogPushError"
                except MetaCatalogPushError as exc:
                    assert exc.code == "catalog_permission_denied"
    client_cls.assert_not_called()


def test_confirm_skips_create_when_sibling_hyphenated_rid_exists():
    parent = _parent()
    parent.external_id = "398551325"
    sibling = _variant("398551325-591001")
    sibling.salla_variant_id = "591001"
    default = _variant("398551325")
    default.salla_variant_id = ""
    parent.variants = [sibling, default]
    db = _mock_db(parent=parent, variant=default)

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        filt = str(params.get("filter") or "")
        if "398551325-591001" in filt:
            return httpx.Response(
                200,
                json={"data": [{"id": "META-SIBLING", "retailer_id": "398551325-591001"}]},
            )
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "398551325", confirm=True)

    assert result["action"] == "skip_existing"
    assert result["error"] == "existing_catalog_identity"
    assert result["ok"] is False
    assert mock_client.post.call_count == 0
    assert (result.get("lookup") or {}).get("identity_class") == "EXISTING_EXACT"


def test_confirm_skips_create_when_legacy_parent_rid_exists():
    parent = _parent()
    parent.variants = [_variant("88001-591001")]
    variant = _variant("nahla_v_501")
    variant.salla_variant_id = ""
    db = _mock_db(parent=parent, variant=variant)

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        filt = str(params.get("filter") or "")
        if "nahla_v_501" in filt:
            return httpx.Response(200, json={"data": []})
        if "88001" in filt:
            return httpx.Response(
                200,
                json={"data": [{"id": "META-LEGACY", "retailer_id": "88001"}]},
            )
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "nahla_v_501", confirm=True)

    assert result["action"] == "skip_existing"
    assert result["error"] == "existing_catalog_identity"
    assert result["ok"] is False
    assert result["meta_product_id"] == "META-LEGACY"
    assert mock_client.post.call_count == 0


def test_confirm_creates_when_no_exact_or_legacy_identity():
    parent = _parent()
    parent.external_id = "99001"
    parent.meta_retailer_id = None
    parent.canonical_retailer_id = None
    parent.source_external_id = None
    parent.variants = []
    variant = _variant("99001-1")
    variant.salla_variant_id = "1"
    db = _mock_db(parent=parent, variant=variant)
    lookup_resp = httpx.Response(200, json={"data": []})
    create_resp = httpx.Response(200, json={"id": "META-NEW-MISSING"})
    mock_client = MagicMock()
    mock_client.get.return_value = lookup_resp
    mock_client.post.return_value = create_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "99001-1", confirm=True)

    assert result["action"] == "create"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-NEW-MISSING"
    assert mock_client.post.call_count == 1


def test_identity_classifier_binds_sibling_not_title():
    parent = _parent()
    parent.title = "تنورة طويلة"
    sibling = _variant("88001-591001")
    default = _variant("88001")
    parent.variants = [sibling, default]
    live = {"88001-591001"}
    assert existing_identity_retailer_id(parent, live, current_rid="88001") == "88001-591001"
    assert parent_would_create_in_meta(parent, live) is False
    assert parent_would_create_in_meta(parent, live) is False  # idempotent
    assert existing_identity_retailer_id(parent, {"unrelated-rid"}, current_rid="88001") is None
    assert parent_would_create_in_meta(parent, {"unrelated-rid"}) is True
    keys = []
    from services.meta_catalog_identity import legacy_identity_retailer_ids
    keys = legacy_identity_retailer_ids(parent, exclude_rid="88001")
    assert parent.title not in keys


def test_lookup_filter_is_exact_retailer_id_not_name():
    db = _mock_db()
    captured = []

    def _get(url, **kwargs):
        captured.append(kwargs.get("params") or {})
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.post.return_value = httpx.Response(200, json={"id": "META-NEW"})
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
    assert captured
    filt = str(captured[0].get("filter") or "")
    assert "retailer_id" in filt
    assert "eq" in filt
    assert "name" not in filt


def test_load_variant_requires_retailer_id():
    db = MagicMock()
    from services.meta_catalog_push import load_variant_for_push  # noqa: PLC0415

    try:
        load_variant_for_push(db, 9, retailer_id="")
        assert False, "expected MetaCatalogPushError"
    except MetaCatalogPushError as exc:
        assert exc.code == "retailer_id_missing"
