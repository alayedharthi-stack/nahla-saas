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
            "price": 120.0,
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
    assert result["payload"]["price"] == 120.0
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
        with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)

    assert result["action"] == "create"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-ITEM-NEW"
    assert mock_client.get.call_count == 1
    assert mock_client.post.call_count == 1
    post_url = mock_client.post.call_args.args[0]
    assert "/CAT-GENERIC-001/products" in post_url
    post_body = mock_client.post.call_args.kwargs.get("data") or mock_client.post.call_args.args[1]
    assert post_body["currency"] == "SAR"
    assert post_body["price"] == 120.0
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
        with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)

    assert result["action"] == "update"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-ITEM-EXISTING"
    assert mock_client.get.call_count == 1
    assert mock_client.post.call_count == 1
    post_url = mock_client.post.call_args.args[0]
    assert post_url.endswith("/META-ITEM-EXISTING")
    post_body = mock_client.post.call_args.kwargs.get("data") or mock_client.post.call_args.args[1]
    assert post_body["currency"] == "SAR"
    assert post_body["price"] == 120.0


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
            with patch("services.meta_catalog_push._select_graph_token", return_value={"token": ""}):
                try:
                    push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)
                    assert False, "expected MetaCatalogPushError"
                except MetaCatalogPushError as exc:
                    assert exc.code == "access_token_missing"
    client_cls.assert_not_called()


def test_load_variant_requires_retailer_id():
    db = MagicMock()
    from services.meta_catalog_push import load_variant_for_push  # noqa: PLC0415

    try:
        load_variant_for_push(db, 9, retailer_id="")
        assert False, "expected MetaCatalogPushError"
    except MetaCatalogPushError as exc:
        assert exc.code == "retailer_id_missing"
