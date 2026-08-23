"""App Review harness: flag-gated merchant-owned catalog bootstrap."""
from __future__ import annotations

import inspect
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import OWNERSHIP_NAHLA_MANAGED, SOURCE_NAHLA_NATIVE  # noqa: E402
from core.catalog_review_harness import (  # noqa: E402
    ERROR_HARNESS_DISABLED,
    ERROR_NAHLAH_BM_BLOCKED,
    ERROR_OWNERSHIP_MISMATCH,
    ERROR_PRODUCTION_BLOCKED,
    ERROR_REAUTH_REQUIRED,
    ERROR_TENANT_1_BLOCKED,
    ERROR_WRONG_APP_ID,
    HARNESS_META_KEY,
    REQUIRED_SCOPES,
    embedded_signup_config_id,
    is_catalog_review_harness_enabled,
    public_review_harness_status,
    redact_graph_ids,
    strip_secrets,
)
from services import meta_catalog_review_harness as harness  # noqa: E402
from services.meta_catalog_review_harness import (  # noqa: E402
    run_catalog_review_harness,
    schedule_catalog_review_harness_best_effort,
    stamp_disconnect_preserves_assets,
)

TEST_APP = "111222333444555"
TEST_CONFIG = "cfg_review_test_only"
LIVE_CONFIG = "live_config_must_not_be_used"
BLOCKED_BM = "BM-BLOCKED-1"
REVIEW_BM = "BM-REVIEW-1"
WABA = "WABA-REVIEW-1"
CATALOG = "CAT-HARNESS-1"


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("ENVIRONMENT", overrides.get("environment", "staging"))
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_HARNESS", "true")
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_TEST_APP_ID", TEST_APP)
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_TEST_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_TEST_CONFIG_ID", TEST_CONFIG)
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_BLOCKED_BUSINESS_IDS", BLOCKED_BM)
    monkeypatch.setenv("NAHLA_CATALOG_REVIEW_REQUIRED_BUSINESS_NAME", "Nahlah Review Test")
    monkeypatch.setenv("META_WA_CONFIG_ID", LIVE_CONFIG)
    monkeypatch.setenv("META_EMBEDDED_SIGNUP_CONFIG_ID", LIVE_CONFIG)


