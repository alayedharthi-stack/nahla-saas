"""Channel retirement: hidden / deleted products are withdrawn from Meta.

Generic merchant data only (متجر تجريبي عام). Graph is a fake client that
records calls; nothing here talks to Meta. Asserts behaviour and state:
which Graph writes happen, what the row/ledger records, tenant isolation.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    Base,
    CatalogChannelRetirement,
    MetaCatalogMembership,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
    is_whatsapp_channel_publish_eligible,
)
from services.meta_catalog_push import (  # noqa: E402
    RETIRED_AVAILABILITY,
    RETIRED_VISIBILITY,
    retire_meta_catalog_item,
)
from services.whatsapp_catalog_retirement import (  # noqa: E402
    LEDGER_STATUS_DONE,
    LEDGER_STATUS_EXHAUSTED,
    LEDGER_STATUS_PENDING,
    REASON_MERCHANT_HIDDEN,
    REASON_SOURCE_DELETED,
    RETIRE_MAX_ATTEMPTS,
    SYNC_STATUS_RETIRED,
    attempt_product_channel_retirement,
    channel_identities_for_product,
    drain_channel_retirement_ledger,
    enqueue_channel_retirement_ledger,
    ledger_snapshot,
    mark_product_channel_retire_pending,
    retirement_is_due,
)
from services.whatsapp_catalog_sync import (  # noqa: E402
    drain_whatsapp_catalog_sync,
    mark_product_pending_after_catalog_write,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeGraph:
    """Minimal Graph: one catalog, items keyed by retailer_id."""

    def __init__(self, items=None, *, reject_visibility=False, post_status=200, get_status=200):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.posts = []
        self.gets = []
        self.reject_visibility = reject_visibility
        self.post_status = post_status
        self.get_status = get_status

    def get(self, url, params=None, headers=None):
        self.gets.append((url, params))
        if self.get_status != 200:
            return FakeResponse(self.get_status, {"error": {"code": 190, "message": "token expired"}})
        if "/products" in url and params and params.get("filter"):
            rid = json.loads(params["filter"])["retailer_id"]["eq"]
            item = self.items.get(rid)
            data = []
            if item is not None:
                row = {"id": item["id"], "retailer_id": rid, "name": item.get("name"), "price": item.get("price"),
                       "currency": item.get("currency"), "availability": item.get("availability")}
                if "visibility" in (params.get("fields") or ""):
                    row["visibility"] = item.get("visibility", "published")
                data.append(row)
            return FakeResponse(200, {"data": data})
        if url.rstrip("/").endswith("CAT-GENERIC-001"):
            return FakeResponse(200, {"id": "CAT-GENERIC-001", "name": "متجر تجريبي عام", "product_count": len(self.items),
                                      "business": {"id": "BM-1", "name": "generic"}})
        return FakeResponse(200, {"data": []})

    def post(self, url, data=None, headers=None):
        self.posts.append((url, dict(data or {})))
        if self.post_status != 200:
            return FakeResponse(self.post_status, {"error": {"code": 1, "message": "boom"}})
        meta_id = url.rstrip("/").split("/")[-1]
        if self.reject_visibility and "visibility" in (data or {}):
            return FakeResponse(400, {"error": {"code": 100, "message": "(#100) Param visibility is not valid"}})
        for item in self.items.values():
            if item["id"] == meta_id:
                item.update({k: v for k, v in (data or {}).items()})
                return FakeResponse(200, {"success": True})
        return FakeResponse(404, {"error": {"code": 100, "message": "unknown item"}})


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    t_a = Tenant(name="متجر تجريبي عام", is_active=True)
    t_b = Tenant(name="متجر تجريبي ثانٍ", is_active=True)
    session.add_all([t_a, t_b])
    session.commit()
    for tenant in (t_a, t_b):
        session.add(
            WhatsAppConnection(
                tenant_id=tenant.id,
                whatsapp_business_account_id=f"WABA-{tenant.id}",
                phone_number_id=f"PN-{tenant.id}",
                access_token="EAAB-test",
                meta_catalog_id="CAT-GENERIC-001" if tenant.id == t_a.id else "CAT-OTHER-002",
                catalog_enabled=True,
                extra_metadata={},
            )
        )
    session.commit()
    return session, t_a.id, t_b.id, engine


def _salla_product(session, tenant_id, *, ext="700100", title="حذاء رياضي أبيض", synced=True, svid="591001"):
    product = Product(
        tenant_id=tenant_id,
        external_id=ext,
        title=title,
        price="249",
        in_stock=True,
        stock_quantity=4,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="active",
        extra_metadata={"currency": "SAR", "status": "sale", "image_url": "https://cdn.example/shoe.jpg",
                        "product_url": "https://store.example/p/shoe"},
        sync_status="synced" if synced else None,
        last_synced_at=datetime.now(timezone.utc) if synced else None,
    )
    session.add(product)
    session.flush()
    variant = ProductVariant(
        tenant_id=tenant_id,
        product_id=product.id,
        salla_variant_id=svid,
        retailer_id=f"{ext}-{svid}",
        price="249",
        currency="SAR",
        stock_quantity=4,
        in_stock=True,
        is_default=False,
    )
    session.add(variant)
    session.flush()
    if synced:
        session.add(
            MetaCatalogMembership(
                tenant_id=tenant_id,
                catalog_id="CAT-GENERIC-001",
                retailer_id=f"{ext}-{svid}",
                product_id=product.id,
                variant_id=variant.id,
                salla_variant_id=svid,
                meta_item_id=f"META-{ext}-{svid}",
                verified_at=datetime.now(timezone.utc),
                provenance="salla_variant_push",
            )
        )
        product.extra_metadata = {
            **product.extra_metadata,
            "sync_meta": {
                "content_verified": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "expected_payloads_by_retailer_id": {
                    f"{ext}-{svid}": {"price": 24900, "currency": "SAR", "availability": "in stock"}
                },
            },
        }
    session.commit()
    return product


def _native_product(session, tenant_id, *, title="عطر ورد 100ml"):
    product = Product(
        tenant_id=tenant_id,
        title=title,
        price="320",
        in_stock=True,
        stock_quantity=2,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        catalog_status="active",
        meta_retailer_id="nahla_p_native",
        meta_item_id="META-NATIVE-1",
        sync_status="synced",
        last_synced_at=datetime.now(timezone.utc),
        extra_metadata={"currency": "SAR", "sync_meta": {"content_verified": True,
                        "expected_payloads_by_retailer_id": {"nahla_p_native": {"price": 32000, "currency": "SAR", "availability": "in stock"}}}},
    )
    session.add(product)
    session.commit()
    return product


def _conn(session, tenant_id):
    return session.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()


def _ledger_rows(session, tenant_id, status=None):
    q = session.query(CatalogChannelRetirement).filter(CatalogChannelRetirement.tenant_id == tenant_id)
    if status:
        q = q.filter(CatalogChannelRetirement.status == status)
    return q.order_by(CatalogChannelRetirement.id.asc()).all()


_READY = patch(
    "services.whatsapp_catalog_sync.get_entitlements",
    lambda *a, **k: SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync"),
)


# ── Graph-level retirement ────────────────────────────────────────────────

def test_retire_sets_out_of_stock_and_staging_then_verifies():
    graph = FakeGraph({"700100-591001": {"id": "META-1", "price": "249.00 SAR", "currency": "SAR", "availability": "in stock"}})
    conn = SimpleNamespace(tenant_id=9, meta_catalog_id="CAT-GENERIC-001", access_token="EAAB-test", extra_metadata={})
    with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
        res = retire_meta_catalog_item(conn, "CAT-GENERIC-001", "700100-591001", "META-1", client=graph)
    assert res["ok"] is True and res["verified"] is True
    assert res["action"] == "retire_update"
    assert len(graph.posts) == 1
    url, body = graph.posts[0]
    assert url.endswith("/META-1")
    assert body == {"availability": RETIRED_AVAILABILITY, "visibility": RETIRED_VISIBILITY}
    assert graph.items["700100-591001"]["availability"] == "out of stock"


def test_retire_falls_back_when_visibility_param_rejected():
    graph = FakeGraph({"700100-591001": {"id": "META-1", "availability": "in stock"}}, reject_visibility=True)
    conn = SimpleNamespace(tenant_id=9, meta_catalog_id="CAT-GENERIC-001", access_token="EAAB-test", extra_metadata={})
    with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
        res = retire_meta_catalog_item(conn, "CAT-GENERIC-001", "700100-591001", "META-1", client=graph)
    assert res["ok"] is True
    assert res["visibility_applied"] is False
    assert graph.posts[-1][1] == {"availability": RETIRED_AVAILABILITY}


def test_retire_absent_item_makes_no_write():
    graph = FakeGraph({})
    conn = SimpleNamespace(tenant_id=9, meta_catalog_id="CAT-GENERIC-001", access_token="EAAB-test", extra_metadata={})
    with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
        res = retire_meta_catalog_item(conn, "CAT-GENERIC-001", "700100-591001", None, client=graph)
    assert res["ok"] is True and res["action"] == "absent"
    assert graph.posts == []


def test_retire_refuses_mismatched_meta_item_id():
    graph = FakeGraph({"700100-591001": {"id": "META-OTHER", "availability": "in stock"}})
    conn = SimpleNamespace(tenant_id=9, meta_catalog_id="CAT-GENERIC-001", access_token="EAAB-test", extra_metadata={})
    with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
        res = retire_meta_catalog_item(conn, "CAT-GENERIC-001", "700100-591001", "META-1", client=graph)
    assert res["ok"] is False and res["error"] == "meta_item_id_mismatch"
    assert graph.posts == []


# ── Product-row request ───────────────────────────────────────────────────

def test_merchant_hidden_synced_product_requests_retirement():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        assert mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN) is True
        session.commit()
        sm = product.extra_metadata["sync_meta"]
        assert sm["retire_pending"] is True and sm["retire_reason"] == REASON_MERCHANT_HIDDEN
        assert retirement_is_due(product) is True
        # idempotent
        assert mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN) is True
    finally:
        session.close(); engine.dispose()


def test_eligible_or_never_published_rows_are_not_retired():
    session, t_a, _t_b, engine = _make_db()
    try:
        live = _salla_product(session, t_a, ext="700200")
        assert mark_product_channel_retire_pending(session, live) is False
        never = _salla_product(session, t_a, ext="700300", synced=False)
        never.catalog_status = "merchant_hidden"
        never.merchant_hidden_at = datetime.now(timezone.utc)
        assert mark_product_channel_retire_pending(session, never) is False
        assert channel_identities_for_product(session, never) == []
    finally:
        session.close(); engine.dispose()


def test_salla_hidden_status_routes_to_retirement_not_publish():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700400", title="قميص قطني أزرق")
        product.extra_metadata = {**product.extra_metadata, "status": "hidden"}
        assert is_whatsapp_channel_publish_eligible(product) is False
        assert mark_product_pending_after_catalog_write(session, product) is False
        session.commit()
        assert product.extra_metadata["sync_meta"]["retire_pending"] is True
        assert product.extra_metadata["sync_meta"]["retire_reason"] == "source_hidden"
        assert product.sync_status == "synced"  # status unchanged until the drain proves the withdrawal
    finally:
        session.close(); engine.dispose()


@_READY
def test_drain_retires_hidden_product_and_stamps_row():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700500")
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product)
        session.commit()
        graph = FakeGraph({"700500-591001": {"id": "META-700500-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            out = drain_whatsapp_catalog_sync(session, t_a, client=graph)
        assert out["retirements"]["retired"] == 1
        session.refresh(product)
        assert product.sync_status == SYNC_STATUS_RETIRED
        assert product.extra_metadata["sync_meta"]["retire_pending"] is False
        assert product.extra_metadata["sync_meta"]["channel_retired_at"]
        assert graph.items["700500-591001"]["availability"] == "out of stock"
        assert graph.items["700500-591001"]["visibility"] == "staging"
        # second drain: nothing left to do, no extra Graph writes
        posts_before = len(graph.posts)
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            drain_whatsapp_catalog_sync(session, t_a, client=graph)
        assert len(graph.posts) == posts_before
    finally:
        session.close(); engine.dispose()


def test_retirement_failure_backs_off_and_exhausts():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700600")
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product)
        session.commit()
        graph = FakeGraph({"700600-591001": {"id": "META-700600-591001", "availability": "in stock"}}, post_status=500)
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            for i in range(RETIRE_MAX_ATTEMPTS):
                product.extra_metadata["sync_meta"]["next_retire_at"] = None  # force due
                session.commit()
                res = attempt_product_channel_retirement(session, t_a, product.id, client=graph)
                assert res["ok"] is False
        session.refresh(product)
        sm = product.extra_metadata["sync_meta"]
        assert sm["retire_attempts"] == RETIRE_MAX_ATTEMPTS
        assert sm["retire_exhausted"] is True
        assert sm["retire_pending"] is True  # still owed, surfaced as needs attention
        assert product.sync_status == "synced"
    finally:
        session.close(); engine.dispose()


def test_restored_before_drain_is_handed_back_to_publish():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700700")
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product)
        session.commit()
        product.catalog_status = "active"
        product.merchant_hidden_at = None
        session.commit()
        graph = FakeGraph({})
        res = attempt_product_channel_retirement(session, t_a, product.id, client=graph)
        assert res["skipped"] is True and res["error_code"] == "eligible_again"
        assert graph.posts == []
        assert product.extra_metadata["sync_meta"]["retire_pending"] is False
    finally:
        session.close(); engine.dispose()


# ── Durable ledger for deleted rows (table, no cap, survives other writers) ──

def test_delete_ledger_survives_row_delete_and_is_drained():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700800")
        identities = channel_identities_for_product(session, product)
        assert [i["retailer_id"] for i in identities] == ["700800-591001"]
        assert identities[0]["meta_item_id"] == "META-700800-591001"
        added = enqueue_channel_retirement_ledger(session, t_a, identities, reason=REASON_SOURCE_DELETED)
        assert added == 1
        session.delete(product)
        session.commit()
        rows = _ledger_rows(session, t_a)
        assert len(rows) == 1 and rows[0].status == LEDGER_STATUS_PENDING
        assert rows[0].product_id == identities[0]["product_id"]
        # duplicate enqueue merges (re-opens the same row)
        assert enqueue_channel_retirement_ledger(session, t_a, identities, reason=REASON_SOURCE_DELETED) == 0
        session.commit()
        assert len(_ledger_rows(session, t_a)) == 1
        graph = FakeGraph({"700800-591001": {"id": "META-700800-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            out = drain_channel_retirement_ledger(session, t_a, client=graph)
        assert out["retired"] == 1 and out["remaining"] == 0
        assert graph.items["700800-591001"]["availability"] == "out of stock"
        rows = _ledger_rows(session, t_a)
        assert rows[0].status == LEDGER_STATUS_DONE and rows[0].done_at is not None
        assert ledger_snapshot(session, t_a)["done_total"] == 1
    finally:
        session.close(); engine.dispose()


def test_ledger_has_no_cap_every_identity_of_a_deleted_product_is_recorded():
    session, t_a, _t_b, engine = _make_db()
    try:
        identities = [
            {"retailer_id": f"9{i:05d}-1", "meta_item_id": f"META-{i}", "catalog_id": "CAT-GENERIC-001", "product_id": 1000 + i}
            for i in range(620)
        ]
        added = enqueue_channel_retirement_ledger(session, t_a, identities, reason=REASON_SOURCE_DELETED)
        session.commit()
        assert added == 620
        assert len(_ledger_rows(session, t_a, LEDGER_STATUS_PENDING)) == 620
        assert ledger_snapshot(session, t_a)["pending"] == 620
    finally:
        session.close(); engine.dispose()


def test_ledger_outage_backs_off_exhausts_and_is_retried_after_reconcile_reset():
    from services.whatsapp_catalog_retirement import reset_exhausted_ledger_entries

    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700850")
        enqueue_channel_retirement_ledger(session, t_a, channel_identities_for_product(session, product),
                                          reason=REASON_SOURCE_DELETED)
        session.delete(product); session.commit()
        down = FakeGraph({"700850-591001": {"id": "META-700850-591001", "availability": "in stock"}}, post_status=503)
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            for _ in range(RETIRE_MAX_ATTEMPTS):
                row = _ledger_rows(session, t_a)[0]
                row.next_attempt_at = None  # force due
                session.commit()
                out = drain_channel_retirement_ledger(session, t_a, client=down)
                assert out["failed"] == 1
        row = _ledger_rows(session, t_a)[0]
        assert row.status == LEDGER_STATUS_EXHAUSTED and row.attempts == RETIRE_MAX_ATTEMPTS
        assert row.last_error == "meta_http_error"
        # nothing is processed while exhausted
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            assert drain_channel_retirement_ledger(session, t_a, client=down)["processed"] == 0
        # reconciliation gives it a fresh budget; Meta is back → retired
        assert reset_exhausted_ledger_entries(session, t_a) == 1
        session.commit()
        up = FakeGraph({"700850-591001": {"id": "META-700850-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            out = drain_channel_retirement_ledger(session, t_a, client=up)
        assert out["retired"] == 1
        assert _ledger_rows(session, t_a)[0].status == LEDGER_STATUS_DONE
    finally:
        session.close(); engine.dispose()


def test_ledger_survives_reconnect_bind_token_refresh_reconcile_and_drain_writers():
    """Writers of whatsapp_connections.extra_metadata (reconnect bind result,
    onboarding ensure, token refresh context, reconcile snapshot) cannot touch
    the ledger: it lives in its own table."""
    from services.meta_catalog_onboarding import _persist_ensure
    from services.meta_catalog_reconnect import _persist_bind_result
    from services.whatsapp_catalog_reconcile import _persist_snapshot

    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700870")
        stale_conn = _conn(session, t_a)  # a row loaded before the delete, as a reconnect worker would hold
        enqueue_channel_retirement_ledger(session, t_a, channel_identities_for_product(session, product),
                                          reason=REASON_SOURCE_DELETED)
        session.delete(product); session.commit()
        # reconnect bind + onboarding ensure rewrite the whole JSON column from the stale object
        _persist_bind_result(stale_conn, {"ok": True, "link_status": "linked", "at": datetime.now(timezone.utc).isoformat()})
        _persist_ensure(stale_conn, {"ok": True, "at": datetime.now(timezone.utc).isoformat()})
        session.commit()
        # token refresh style writer: replaces extra_metadata wholesale
        conn = _conn(session, t_a)
        conn.extra_metadata = {"token_status": "ok", "oauth_debug": {"is_valid": True}}
        session.commit()
        # reconcile snapshot writer
        _persist_snapshot(session, t_a, {"at": datetime.now(timezone.utc).isoformat(), "ok": True})
        session.commit()
        rows = _ledger_rows(session, t_a)
        assert len(rows) == 1 and rows[0].status == LEDGER_STATUS_PENDING
        graph = FakeGraph({"700870-591001": {"id": "META-700870-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            out = drain_channel_retirement_ledger(session, t_a, client=graph)
        assert out["retired"] == 1
        assert graph.items["700870-591001"]["availability"] == "out of stock"
    finally:
        session.close(); engine.dispose()


def test_store_sync_delete_writes_ledger_before_deleting_row():
    from services.store_sync import StoreSyncService

    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="700900")
        svc = StoreSyncService(session, t_a)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain") as sched:
            asyncio.run(svc.handle_product_deleted("700900"))
        assert session.query(Product).filter_by(tenant_id=t_a, external_id="700900").count() == 0
        rows = _ledger_rows(session, t_a)
        assert [r.retailer_id for r in rows] == ["700900-591001"]
        assert rows[0].reason == REASON_SOURCE_DELETED
        sched.assert_called_once_with(t_a)
        # a product that was never published leaves no ledger entry
        session.expire_all()
        never = _salla_product(session, t_a, ext="701000", synced=False, svid="591002")
        asyncio.run(svc.handle_product_deleted("701000"))
        assert len(_ledger_rows(session, t_a)) == 1
    finally:
        session.close(); engine.dispose()


def test_store_sync_delete_is_refused_when_the_ledger_cannot_be_written():
    """No delete without a durable retirement record: the event is retried."""
    from services.store_sync import StoreSyncService

    session, t_a, _t_b, engine = _make_db()
    try:
        _salla_product(session, t_a, ext="700950")
        svc = StoreSyncService(session, t_a)
        with patch("services.whatsapp_catalog_retirement.enqueue_channel_retirement_ledger",
                   side_effect=RuntimeError("db_down")):
            with pytest.raises(RuntimeError):
                asyncio.run(svc.handle_product_deleted("700950"))
        session.rollback()
        assert session.query(Product).filter_by(tenant_id=t_a, external_id="700950").count() == 1
        assert _ledger_rows(session, t_a) == []
    finally:
        session.close(); engine.dispose()


def test_ledger_and_identities_are_tenant_isolated():
    session, t_a, t_b, engine = _make_db()
    try:
        prod_a = _salla_product(session, t_a, ext="800100")
        prod_b = _salla_product(session, t_b, ext="800100")  # same Salla id in another store
        ids_a = channel_identities_for_product(session, prod_a)
        ids_b = channel_identities_for_product(session, prod_b)
        assert ids_a[0]["product_id"] == prod_a.id and ids_b[0]["product_id"] == prod_b.id
        enqueue_channel_retirement_ledger(session, t_a, ids_a, reason=REASON_SOURCE_DELETED)
        session.commit()
        assert _ledger_rows(session, t_b) == []
        graph_b = FakeGraph({"800100-591001": {"id": "META-800100-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-OTHER-002", "tok")):
            out_b = drain_channel_retirement_ledger(session, t_b, client=graph_b)
        assert out_b["processed"] == 0 and graph_b.posts == []
    finally:
        session.close(); engine.dispose()


def test_imported_meta_rows_are_never_retired_by_this_path():
    session, t_a, _t_b, engine = _make_db()
    try:
        imported = Product(
            tenant_id=t_a, title="فستان مستورد", price="500", source="meta", ownership_mode="meta_readonly",
            catalog_status="merchant_hidden", merchant_hidden_at=datetime.now(timezone.utc),
            meta_item_id="META-IMPORTED-1", sync_status=None, extra_metadata={},
        )
        session.add(imported)
        session.commit()
        assert channel_identities_for_product(session, imported) == []
        assert mark_product_channel_retire_pending(session, imported, reason=REASON_MERCHANT_HIDDEN) is False
        assert mark_product_pending_after_catalog_write(session, imported) is False
        assert "sync_meta" not in (imported.extra_metadata or {})
    finally:
        session.close(); engine.dispose()


def test_native_identities_use_product_meta_item():
    session, t_a, _t_b, engine = _make_db()
    try:
        product = _native_product(session, t_a)
        ids = channel_identities_for_product(session, product)
        assert len(ids) == 1
        assert ids[0]["retailer_id"] == "nahla_p_native"
        assert ids[0]["meta_item_id"] == "META-NATIVE-1"
    finally:
        session.close(); engine.dispose()


def test_ledger_entry_added_during_graph_io_survives_the_drain_merge():
    """A product.deleted webhook landing while the drain is talking to Graph
    is a new row the drain never touches (updates are by primary key)."""
    session, t_a, _t_b, engine = _make_db()
    try:
        first = _salla_product(session, t_a, ext="810100")
        enqueue_channel_retirement_ledger(session, t_a, channel_identities_for_product(session, first),
                                          reason=REASON_SOURCE_DELETED)
        session.commit()
        later = _salla_product(session, t_a, ext="810200", svid="591009")
        later_ids = channel_identities_for_product(session, later)

        class RacingGraph(FakeGraph):
            def post(self, url, data=None, headers=None):
                enqueue_channel_retirement_ledger(session, t_a, later_ids, reason=REASON_SOURCE_DELETED)
                session.commit()
                return super().post(url, data=data, headers=headers)

        graph = RacingGraph({"810100-591001": {"id": "META-810100-591001", "availability": "in stock"}})
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-GENERIC-001", "tok")):
            out = drain_channel_retirement_ledger(session, t_a, client=graph)
        assert out["retired"] == 1
        remaining = [r.retailer_id for r in _ledger_rows(session, t_a, LEDGER_STATUS_PENDING)]
        assert remaining == ["810200-591009"]
    finally:
        session.close(); engine.dispose()


def test_rehide_after_exhausted_or_restore_starts_a_fresh_retirement_budget():
    from services.native_meta_sync_orchestrator import mark_native_meta_sync_pending

    session, t_a, _t_b, engine = _make_db()
    try:
        product = _salla_product(session, t_a, ext="820100")
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product)
        sm = dict(product.extra_metadata["sync_meta"]); sm.update({"retire_exhausted": True, "retire_attempts": RETIRE_MAX_ATTEMPTS})
        product.extra_metadata = {**product.extra_metadata, "sync_meta": sm}
        session.commit()
        assert retirement_is_due(product) is False
        # re-hide (same reason) after exhaustion → budget resets
        assert mark_product_channel_retire_pending(session, product) is True
        assert product.extra_metadata["sync_meta"]["retire_exhausted"] is False
        assert retirement_is_due(product) is True
        # restore → publish queue clears the withdrawal request entirely
        product.catalog_status = "active"
        product.merchant_hidden_at = None
        assert mark_native_meta_sync_pending(session, product) is True
        session.commit()
        sm = product.extra_metadata["sync_meta"]
        assert sm["retire_pending"] is False and sm["retire_exhausted"] is False and sm["retire_attempts"] == 0
        assert product.sync_status == "pending"
    finally:
        session.close(); engine.dispose()
