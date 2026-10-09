"""Catalog-only consent retirement/reconciliation against isolated SQLite/Graph.

The real binding, publication evidence and retirement helpers are used. No
WhatsApp row, WABA lookup, real provider request or external database is needed.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    CatalogChannelRetirement,
    MetaCatalogAuthorization,
    MetaCatalogMembership,
    Product,
    Tenant,
    TenantSettings,
    WhatsAppConnection,
)
from services import whatsapp_catalog_reconcile as reconcile
from services import whatsapp_catalog_retirement as retirement

CATALOG = "880000000000011"
BUSINESS = "770000000000011"
APP_ID = "600000000000011"
RID = "SHIRT-BLUE-M"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.text = json.dumps(body)
        self._body = body

    def json(self):
        return self._body


class _Graph:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.fail = False

    def _record(self, method, url, headers):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        self.calls.append((method, url, token))
        assert not any(part in url for part in ("whatsapp", "phone_number", "product_catalogs", "subscribed_apps", "/messages"))

    def get(self, url, params=None, headers=None):
        self._record("GET", url, headers)
        assert f"/{CATALOG}/products" in url
        if self.fail:
            return _Response(500, {"error": {"code": 1}})
        filt = (params or {}).get("filter")
        rid = json.loads(filt)["retailer_id"]["eq"] if filt else None
        return _Response(200, {"data": [dict(item) for key, item in self.items.items() if rid is None or key == rid]})

    def post(self, url, data=None, headers=None, params=None):
        self._record("POST", url, headers)
        mid = url.rsplit("/", 1)[-1]
        for item in self.items.values():
            if item["id"] == mid:
                item.update(data or {})
                return _Response(200, {"success": True})
        raise AssertionError("Retirement must never create an item")


@pytest.fixture()
def world(monkeypatch):
    import core.plan_entitlements as entitlements
    from core.wa_token_crypto import encrypt_access_token
    from services import meta_catalog_access as access
    from services import meta_catalog_consent as consent
    from services import meta_catalog_linking as linking
    from services import meta_catalog_push as push
    from services import whatsapp_catalog_sync as sync

    for key in ("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "NAHLA_CATALOG_REVIEW_ENV": "0",
        "NAHLA_META_CATALOG_CONSENT_ENABLED": "1",
        "NAHLA_META_CATALOG_CONSENT_PRIMARY_ENABLED": "1",
        "META_APP_ID": APP_ID,
        "META_APP_SECRET": secrets.token_hex(24),
        "META_CATALOG_CONSENT_CONFIG_ID": "900000000000011",
        "META_CATALOG_CONSENT_REDIRECT_URI": "https://api.nahlah.ai/merchant/catalog/meta-consent/callback",
        "DASHBOARD_URL": "https://app.nahlah.ai",
        "WA_TOKEN_ENC_KEY": Fernet.generate_key().decode(),
        "NAHLA_WHATSAPP_CATALOG_AUTO_SYNC": "1",
    }.items():
        monkeypatch.setenv(key, value)

    def remap(target, connection, **kwargs):
        for table in target.sorted_tables:
            for column in table.columns:
                if isinstance(column.type, JSONB):
                    column.type = JSON()

    engine = create_engine("sqlite:///:memory:")
    event.listen(Base.metadata, "before_create", remap)
    try:
        Base.metadata.create_all(engine)
    finally:
        event.remove(Base.metadata, "before_create", remap)
    db = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي للملابس", is_active=True)
    db.add(tenant)
    db.commit()
    monkeypatch.setenv("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{tenant.id}:{CATALOG}:{BUSINESS}")
    token = "EAAC" + secrets.token_hex(20)
    now = datetime.now(timezone.utc)
    authorization = MetaCatalogAuthorization(
        tenant_id=tenant.id, catalog_id=CATALOG, business_id=BUSINESS, meta_app_id=APP_ID,
        meta_user_id="500000000000011", access_token_enc=encrypt_access_token(token),
        granted_scopes=["business_management", "catalog_management"],
        token_expires_at=now + timedelta(days=30), data_access_expires_at=now + timedelta(days=60),
        status="active", verified_at=now, created_at=now, updated_at=now,
    )
    db.add(authorization)
    db.commit()
    w = SimpleNamespace(db=db, tid=tenant.id, graph=_Graph(), token=token, probes=[], wa_calls=[], entitled=True)

    def entitlement(*args, **kwargs):
        return SimpleNamespace(has_feature=lambda feature: w.entitled and feature == "meta_catalog_sync")

    monkeypatch.setattr(entitlements, "get_entitlements", entitlement)
    monkeypatch.setattr(sync, "get_entitlements", entitlement)

    def forbidden(*args, **kwargs):
        w.wa_calls.append((args, kwargs))
        raise AssertionError("Catalog-only work touched a WhatsApp token/link path")

    monkeypatch.setattr(access, "_select_graph_token", forbidden)
    monkeypatch.setattr(push, "_select_graph_token", forbidden)
    monkeypatch.setattr(linking, "get_waba_catalog_link_status", forbidden)

    def probe(token_value, catalog_id, client=None):
        w.probes.append((token_value, catalog_id))
        assert token_value == w.token and catalog_id == CATALOG
        return {"ok": True, "catalog_id": catalog_id, "business_id": BUSINESS}

    monkeypatch.setattr(access, "probe_catalog_readable", probe)
    monkeypatch.setattr(push, "probe_catalog_readable", probe)
    consent._TABLE_SEEN.clear()
    yield w
    db.close()
    engine.dispose()
    consent._TABLE_SEEN.clear()


def _add_wa(w, *, bound=True, enabled=True):
    conn = WhatsAppConnection(
        tenant_id=w.tid, whatsapp_business_account_id="WABA-GENERIC", phone_number_id="PHONE-GENERIC",
        access_token="EAAB" + secrets.token_hex(20), meta_catalog_id=CATALOG if bound else None,
        catalog_enabled=enabled, extra_metadata={"preserved": {"value": 1}},
    )
    w.db.add(conn)
    w.db.commit()
    return conn


def _published(w, *, rid=RID, provenance="native_product_push", catalog_id=CATALOG):
    row = Product(
        tenant_id=w.tid, title="قميص قطني أزرق", price="250", in_stock=True, stock_quantity=4,
        source="nahla_native", ownership_mode="nahla_managed", catalog_status="active", sync_status="synced",
        last_synced_at=datetime.now(timezone.utc),
        extra_metadata={"currency": "SAR", "sync_meta": {
            "content_verified": True,
            "expected_payloads_by_retailer_id": {rid: {"price": 25000, "currency": "SAR", "availability": "in stock"}},
        }},
    )
    w.db.add(row)
    w.db.flush()
    mid = "META-" + rid
    w.db.add(MetaCatalogMembership(
        tenant_id=w.tid, catalog_id=catalog_id, retailer_id=rid, product_id=row.id,
        meta_item_id=mid, verified_at=datetime.now(timezone.utc), provenance=provenance,
    ))
    w.db.commit()
    w.graph.items[rid] = {"id": mid, "retailer_id": rid, "price": "250.00 SAR", "currency": "SAR",
                          "availability": "in stock", "visibility": "published"}
    return row


def _delete_with_ledger(w, product):
    identities = retirement.channel_identities_for_product(w.db, product)
    count = retirement.enqueue_channel_retirement_ledger(w.db, w.tid, identities, reason=retirement.REASON_MANUAL_DELETED)
    w.db.query(MetaCatalogMembership).filter_by(tenant_id=w.tid, product_id=product.id).delete()
    w.db.delete(product)
    w.db.commit()
    return count


def _assert_consent_only(w):
    assert not w.wa_calls
    assert {token for _, _, token in w.graph.calls} | {token for token, _ in w.probes} == {w.token}


@pytest.mark.parametrize("catalog_version", [None, "v26.0"])
@pytest.mark.parametrize("with_wa", [False, True])
def test_deleted_owned_product_is_retired_using_only_consent(world, with_wa, monkeypatch, catalog_version):
    from core import config
    monkeypatch.setattr(config, "META_GRAPH_API_VERSION", "v21.0")
    monkeypatch.delenv("META_CATALOG_GRAPH_API_VERSION", raising=False)
    if catalog_version:
        monkeypatch.setenv("META_CATALOG_GRAPH_API_VERSION", catalog_version)
    w = world
    if with_wa:
        _add_wa(w)
    product = _published(w)
    assert _delete_with_ledger(w, product) == 1
    assert w.db.query(Product).count() == 0
    assert w.db.query(MetaCatalogMembership).count() == 0
    out = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    assert out["retired"] == 1 and out["remaining"] == 0
    assert w.graph.items[RID]["availability"] == "out of stock"
    assert w.graph.items[RID]["visibility"] == "staging"
    assert w.db.query(CatalogChannelRetirement).one().status == "done"
    assert w.db.query(WhatsAppConnection).count() == int(with_wa)
    _assert_consent_only(w)
    assert any(method == "POST" for method, _, _ in w.graph.calls)
    prefix = f"https://graph.facebook.com/{catalog_version or 'v21.0'}/"
    assert all(url.startswith(prefix) for _, url, _ in w.graph.calls)


def test_hidden_owned_product_retires_without_whatsapp(world):
    w = world
    product = _published(w)
    product.catalog_status = "merchant_hidden"
    product.merchant_hidden_at = datetime.now(timezone.utc)
    assert retirement.mark_product_channel_retire_pending(w.db, product)
    w.db.commit()
    out = retirement.attempt_product_channel_retirement(w.db, w.tid, product.id, client=w.graph)
    assert out["ok"] and out["retired"] == 1
    assert w.db.get(Product, product.id).sync_status == "retired"
    assert w.db.query(WhatsAppConnection).count() == 0
    _assert_consent_only(w)


@pytest.mark.parametrize("with_wa", [False, True])
@pytest.mark.parametrize("state", ["revoked", "token_expired", "data_expired", "disabled", "approval_changed", "entitlement_removed"])
def test_unusable_consent_leaves_ledger_pending_without_fallback(world, monkeypatch, state, with_wa):
    w = world
    if with_wa:
        _add_wa(w)
    assert _delete_with_ledger(w, _published(w)) == 1
    authorization = w.db.query(MetaCatalogAuthorization).one()
    if state == "revoked":
        authorization.status = "revoked"
    elif state == "token_expired":
        authorization.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif state == "data_expired":
        authorization.data_access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif state == "disabled":
        monkeypatch.setenv("NAHLA_META_CATALOG_CONSENT_ENABLED", "0")
    elif state == "approval_changed":
        monkeypatch.setenv("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{w.tid}:{CATALOG}:770000000009999")
    else:
        w.entitled = False
    w.db.commit()
    out = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    assert out["error_code"] == "catalog_consent_inactive"
    assert out["processed"] == 0 and out["remaining"] == 1
    ledger = w.db.query(CatalogChannelRetirement).one()
    assert ledger.status == "pending" and ledger.attempts == 0
    assert not w.graph.calls and not w.probes and not w.wa_calls


def test_reconcile_presence_does_not_grant_retirement_ownership(world):
    w = world
    product = _published(w, provenance="meta_graph_reconcile")
    assert _delete_with_ledger(w, product) == 0
    out = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    assert out["processed"] == 0
    assert w.db.query(CatalogChannelRetirement).count() == 0
    assert not w.graph.calls and not w.probes


@pytest.mark.parametrize("case", ["wrong_catalog", "replaced_item", "missing_evidence"])
def test_ledger_evidence_and_catalog_guards_survive_consent_bridge(world, case):
    w = world
    product = _published(w, catalog_id="880000000009999" if case == "wrong_catalog" else CATALOG)
    assert _delete_with_ledger(w, product) == 1
    if case == "replaced_item":
        w.graph.items[RID]["id"] = "META-UNOWNED-REPLACEMENT"
    if case == "missing_evidence":
        w.db.query(CatalogChannelRetirement).one().meta_item_id = None
        w.db.commit()
    out = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    assert out["retired"] == 0
    assert not any(method == "POST" for method, _, _ in w.graph.calls)
    if case == "missing_evidence":
        assert out["refused"] == 1 and not w.graph.calls and not w.probes
    else:
        assert out["failed"] == 1
        error = w.db.query(CatalogChannelRetirement).one().last_error
        assert error == ("catalog_not_current" if case == "wrong_catalog" else "meta_item_id_mismatch")


@pytest.mark.parametrize("with_wa", [False, True])
def test_reconcile_drift_uses_consent_without_waba_and_persists_cadence(world, with_wa):
    w = world
    if with_wa:
        _add_wa(w)
    else:
        w.db.add(TenantSettings(tenant_id=w.tid, extra_metadata={"preserved": {"value": 1}}))
        w.db.commit()
    auth_updated = w.db.query(MetaCatalogAuthorization).one().updated_at
    row = _published(w)
    w.graph.items[RID]["price"] = "240.00 SAR"
    out = reconcile.reconcile_tenant_channel_catalog(w.db, w.tid, client=w.graph)
    assert out["ok"] and out["drifted"] == 1 and out["requeued"] == 1
    assert out["waba_link_state"] is None and out["waba_link_skipped"] == "catalog_only_consent"
    assert w.db.get(Product, row.id).sync_status == "pending"
    w.db.expire_all()
    holder = w.db.query(WhatsAppConnection if with_wa else TenantSettings).one()
    assert holder.extra_metadata["preserved"] == {"value": 1}
    assert reconcile.reconcile_snapshot(holder)["catalog_id"] == CATALOG
    assert not reconcile.reconcile_is_due(holder)
    assert w.db.query(MetaCatalogAuthorization).one().updated_at == auth_updated
    before = len(w.graph.calls)
    assert reconcile.reconcile_due_tenants(w.db, client=w.graph)["tenants"] == 0
    assert len(w.graph.calls) == before
    assert not any(method == "POST" for method, _, _ in w.graph.calls)
    _assert_consent_only(w)


def test_zero_product_consent_tenant_is_discovered_once_with_durable_snapshot(world):
    w = world
    assert w.db.query(Product).count() == 0 and w.db.query(TenantSettings).count() == 0
    auth_updated = w.db.query(MetaCatalogAuthorization).one().updated_at
    first = reconcile.reconcile_due_tenants(w.db, client=w.graph)
    assert first["tenants"] == 1 and first["errors"] == 0
    assert w.db.query(WhatsAppConnection).count() == 0
    # A fresh session sees the cadence, not a transient binding attribute.
    with sessionmaker(bind=w.db.get_bind())() as fresh:
        snapshot = reconcile.reconcile_snapshot(fresh.query(TenantSettings).one())
        assert snapshot["ok"] and snapshot["checked_products"] == 0
        assert reconcile.reconcile_due_tenants(fresh, client=w.graph)["tenants"] == 0
        assert fresh.query(MetaCatalogAuthorization).one().updated_at == auth_updated
    _assert_consent_only(w)


def test_inactive_consent_reconcile_records_skip_without_graph_or_starvation(world):
    w = world
    w.db.query(MetaCatalogAuthorization).one().status = "revoked"
    w.db.commit()
    first = reconcile.reconcile_due_tenants(w.db, client=w.graph)
    assert first["tenants"] == 1
    snapshot = reconcile.reconcile_snapshot(w.db.query(TenantSettings).one())
    assert snapshot["skipped"] and snapshot["blocker_code"] == "catalog_consent_inactive"
    assert reconcile.reconcile_due_tenants(w.db, client=w.graph)["tenants"] == 0
    assert not w.graph.calls and not w.probes and not w.wa_calls


def test_reconcile_incomplete_read_never_marks_items_missing(world):
    w = world
    product = _published(w)
    w.graph.fail = True
    out = reconcile.reconcile_tenant_channel_catalog(w.db, w.tid, client=w.graph)
    assert not out["ok"] and out["error"] and not out["live_complete"]
    assert out["missing"] == out["requeued"] == 0
    assert w.db.get(Product, product.id).sync_status == "synced"
    assert not reconcile.reconcile_is_due(w.db.query(TenantSettings).one())


def test_reconcile_unbound_whatsapp_row_does_not_hide_catalog_only_consent(world):
    w = world
    conn = _add_wa(w, bound=False, enabled=False)
    out = reconcile.reconcile_due_tenants(w.db, client=w.graph)
    assert out["tenants"] == 1 and out["errors"] == 0
    assert reconcile.reconcile_snapshot(conn)["ok"]
    assert conn.meta_catalog_id is None and conn.catalog_enabled is False
    _assert_consent_only(w)


def test_ledger_batches_report_progress_and_never_repeat_an_attempt(world):
    w = world
    for index in range(27):
        assert _delete_with_ledger(w, _published(w, rid=f"SHIRT-{index}")) == 1
    attempted = set()
    first = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph, attempted_retirement_ids=attempted)
    assert first["processed"] == 25 and first["remaining"] == 2 and len(attempted) == 25
    second = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph, attempted_retirement_ids=attempted)
    assert second["processed"] == 2 and second["remaining"] == 0 and len(attempted) == 27
    reopened = w.db.query(CatalogChannelRetirement).first()
    reopened.status = "pending"
    reopened.next_attempt_at = None
    w.db.commit()
    third = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph, attempted_retirement_ids=attempted)
    assert third["processed"] == 0 and third["remaining"] == 1
    _assert_consent_only(w)


def test_zero_product_reconcile_resets_exhausted_ledger_for_consent_drain(world):
    w = world
    assert _delete_with_ledger(w, _published(w)) == 1
    ledger = w.db.query(CatalogChannelRetirement).one()
    ledger.status = "exhausted"
    ledger.attempts = retirement.RETIRE_MAX_ATTEMPTS
    ledger.last_error = "temporary_provider_error"
    w.db.commit()
    out = reconcile.reconcile_tenant_channel_catalog(w.db, w.tid, client=w.graph)
    assert out["ok"] and out["ledger_reset"] == 1 and out["checked_products"] == 0
    w.db.refresh(ledger)
    assert ledger.status == "pending" and ledger.attempts == 0
    drained = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    assert drained["retired"] == 1 and drained["remaining"] == 0
    assert w.db.query(WhatsAppConnection).count() == 0
    _assert_consent_only(w)


def test_unknown_consent_state_blocks_ledger_and_reconcile_before_graph(world, monkeypatch):
    from services import meta_catalog_consent as consent

    w = world
    _add_wa(w)
    assert _delete_with_ledger(w, _published(w)) == 1
    monkeypatch.setattr(consent, "authorization_schema_state", lambda db: consent.SCHEMA_UNKNOWN)
    drained = retirement.drain_channel_retirement_ledger(w.db, w.tid, client=w.graph)
    reconciled = reconcile.reconcile_tenant_channel_catalog(w.db, w.tid, client=w.graph)
    assert drained["error_code"] == reconciled["blocker_code"] == "catalog_consent_inactive"
    assert drained["remaining"] == 1 and drained["processed"] == 0
    assert reconciled["skipped"] and not reconciled["ok"]
    assert not w.graph.calls and not w.probes and not w.wa_calls


def test_existing_explicit_catalog_disable_is_not_reenabled_by_consent_reconcile(world):
    w = world
    conn = _add_wa(w, enabled=False)
    assert reconcile.reconcile_due_tenants(w.db, client=w.graph)["tenants"] == 0
    direct = reconcile.reconcile_tenant_channel_catalog(w.db, w.tid, client=w.graph)
    assert direct["skipped"] and direct["blocker_code"] == "catalog_disabled"
    assert conn.catalog_enabled is False
    assert not w.graph.calls and not w.probes and not w.wa_calls


@pytest.mark.parametrize("new_provenance", ["meta_graph_reconcile", "native_product_push"])
def test_retirement_identity_refreshes_cached_publication_evidence(world, new_provenance):
    w = world
    product = _published(w)
    cached = w.db.query(MetaCatalogMembership).one()
    assert cached.provenance == "native_product_push" and cached.meta_item_id == "META-" + RID
    with sessionmaker(bind=w.db.get_bind())() as writer:
        fresh = writer.query(MetaCatalogMembership).one()
        fresh.provenance = new_provenance
        fresh.meta_item_id = "META-NEW-COMMITTED-IDENTITY"
        writer.commit()
    # The existing identity map still has the old evidence until the helper
    # explicitly refreshes it. No product/authorization changes are involved.
    assert cached.meta_item_id == "META-" + RID
    identities = retirement.channel_identities_for_product(w.db, product)
    if new_provenance == "meta_graph_reconcile":
        assert identities == []
    else:
        assert len(identities) == 1
        assert identities[0]["meta_item_id"] == "META-NEW-COMMITTED-IDENTITY"
    assert not w.graph.calls and not w.probes