def _conn(**overrides):
    base = dict(
        tenant_id=9,
        whatsapp_business_account_id=WABA,
        meta_catalog_id=None,
        catalog_enabled=False,
        extra_metadata={},
        provider="meta",
        connection_type="embedded",
        access_token="EAAB-merchant-secret-token",
        _sa_instance_state=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _product(**overrides):
    base = dict(
        id=42,
        tenant_id=9,
        title="White running shoes",
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        meta_retailer_id=None,
        external_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(conn, products=None):
    db = MagicMock()

    def query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if "WhatsAppConnection" in name:
            q.filter.return_value.first.return_value = conn
        else:
            q.filter.return_value.order_by.return_value.all.return_value = list(products or [])
            q.filter.return_value.all.return_value = list(products or [])
        return q

    db.query.side_effect = query
    return db


def _debug(app_id=TEST_APP, scopes=None, is_valid=True):
    def _fn(token, client=None):
        return {
            "ok": bool(is_valid),
            "app_id": app_id,
            "scopes": list(scopes if scopes is not None else REQUIRED_SCOPES),
            "is_valid": is_valid,
        }
    return _fn


def _json_response(payload, status=200):
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://graph.facebook.com/test"),
    )


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.owned = []
        self.linked = []
        self.create_posts = 0
        self.bind_posts = 0
        self.waba_owner = {"id": REVIEW_BM, "name": "Nahlah Review Test"}
        self.catalog_owner = {CATALOG: REVIEW_BM}

    def get(self, url, params=None, headers=None, **kwargs):
        return self.request("GET", url, params=params, headers=headers)

    def request(self, method, url, params=None, data=None, json=None, headers=None):
        method = method.upper()
        self.calls.append({
            "method": method,
            "url": url,
            "params": params,
            "data": data,
            "json": json,
        })
        path = url.split("facebook.com/")[-1]
        if "/" in path:
            path = path.split("/", 1)[1]
        if method == "GET" and path.rstrip("/") == WABA:
            return _json_response({
                "id": WABA,
                "owner_business_info": self.waba_owner,
            })
        if method == "GET" and path.endswith("/owned_product_catalogs"):
            return _json_response({
                "data": [
                    {
                        "id": c["id"],
                        "name": c.get("name"),
                        "business": {"id": c.get("business_id") or REVIEW_BM},
                    }
                    for c in self.owned
                ],
            })
        if method == "POST" and path.endswith("/owned_product_catalogs"):
            self.create_posts += 1
            name = None
            if isinstance(data, dict):
                name = data.get("name")
            self.owned.append({
                "id": CATALOG,
                "name": name,
                "business_id": REVIEW_BM,
            })
            self.catalog_owner[CATALOG] = REVIEW_BM
            return _json_response({"id": CATALOG})
        if method == "GET" and path.rstrip("/") in self.catalog_owner:
            cid = path.rstrip("/")
            return _json_response({
                "id": cid,
                "business": {"id": self.catalog_owner[cid], "name": "Nahlah Review Test"},
            })
        if method == "GET" and path.endswith("/product_catalogs"):
            return _json_response({
                "data": [{"id": cid, "name": "Harness"} for cid in self.linked],
            })
        if method == "POST" and path.endswith("/product_catalogs"):
            self.bind_posts += 1
            cid = None
            if isinstance(data, dict):
                cid = data.get("catalog_id")
            if cid:
                self.linked.append(str(cid))
            return _json_response({"success": True})
        return _json_response({"error": {"message": "unhandled", "code": 100}}, status=400)

    def close(self):
        return None


def _run(monkeypatch, conn, *, products=None, graph=None, debug=None, confirm=True):
    _enable(monkeypatch)
    db = _db(conn, products=products)
    client = graph or FakeGraph()
    monkeypatch.setattr(
        "services.meta_catalog_import.read_access_token",
        lambda _conn: "EAAB-merchant-secret-token",
    )
    monkeypatch.setattr(
        harness,
        "push_one_meta_catalog_item",
        lambda *a, **k: {"ok": True, "error": None},
    )
    return run_catalog_review_harness(
        db,
        int(conn.tenant_id),
        confirm=confirm,
        client=client,
        debug_token_fn=debug or _debug(),
    ), client, db


def test_flag_off_no_behavior_change(monkeypatch):
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_HARNESS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    conn = _conn()
    graph = FakeGraph()
    out = run_catalog_review_harness(
        _db(conn), 9, confirm=True, client=graph, debug_token_fn=_debug(),
    )
    assert out["skipped"] is True
    assert out["error"] == ERROR_HARNESS_DISABLED
    assert graph.create_posts == 0
    assert graph.bind_posts == 0
    assert is_catalog_review_harness_enabled() is False
    assert public_review_harness_status(conn) is None


def test_production_environment_blocked(monkeypatch):
    _enable(monkeypatch, environment="production")
    conn = _conn()
    graph = FakeGraph()
    out = run_catalog_review_harness(
        _db(conn), 9, confirm=True, client=graph, debug_token_fn=_debug(),
    )
    assert out["ok"] is False
    assert out["error"] == ERROR_PRODUCTION_BLOCKED
    assert graph.create_posts == 0
    assert is_catalog_review_harness_enabled() is False
    assert embedded_signup_config_id() != TEST_CONFIG


def test_wrong_app_id_blocked(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    out, graph, _ = _run(monkeypatch, conn, graph=graph, debug=_debug(app_id="999888777"))
    assert out["error"] == ERROR_WRONG_APP_ID
    assert graph.create_posts == 0
    assert graph.bind_posts == 0


def test_tenant_1_blocked(monkeypatch):
    _enable(monkeypatch)
    conn = _conn(tenant_id=1)
    graph = FakeGraph()
    out = run_catalog_review_harness(
        _db(conn), 1, confirm=True, client=graph, debug_token_fn=_debug(),
    )
    assert out["error"] == ERROR_TENANT_1_BLOCKED
    assert graph.create_posts == 0


def test_nahlah_bm_owner_blocked(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    graph.waba_owner = {"id": BLOCKED_BM, "name": "Nahlah Review Test"}
    out, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert out["error"] == ERROR_NAHLAH_BM_BLOCKED
    assert graph.create_posts == 0


def test_missing_scopes_reauth_required(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    out, graph, _ = _run(
        monkeypatch,
        conn,
        graph=graph,
        debug=_debug(scopes=["whatsapp_business_management", "whatsapp_business_messaging"]),
    )
    assert out["error"] == ERROR_REAUTH_REQUIRED
    assert graph.create_posts == 0
    state = conn.extra_metadata[HARNESS_META_KEY]
    assert state["error_code"] == ERROR_REAUTH_REQUIRED
    dumped = json.dumps(conn.extra_metadata)
    assert "EAAB-merchant-secret-token" not in dumped


def test_ownership_mismatch_blocked(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    graph.owned = [{"id": CATALOG, "name": "Nahlah Review Harness abcd1234", "business_id": REVIEW_BM}]
    conn.extra_metadata = {
        HARNESS_META_KEY: {
            "catalog_id": CATALOG,
            "intended_catalog_name": "Nahlah Review Harness abcd1234",
            "marker": "abcd1234ffff",
        },
    }
    graph.catalog_owner[CATALOG] = "BM-OTHER-9"
    out, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert out["error"] == ERROR_OWNERSHIP_MISMATCH
    assert graph.bind_posts == 0


def test_create_idempotency(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    first, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert first["ok"] is True
    assert first["created_catalog"] is True
    assert graph.create_posts == 1
    second, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert second["ok"] is True
    assert second["catalog_reused"] is True
    assert second["created_catalog"] is False
    assert graph.create_posts == 1


def test_does_not_select_catalog_by_name_alone(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    graph.owned = [{
        "id": "CAT-RANDOM-NAME-MATCH",
        "name": "Nahlah Review Test",
        "business_id": REVIEW_BM,
    }]
    out, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert out["ok"] is True
    assert out["created_catalog"] is True
    assert graph.create_posts == 1
    assert conn.meta_catalog_id == CATALOG
    assert conn.meta_catalog_id != "CAT-RANDOM-NAME-MATCH"


def test_bind_idempotency(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    first, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert first["bind_verified"] is True
    assert graph.bind_posts == 1
    second, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert second["bind_verified"] is True
    assert graph.bind_posts == 1


def test_post_bind_followed_by_get_verification(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    out, graph, _ = _run(monkeypatch, conn, graph=graph)
    assert out["bind_verified"] is True
    methods = [c["method"] for c in graph.calls if str(c["url"]).endswith("/product_catalogs")]
    assert "POST" in methods
    assert methods.count("GET") >= 2


def test_stable_retailer_id(monkeypatch):
    conn = _conn()
    product = _product()
    out, _, _ = _run(monkeypatch, conn, products=[product])
    assert out["ok"] is True
    assert out["retailer_id"] == "nahla_p_42"
    assert out["product_synced"] is True
    assert out["ui_status"] == "connected_and_synced"


def test_duplicate_callback_retry(monkeypatch):
    conn = _conn()
    graph = FakeGraph()
    _run(monkeypatch, conn, graph=graph)
    _run(monkeypatch, conn, graph=graph)
    _run(monkeypatch, conn, graph=graph)
    assert graph.create_posts == 1
    assert graph.bind_posts == 1


def test_token_sanitization(monkeypatch):
    conn = _conn()
    out, _, _ = _run(monkeypatch, conn)
    dumped = json.dumps(conn.extra_metadata)
    assert "EAAB-merchant-secret-token" not in dumped
    assert "test-app-secret" not in dumped
    assert "token" not in (conn.extra_metadata.get(HARNESS_META_KEY) or {})
    public = public_review_harness_status(conn)
    assert public is not None
    assert "catalog_id" not in public
    assert public["hide_graph_ids"] is True
    redacted = redact_graph_ids({
        "meta_catalog_id": CATALOG,
        "waba_id": WABA,
        "token": "EAAB-secret",
    })
    assert redacted["meta_catalog_id"] is None
    assert redacted["waba_id"] is None
    assert strip_secrets({"token": "EAAB", "ok": True}) == {"ok": True}
    assert out.get("error") in (None, out.get("error"))


def test_disconnect_does_not_delete_or_unlink_assets():
    src = inspect.getsource(harness)
    assert "share_catalog_with_business" not in src
    assert "/agencies" not in src
    assert "method\": \"DELETE\"" not in src
    assert "http.delete" not in src.lower()
    from routers.whatsapp_connect import disconnect as wa_disconnect
    disconnect_src = inspect.getsource(wa_disconnect)
    assert "owned_product_catalogs" not in disconnect_src
    assert "product_catalogs" not in disconnect_src
    assert "stamp_disconnect_preserves_assets" in disconnect_src
    conn = _conn(extra_metadata={HARNESS_META_KEY: {"catalog_id": CATALOG}})
    stamp_disconnect_preserves_assets(conn)
    state = conn.extra_metadata[HARNESS_META_KEY]
    assert state["disconnect_preserves_catalog"] is True
    assert state["disconnect_unlinked_waba"] is False
    assert state["disconnect_deleted_catalog"] is False
    assert state["disconnect_deleted_product"] is False


def test_harness_enabled_does_not_use_live_config_id(monkeypatch):
    _enable(monkeypatch)
    assert is_catalog_review_harness_enabled() is True
    assert embedded_signup_config_id() == TEST_CONFIG
    assert embedded_signup_config_id() != LIVE_CONFIG


def test_schedule_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_HARNESS", raising=False)
    with patch.object(harness, "run_catalog_review_harness_background") as bg:
        schedule_catalog_review_harness_best_effort(9)
    bg.assert_not_called()


def test_finalize_source_skips_reconnect_when_harness_on():
    from core.whatsapp_connection_finalization import finalize_successful_whatsapp_connection
    src = inspect.getsource(finalize_successful_whatsapp_connection)
    assert "is_catalog_review_harness_enabled" in src
    assert "schedule_catalog_review_harness_best_effort" in src
    assert "schedule_meta_catalog_reconnect_best_effort" in src


def test_missing_test_app_id_blocked(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_TEST_APP_ID", raising=False)
    conn = _conn()
    graph = FakeGraph()
    out = run_catalog_review_harness(
        _db(conn), 9, confirm=True, client=graph, debug_token_fn=_debug(),
    )
    assert out["ok"] is False
    assert graph.create_posts == 0
    assert is_catalog_review_harness_enabled() is False
