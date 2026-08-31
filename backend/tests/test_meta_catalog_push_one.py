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
        source="salla",
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


def _sibling_live_item(meta_id="META-SIBLING", retailer_id="398551325-591001", **overrides):
    item = {
        "id": meta_id,
        "retailer_id": retailer_id,
        "price": 12000,
        "currency": "SAR",
        "availability": "in stock",
        "url": "https://store.example/p/88001",
        "image_url": "https://cdn.example/parent.jpg",
    }
    item.update(overrides)
    return item


def _preview_fatal():
    report = _preview_ok()
    report["fatal"] = True
    report["warnings"] = ["missing_image_url"]
    report["payload"]["image_url"] = None
    return report


def _mock_db(parent=None, variant=None, conn=None, occupied=None):
    db = MagicMock()
    parent = parent or _parent()
    variant = variant or _variant()
    conn = conn or _conn()
    occupied_rows = occupied if occupied is not None else []

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "ProductVariant":
            filtered = MagicMock()
            filtered.first.return_value = variant
            filtered.all.return_value = list(getattr(parent, "variants", None) or [variant])
            q.filter.return_value = filtered
        elif name == "Product":
            filtered = MagicMock()
            filtered.first.return_value = parent
            filtered.all.return_value = list(occupied_rows)
            q.filter.return_value = filtered
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


def test_confirm_does_not_update_different_bound_meta_item():
    parent = _parent()
    parent.meta_item_id = "META-SIBLING"
    db = _mock_db(parent=parent)
    lookup_resp = httpx.Response(
        200,
        json={"data": [{"id": "META-ITEM-EXISTING", "retailer_id": "88001-591001"}]},
    )
    mock_client = MagicMock()
    mock_client.get.return_value = lookup_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "88001-591001", confirm=True)

    assert result["action"] == "block_ambiguous_sibling"
    assert result["error"] == "ambiguous_sibling"
    assert (result.get("lookup") or {}).get("reason") == "already_bound_other"
    assert mock_client.post.call_count == 0


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


def test_confirm_links_unique_canonical_sibling_without_create():
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
        fields = str(params.get("fields") or "")
        if "398551325-591001" in filt:
            assert "name" not in fields
            return httpx.Response(200, json={"data": [_sibling_live_item()]})
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "398551325", confirm=True)

    assert result["action"] == "link_canonical_sibling"
    assert result["ok"] is True
    assert result["error"] is None
    assert result["meta_product_id"] == "META-SIBLING"
    assert mock_client.post.call_count == 0
    lookup = result.get("lookup") or {}
    assert lookup.get("identity_class") == "EXISTING_CANONICAL_SIBLING"
    assert lookup.get("sibling_retailer_id") == "398551325-591001"


def test_confirm_blocks_when_multiple_canonical_siblings():
    parent = _parent()
    parent.external_id = "398551325"
    v1 = _variant("398551325-591001")
    v1.salla_variant_id = "591001"
    v2 = _variant("398551325-591002")
    v2.id = 502
    v2.salla_variant_id = "591002"
    default = _variant("398551325")
    default.salla_variant_id = ""
    parent.variants = [v1, v2, default]
    db = _mock_db(parent=parent, variant=default)

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        filt = str(params.get("filter") or "")
        if "398551325-591001" in filt:
            return httpx.Response(
                200,
                json={"data": [_sibling_live_item(meta_id="META-A", retailer_id="398551325-591001")]},
            )
        if "398551325-591002" in filt:
            return httpx.Response(
                200,
                json={"data": [_sibling_live_item(meta_id="META-B", retailer_id="398551325-591002")]},
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

    assert result["action"] == "block_ambiguous_sibling"
    assert result["error"] == "ambiguous_sibling"
    assert (result.get("lookup") or {}).get("reason") == "multiple_siblings"
    assert result["ok"] is False
    assert mock_client.post.call_count == 0


def test_confirm_blocks_when_meta_item_occupied_by_other_active_row():
    parent = _parent()
    parent.external_id = "398551325"
    sibling = _variant("398551325-591001")
    sibling.salla_variant_id = "591001"
    default = _variant("398551325")
    default.salla_variant_id = ""
    parent.variants = [sibling, default]
    other = SimpleNamespace(
        id=777, tenant_id=9, meta_item_id="META-SIBLING", catalog_status="active",
    )
    db = _mock_db(parent=parent, variant=default, occupied=[other])

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        filt = str(params.get("filter") or "")
        if "398551325-591001" in filt:
            return httpx.Response(200, json={"data": [_sibling_live_item()]})
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "398551325", confirm=True)

    assert result["action"] == "block_ambiguous_sibling"
    assert result["error"] == "ambiguous_sibling"
    assert (result.get("lookup") or {}).get("reason") == "foreign_meta_item"
    assert mock_client.post.call_count == 0


def test_confirm_blocks_when_sibling_price_mismatches():
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
                json={"data": [_sibling_live_item(price=999)]},
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

    assert result["action"] == "block_ambiguous_sibling"
    assert result["error"] == "ambiguous_sibling"
    assert (result.get("lookup") or {}).get("reason") == "content_mismatch"
    assert "price" in ((result.get("lookup") or {}).get("content_mismatches") or [])
    assert mock_client.post.call_count == 0


def test_confirm_blocks_when_live_item_lineage_mismatches():
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
                json={"data": [_sibling_live_item(retailer_id="77001-B")]},
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

    assert result["action"] == "block_ambiguous_sibling"
    assert (result.get("lookup") or {}).get("reason") == "lineage_mismatch"
    assert mock_client.post.call_count == 0


def test_canonical_sibling_link_is_idempotent_and_does_not_post():
    parent = _parent()
    parent.external_id = "398551325"
    parent.meta_item_id = "META-SIBLING"
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
            return httpx.Response(200, json={"data": [_sibling_live_item()]})
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                first = push_one_meta_catalog_item(db, 9, "398551325", confirm=True)
                second = push_one_meta_catalog_item(db, 9, "398551325", confirm=True)

    assert first["action"] == "link_canonical_sibling"
    assert second["action"] == "link_canonical_sibling"
    assert first["meta_product_id"] == second["meta_product_id"] == "META-SIBLING"
    assert (second.get("lookup") or {}).get("idempotent") is True
    assert mock_client.post.call_count == 0


def test_confirm_creates_when_only_bare_parent_rid_exists():
    parent = _parent()
    parent.variants = [_variant("88001-591001")]
    variant = _variant("nahla_v_501")
    variant.salla_variant_id = ""
    db = _mock_db(parent=parent, variant=variant)
    create_resp = httpx.Response(200, json={"id": "META-NEW-MISSING"})

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        filt = str(params.get("filter") or "")
        if "88001-591001" in filt:
            return httpx.Response(200, json={"data": []})
        if filt.endswith('eq":"88001"}') or "88001" in filt:
            return httpx.Response(
                200,
                json={"data": [{"id": "META-LEGACY", "retailer_id": "88001"}]},
            )
        return httpx.Response(200, json={"data": []})

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client.post.return_value = create_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("services.meta_catalog_push.preview_meta_variant_payload", return_value=_preview_ok()):
        with patch("services.meta_catalog_push.select_catalog_graph_token", return_value={"token": "tok"}):
            with patch("services.meta_catalog_push.httpx.Client", return_value=mock_client):
                result = push_one_meta_catalog_item(db, 9, "nahla_v_501", confirm=True)

    assert result["action"] == "create"
    assert result["ok"] is True
    assert result["meta_product_id"] == "META-NEW-MISSING"
    assert mock_client.post.call_count == 1


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
