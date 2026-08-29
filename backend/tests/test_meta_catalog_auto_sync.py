"""Meta catalog auto-sync: token fallback, WABA bind, reconnect reconcile."""
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

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
    is_meta_export_eligible,
)
from services.meta_catalog_access import (  # noqa: E402
    ERROR_CATALOG_NOT_READABLE,
    select_catalog_graph_token,
)
from services.meta_catalog_import import (  # noqa: E402
    _TOKEN_SOURCE_MERCHANT_OAUTH,
    _TOKEN_SOURCE_PLATFORM_SYSTEM,
)
from services.meta_catalog_linking import (  # noqa: E402
    link_waba_to_catalog,
    share_catalog_with_business,
)
from services.meta_catalog_reconnect import (  # noqa: E402
    _eligible_product_ids,
    bind_current_waba_to_merchant_catalog,
    catalog_config_changes_require_reconcile,
    reconcile_meta_catalog_after_whatsapp_change,
)


def _graph_ok(payload, status=200):
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


def _graph_denied():
    return httpx.Response(
        400,
        json={"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request"}},
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


def _native(**overrides):
    base = dict(
        id=501,
        tenant_id=9,
        title="قميص قطني أزرق",
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _conn(**overrides):
    base = dict(
        tenant_id=9,
        meta_catalog_id="CAT-GENERIC-001",
        whatsapp_business_account_id="WABA-GENERIC-001",
        catalog_enabled=True,
        extra_metadata={},
        provider="meta",
        connection_type="embedded",
        access_token="EAAB-merchant",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(conn, products=None):
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        elif name == "Product":
            q.filter.return_value.all.return_value = list(products or [])
        else:
            q.filter.return_value.first.return_value = None
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def test_merchant_catalog_100_33_falls_back_to_platform_token():
    conn = _conn()
    client = MagicMock()

    def _get(url, params=None, headers=None):
        token = (headers or {}).get("Authorization", "").split()[-1]
        if token == "EAAB-merchant":
            return _graph_denied()
        return _graph_ok({
            "id": "CAT-GENERIC-001",
            "name": "متجر تجريبي عام",
            "product_count": 43,
            "business": {"id": "BM-PLATFORM"},
        })

    client.get.side_effect = _get
    with patch(
        "services.meta_catalog_access._select_graph_token",
        return_value={"token": "EAAB-merchant", "token_source": _TOKEN_SOURCE_MERCHANT_OAUTH},
    ):
        with patch("services.meta_catalog_access.WA_TOKEN", "EAAB-platform"):
            pick = select_catalog_graph_token(conn, "CAT-GENERIC-001", client=client)

    assert pick["catalog_readable"] is True
    assert pick["token"] == "EAAB-platform"
    assert pick["token_source"] == _TOKEN_SOURCE_PLATFORM_SYSTEM
    assert pick["catalog"]["product_count"] == 43
    assert client.get.call_count == 2


def test_merchant_readable_catalog_does_not_use_platform_token():
    conn = _conn()
    client = MagicMock()
    client.get.return_value = _graph_ok({
        "id": "CAT-GENERIC-001",
        "name": "متجر تجريبي عام",
        "product_count": 8,
        "business": {"id": "BM-MERCHANT"},
    })
    with patch(
        "services.meta_catalog_access._select_graph_token",
        return_value={"token": "EAAB-merchant", "token_source": _TOKEN_SOURCE_MERCHANT_OAUTH},
    ):
        with patch("services.meta_catalog_access.WA_TOKEN", "EAAB-platform"):
            pick = select_catalog_graph_token(conn, "CAT-GENERIC-001", client=client)

    assert pick["token"] == "EAAB-merchant"
    assert pick["token_source"] == _TOKEN_SOURCE_MERCHANT_OAUTH
    assert client.get.call_count == 1


def test_neither_token_can_read_catalog_is_not_silent_success():
    conn = _conn()
    client = MagicMock()
    client.get.return_value = _graph_denied()
    with patch(
        "services.meta_catalog_access._select_graph_token",
        return_value={"token": "EAAB-merchant", "token_source": _TOKEN_SOURCE_MERCHANT_OAUTH},
    ):
        with patch("services.meta_catalog_access.WA_TOKEN", "EAAB-platform"):
            pick = select_catalog_graph_token(conn, "CAT-GENERIC-001", client=client)

    assert pick["token"] is None
    assert pick["catalog_readable"] is False
    assert pick["error"] == ERROR_CATALOG_NOT_READABLE


def test_already_linked_waba_does_not_post():
    client = MagicMock()
    with patch(
        "services.meta_catalog_linking._fetch_waba_product_catalogs",
        return_value=([{"id": "CAT-GENERIC-001", "name": "متجر تجريبي عام"}], 200, None),
    ) as fetch:
        with patch("services.meta_catalog_linking._graph_json") as post:
            out = link_waba_to_catalog(
                "WABA-GENERIC-001", "CAT-GENERIC-001", "tok", confirm=True, client=client,
            )
    assert out["ok"] is True
    assert out["already_linked"] is True
    assert out["action"] == "already_linked"
    fetch.assert_called()
    post.assert_not_called()


def test_link_posts_then_verifies_get():
    fetches = [
        ([], 200, None),
        ([{"id": "CAT-GENERIC-001", "name": "متجر تجريبي عام"}], 200, None),
    ]

    def _fetch(*_a, **_k):
        return fetches.pop(0)

    with patch("services.meta_catalog_linking._fetch_waba_product_catalogs", side_effect=_fetch):
        with patch(
            "services.meta_catalog_linking._graph_json",
            return_value={"ok": True, "http_status": 200, "error": None, "body": {"success": True}},
        ) as post:
            out = link_waba_to_catalog(
                "WABA-GENERIC-001", "CAT-GENERIC-001", "tok", confirm=True,
            )
    assert out["ok"] is True
    assert out["already_linked"] is True
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "POST"
    assert args[1] == "WABA-GENERIC-001/product_catalogs"
    assert kwargs.get("data") == {"catalog_id": "CAT-GENERIC-001"}


def test_share_refuses_business_that_is_not_this_waba_owner():
    out = share_catalog_with_business(
        "CAT-GENERIC-001",
        "BM-OTHER-TENANT",
        "tok",
        confirm=True,
        allowed_business_id="BM-OWNER",
    )
    assert out["ok"] is False
    assert out["error"] == "business_id_not_waba_owner"


def test_catalog_disabled_skips_meta_writes():
    conn = _conn(catalog_enabled=False)
    db = _db(conn, products=[_native()])
    with patch("services.meta_catalog_reconnect.select_catalog_graph_token") as probe:
        with patch("services.meta_catalog_linking.link_waba_to_catalog") as link:
            out = bind_current_waba_to_merchant_catalog(db, 9, confirm=True)
    assert out["ok"] is True
    assert out["skipped"] is True
    assert out["error"] == "catalog_disabled"
    probe.assert_not_called()
    link.assert_not_called()


def test_missing_catalog_id_does_not_create_catalog():
    conn = _conn(meta_catalog_id="")
    db = _db(conn)
    with patch("services.meta_catalog_linking._graph_json") as graph:
        out = bind_current_waba_to_merchant_catalog(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == "missing_catalog_id"
    assert out["catalog_reused"] is False
    graph.assert_not_called()


def test_zero_eligible_products_still_binds():
    conn = _conn()
    db = _db(conn, products=[])
    with patch(
        "services.meta_catalog_reconnect.select_catalog_graph_token",
        return_value={
            "token": "EAAB-platform",
            "token_source": _TOKEN_SOURCE_PLATFORM_SYSTEM,
            "catalog": {"business_id": "BM-PLATFORM"},
        },
    ):
        with patch(
            "services.meta_catalog_reconnect._select_graph_token",
            return_value={"token": "EAAB-merchant"},
        ):
            with patch(
                "services.meta_catalog_reconnect.fetch_waba_owner_business_id",
                return_value={"ok": True, "business_id": "BM-MERCHANT"},
            ):
                with patch(
                    "services.meta_catalog_reconnect.share_catalog_with_business",
                    return_value={"ok": True, "action": "share"},
                ):
                    with patch(
                        "services.meta_catalog_reconnect.link_waba_to_catalog",
                        return_value={
                            "ok": True,
                            "already_linked": False,
                            "link_status": "linked",
                            "linked_catalog_ids": ["CAT-GENERIC-001"],
                            "action": "link",
                        },
                    ) as link:
                        with patch(
                            "services.native_meta_sync_orchestrator.attempt_native_meta_sync",
                        ) as sync:
                            out = reconcile_meta_catalog_after_whatsapp_change(
                                db, 9, confirm=True,
                            )
    assert out["ok"] is True
    assert out["product_ids"] == []
    link.assert_called_once()
    sync.assert_not_called()


def test_reconnect_retries_synced_eligible_products(monkeypatch):
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    conn = _conn()
    products = [_native(id=501), _native(id=502, title="حذاء رياضي أبيض")]
    db = _db(conn, products=products)
    with patch(
        "services.meta_catalog_reconnect.select_catalog_graph_token",
        return_value={
            "token": "EAAB-platform",
            "token_source": _TOKEN_SOURCE_PLATFORM_SYSTEM,
            "catalog": {"business_id": "BM-MERCHANT"},
        },
    ):
        with patch(
            "services.meta_catalog_reconnect._select_graph_token",
            return_value={"token": "EAAB-merchant"},
        ):
            with patch(
                "services.meta_catalog_reconnect.fetch_waba_owner_business_id",
                return_value={"ok": True, "business_id": "BM-MERCHANT"},
            ):
                with patch(
                    "services.meta_catalog_reconnect.link_waba_to_catalog",
                    return_value={
                        "ok": True,
                        "already_linked": True,
                        "link_status": "linked",
                        "action": "already_linked",
                        "linked_catalog_ids": ["CAT-GENERIC-001"],
                    },
                ):
                    with patch(
                        "services.native_meta_sync_orchestrator.attempt_native_meta_sync",
                        return_value={"ok": True},
                    ) as sync:
                        out = reconcile_meta_catalog_after_whatsapp_change(
                            db, 9, confirm=True,
                        )
    assert out["ok"] is True
    assert out["synced"] == 2
    assert sync.call_count == 2
    assert sync.call_args.kwargs.get("allow_synced_retry") is True


def test_permission_failure_is_persisted_not_ok():
    conn = _conn()
    db = _db(conn, products=[_native()])
    with patch(
        "services.meta_catalog_reconnect.select_catalog_graph_token",
        return_value={"token": None, "error": ERROR_CATALOG_NOT_READABLE, "probes": []},
    ):
        out = bind_current_waba_to_merchant_catalog(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_NOT_READABLE
    assert conn.extra_metadata["meta_catalog_bind"]["error"] == ERROR_CATALOG_NOT_READABLE
    assert "token" not in conn.extra_metadata["meta_catalog_bind"]


def test_eligible_ids_are_tenant_scoped_and_skip_external_meta():
    rows = [
        _native(id=1, tenant_id=9),
        _native(id=2, tenant_id=10, title="عطر ورد 100ml"),
        _native(
            id=3,
            tenant_id=9,
            source="salla",
            ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        ),
        _native(
            id=4,
            tenant_id=9,
            source="meta",
            ownership_mode=OWNERSHIP_META_READONLY,
        ),
    ]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    ids = _eligible_product_ids(db, 9)
    assert ids == [1, 3]
    assert is_meta_export_eligible(rows[2]) is False
    assert is_meta_export_eligible(rows[3]) is False


def test_rerun_bind_is_idempotent_already_linked():
    conn = _conn()
    db = _db(conn)
    with patch(
        "services.meta_catalog_reconnect.select_catalog_graph_token",
        return_value={
            "token": "EAAB-platform",
            "token_source": _TOKEN_SOURCE_PLATFORM_SYSTEM,
            "catalog": {"business_id": "BM-MERCHANT"},
        },
    ):
        with patch(
            "services.meta_catalog_reconnect._select_graph_token",
            return_value={"token": "EAAB-merchant"},
        ):
            with patch(
                "services.meta_catalog_reconnect.fetch_waba_owner_business_id",
                return_value={"ok": True, "business_id": "BM-MERCHANT"},
            ):
                with patch(
                    "services.meta_catalog_reconnect.link_waba_to_catalog",
                    return_value={
                        "ok": True,
                        "already_linked": True,
                        "action": "already_linked",
                        "link_status": "linked",
                        "linked_catalog_ids": ["CAT-GENERIC-001"],
                    },
                ) as link:
                    first = bind_current_waba_to_merchant_catalog(db, 9, confirm=True)
                    second = bind_current_waba_to_merchant_catalog(db, 9, confirm=True)
    assert first["ok"] is True and second["ok"] is True
    assert first["catalog_reused"] is True
    assert link.call_count == 2
    assert all(c.kwargs.get("confirm") is True for c in link.call_args_list)


def test_catalog_enable_change_requires_reconcile():
    assert catalog_config_changes_require_reconcile(
        {"catalog_enabled": {"before": False, "after": True}}
    ) is True
    assert catalog_config_changes_require_reconcile(
        {"catalog_enabled": {"before": True, "after": False}}
    ) is False
    assert catalog_config_changes_require_reconcile({}) is False
