"""Consent-backed native catalog sync, end to end in process.

A tenant holds a verified catalog-only Meta consent for its catalog AND an
existing WhatsApp connection bound to the same catalog (with a WABA, a phone
number and a merchant token). Preview, confirmed create, update and the
reconciling re-sync run through the real native orchestrator against an
in-memory catalog. Proven:

* every Graph write and read uses the consent token only — the WhatsApp
  merchant token and the platform token are never read or sent;
* no WABA or phone endpoint is called; WhatsApp linkage is recorded as not
  proven (never "linked");
* revoking the entitlement or the sync scope after consent stops the next
  consent-backed operation before any Graph call;
* with no consent row the same tenant keeps the existing WhatsApp path.

Generic merchant data (متجر تجريبي عام, «حذاء رياضي أبيض»). Nothing talks to Meta.
"""
from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.models import (  # noqa: E402
    Base,
    MetaCatalogAuthorization,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import OWNERSHIP_NAHLA_MANAGED, SOURCE_NAHLA_NATIVE  # noqa: E402

CATALOG = "880000000000001"
BUSINESS = "770000000000001"
APP_ID = "600000000000001"
RID = "SHOE-WHITE-42"


def _remap(target, connection, **kw):
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class ConsentGraph:
    """The approved catalog only; records the bearer token on every call."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self._n = 0

    def _record(self, method, url, headers):
        auth = str((headers or {}).get("Authorization") or "")
        self.calls.append((method, url, auth.replace("Bearer ", "")))
        for fragment in ("whatsapp", "phone_number", "product_catalogs", "subscribed_apps", "/messages"):
            assert fragment not in url, url

    def get(self, url, params=None, headers=None):
        self._record("GET", url, headers)
        assert f"/{CATALOG}" in url, url
        filt = (params or {}).get("filter")
        if "/products" in url and filt:
            rid = json.loads(filt)["retailer_id"]["eq"]
            item = self.items.get(rid)
            return _Resp(200, {"data": [] if item is None else [{"retailer_id": rid, "visibility": "published", **item}]})
        if "/products" in url:
            return _Resp(200, {"data": []})
        return _Resp(200, {"id": CATALOG, "name": "متجر تجريبي عام"})

    def post(self, url, data=None, headers=None, params=None):
        self._record("POST", url, headers)
        body = dict(data or {})
        if url.rstrip("/").endswith(f"/{CATALOG}/products"):
            self._n += 1
            meta_id = f"META-CONSENT-{self._n}"
            self.items[body["retailer_id"]] = {**body, "id": meta_id}
            return _Resp(200, {"id": meta_id})
        meta_id = url.rstrip("/").rsplit("/", 1)[-1]
        for item in self.items.values():
            if item["id"] == meta_id:
                item.update(body)
                return _Resp(200, {"success": True})
        return _Resp(404, {"error": {"code": 100}})

    def close(self):
        return None


@pytest.fixture()
def world(monkeypatch):
    import core.plan_entitlements as ent_mod
    import services.meta_catalog_access as access
    import services.meta_catalog_import as importer
    import services.meta_catalog_linking as linking
    import services.native_meta_sync_orchestrator as orchestrator
    from core.wa_token_crypto import encrypt_access_token
    from services import meta_catalog_consent as consent
    from services import meta_catalog_push as push

    for name in ("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"):
        monkeypatch.delenv(name, raising=False)
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "NAHLA_META_CATALOG_CONSENT_ENABLED": "1",
        "META_APP_ID": APP_ID,
        "WA_TOKEN_ENC_KEY": Fernet.generate_key().decode(),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    engine = create_engine("sqlite:///:memory:")
    event.listen(Base.metadata, "before_create", _remap)
    try:
        Base.metadata.create_all(engine)
    finally:
        event.remove(Base.metadata, "before_create", _remap)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    tid = tenant.id
    monkeypatch.setenv("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{tid}:{CATALOG}:{BUSINESS}")

    merchant_token = "EAAB" + secrets.token_hex(20)
    platform_token = "EAAP" + secrets.token_hex(20)
    consent_token = "EAAC" + secrets.token_hex(20)
    session.add(WhatsAppConnection(
        tenant_id=tid, whatsapp_business_account_id="WABA-GENERIC-1", phone_number_id="PN-GENERIC-1",
        access_token=merchant_token, meta_catalog_id=CATALOG, catalog_enabled=True,
        provider="meta", connection_type="embedded", extra_metadata={},
    ))
    session.commit()

    def _raise(*_a, **_k):
        raise AssertionError("WhatsApp/WABA path touched on the catalog-consent path")

    monkeypatch.setattr(orchestrator, "get_waba_catalog_link_status", _raise)
    monkeypatch.setattr(linking, "_fetch_waba_product_catalogs", _raise)
    monkeypatch.setattr(importer, "read_access_token", _raise)
    monkeypatch.setattr(access, "WA_TOKEN", platform_token)

    probes = []

    def _probe(token, catalog_id, client=None):
        probes.append((token, catalog_id))
        return {"ok": True, "catalog_id": catalog_id, "business_id": BUSINESS}

    monkeypatch.setattr(push, "probe_catalog_readable", _probe)

    class _Ent:
        def __init__(self, ok):
            self.ok = ok

        def has_feature(self, key):
            return self.ok and key == "meta_catalog_sync"

    entitled = {"ok": True}
    monkeypatch.setattr(ent_mod, "get_entitlements", lambda db, t, strict_lookup=True: _Ent(entitled["ok"]))

    now = datetime.now(timezone.utc)
    session.add(MetaCatalogAuthorization(
        tenant_id=tid, catalog_id=CATALOG, business_id=BUSINESS, meta_app_id=APP_ID, meta_user_id="500000000000001",
        access_token_enc=encrypt_access_token(consent_token), granted_scopes=["business_management", "catalog_management"],
        token_expires_at=now + timedelta(days=50), data_access_expires_at=now + timedelta(days=80),
        status="active", verified_at=now, created_at=now, updated_at=now,
    ))
    product = Product(
        tenant_id=tid, title="حذاء رياضي أبيض", price="250", in_stock=True, stock_quantity=4,
        source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED, catalog_status="active",
        sync_status="pending",
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shoe.jpg",
                        "product_url": "https://store.example/p/shoe"},
    )
    session.add(product)
    session.flush()
    session.add(ProductVariant(tenant_id=tid, product_id=product.id, retailer_id=RID, price="250", currency="SAR",
                               stock_quantity=4, in_stock=True, is_default=True))
    session.commit()
    consent._TABLE_SEEN.clear()

    class W:
        pass

    w = W()
    w.session, w.tid, w.pid, w.graph, w.probes = session, tid, product.id, ConsentGraph(), probes
    w.consent_token, w.merchant_token, w.platform_token = consent_token, merchant_token, platform_token
    w.entitled = entitled
    yield w
    session.close()
    engine.dispose()
    consent._TABLE_SEEN.clear()


def _sync(w):
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync

    out = attempt_native_meta_sync(w.session, w.tid, w.pid, client=w.graph)
    w.session.expire_all()
    return out


def _tokens_used(w):
    return {token for _m, _u, token in w.graph.calls} | {t for t, _c in w.probes}


def test_preview_create_update_and_reconcile_use_only_the_consent_token(world):
    from services.meta_catalog_sync_preview import preview_native_meta_sync

    w = world
    preview = preview_native_meta_sync(w.session, w.tid, w.pid)
    assert preview.get("eligible") is True and preview.get("meta_catalog_id") == CATALOG, preview
    assert w.graph.calls == []  # preview never calls Graph

    created = _sync(w)
    assert created.get("ok") is True, created
    product = w.session.get(Product, w.pid)
    assert product.sync_status == "synced" and RID in w.graph.items
    assert any(m == "POST" and u.endswith(f"/{CATALOG}/products") for m, u, _t in w.graph.calls)
    sync_meta = (product.extra_metadata or {}).get("sync_meta") or {}
    assert sync_meta.get("waba_linked") in (None, False)  # never claimed linked

    product.price = "199"
    variant = w.session.query(ProductVariant).filter_by(tenant_id=w.tid, retailer_id=RID).one()
    variant.price = "199"
    product.sync_status = "pending"
    w.session.commit()
    posts_before = len([c for c in w.graph.calls if c[0] == "POST"])
    updated = _sync(w)
    assert updated.get("ok") is True, updated
    assert len([c for c in w.graph.calls if c[0] == "POST"]) == posts_before + 1
    update_posts = [u for m, u, _t in w.graph.calls if m == "POST" and not u.endswith(f"/{CATALOG}/products")]
    assert update_posts and update_posts[-1].endswith("/" + w.graph.items[RID]["id"])
    assert "199" in str(w.graph.items[RID].get("price"))

    w.session.get(Product, w.pid).sync_status = "pending"
    w.session.commit()
    reconciled = _sync(w)
    assert reconciled.get("ok") is True, reconciled
    assert len(w.graph.items) == 1  # the reconciling re-sync never duplicates the item

    assert _tokens_used(w) == {w.consent_token}
    assert w.merchant_token not in json.dumps(w.graph.calls) and w.platform_token not in json.dumps(w.graph.calls)


@pytest.mark.parametrize("revoke", ["entitlement", "sync_scope"])
def test_revoked_entitlement_or_scope_stops_the_next_consent_operation(world, monkeypatch, revoke):
    w = world
    assert _sync(w).get("ok") is True
    calls = len(w.graph.calls)
    if revoke == "entitlement":
        w.entitled["ok"] = False
    else:
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", str(w.tid + 1000))
    w.session.get(Product, w.pid).sync_status = "pending"
    w.session.commit()
    out = _sync(w)
    assert out.get("ok") is not True, out
    assert len(w.graph.calls) == calls  # nothing reached Graph
    from services.meta_catalog_push import MetaCatalogPushError, _resolve_connection

    with pytest.raises(MetaCatalogPushError) as exc:
        _resolve_connection(w.session, w.tid)
    assert exc.value.code == "catalog_consent_inactive"
    assert exc.value.detail["reason"] in ("entitlement_missing", "sync_scope_excluded")


def test_whatsapp_token_pickers_never_serve_a_consent_governed_catalog(world):
    from models import WhatsAppConnection as ServiceConnection
    from services.meta_catalog_access import catalog_token_candidates, select_catalog_graph_token

    w = world
    conn = w.session.query(ServiceConnection).filter_by(tenant_id=w.tid).one()
    assert catalog_token_candidates(conn) == []
    assert select_catalog_graph_token(conn, CATALOG)["token"] is None


def test_without_consent_the_existing_whatsapp_path_is_unchanged(world, monkeypatch):
    import services.meta_catalog_import as importer
    from models import WhatsAppConnection as ServiceConnection
    from services.meta_catalog_access import catalog_token_candidates
    from services.meta_catalog_push import _resolve_connection
    from services.whatsapp_platform.wa_connection_secrets import read_access_token

    w = world
    w.session.query(MetaCatalogAuthorization).delete()
    w.session.commit()
    monkeypatch.setattr(importer, "read_access_token", read_access_token)
    conn = _resolve_connection(w.session, w.tid)
    assert isinstance(conn, ServiceConnection)
    assert [c["token"] for c in catalog_token_candidates(conn)] == [w.merchant_token, w.platform_token]


def test_without_consent_the_waba_wrapper_queries_nothing_new():
    """Regression: schemas without whatsapp_connections (PG lock tests) keep working."""
    from unittest.mock import patch

    from services import meta_catalog_consent as consent
    from services.native_meta_sync_orchestrator import _waba_link_status_for_push

    engine = create_engine("sqlite:///:memory:")
    event.listen(Base.metadata, "before_create", _remap)
    try:
        Base.metadata.create_all(engine, tables=[Tenant.__table__, Product.__table__, ProductVariant.__table__])
    finally:
        event.remove(Base.metadata, "before_create", _remap)
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *a: statements.append(statement))
    session = sessionmaker(bind=engine)()
    consent._TABLE_SEEN.clear()
    try:
        with patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status",
                   return_value={"ok": True, "expected_catalog_linked": True}) as legacy:
            assert _waba_link_status_for_push(session, 7) == {"ok": True, "expected_catalog_linked": True}
        legacy.assert_called_once()
        assert not any("whatsapp_connections" in s or "meta_catalog_authorizations" in s for s in statements)
    finally:
        session.close()
        engine.dispose()
        consent._TABLE_SEEN.clear()


@pytest.mark.parametrize("status", ["active", "revoked"])
def test_catalog_import_and_probe_refuse_a_consent_governed_catalog(world, monkeypatch, status):
    import httpx

    from models import WhatsAppConnection as ServiceConnection
    from services.meta_catalog_import import (
        GRAPH_RESULT_CATALOG_CONSENT_GOVERNED,
        MetaCatalogImportError,
        build_graph_import_diagnostics,
        import_from_meta,
    )

    w = world
    row = w.session.query(MetaCatalogAuthorization).one()
    row.status = status
    w.session.commit()

    def _no_network(*_a, **_k):
        raise AssertionError("provider read on a consent-governed catalog")

    monkeypatch.setattr(httpx, "Client", _no_network)
    monkeypatch.setattr(httpx, "get", _no_network)
    conn = w.session.query(ServiceConnection).filter_by(tenant_id=w.tid).one()
    diag = build_graph_import_diagnostics(conn, tenant_id=w.tid, run_preflight=True)
    assert diag["result_code"] == GRAPH_RESULT_CATALOG_CONSENT_GOVERNED and diag["token_selection"] is None
    with pytest.raises(MetaCatalogImportError) as exc:
        import_from_meta(w.session, w.tid)
    assert exc.value.code == GRAPH_RESULT_CATALOG_CONSENT_GOVERNED
    text = json.dumps(diag) + str(exc.value) + json.dumps(getattr(exc.value, "detail", {}) or {})
    assert w.merchant_token not in text and w.platform_token not in text


def test_catalog_import_probe_without_consent_keeps_the_legacy_token_path(world, monkeypatch):
    import services.meta_catalog_import as importer
    from models import WhatsAppConnection as ServiceConnection
    from services.whatsapp_platform.wa_connection_secrets import read_access_token

    w = world
    w.session.query(MetaCatalogAuthorization).delete()
    w.session.commit()
    monkeypatch.setattr(importer, "read_access_token", read_access_token)
    conn = w.session.query(ServiceConnection).filter_by(tenant_id=w.tid).one()
    diag = importer.build_graph_import_diagnostics(conn, tenant_id=w.tid, run_preflight=False)
    assert diag["result_code"] != importer.GRAPH_RESULT_CATALOG_CONSENT_GOVERNED
    assert (diag["token_selection"] or {}).get("token_source") == "merchant_meta_oauth"
