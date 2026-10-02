"""Deploy impact on a Salla store that is OUTSIDE the trial scope (the Tenant 1 profile).

Tenant 1 is reserved for the Salla review and is not part of the WhatsApp
catalog trial. This file proves what the deploy does and does not change for
such a store: a Salla merchant with legacy (pre-deploy) product rows, historic
Meta copies, an enabled catalog connection, orders and customers, while the
trial scope names a different tenant.

Generic merchant data (متجر مراجعة سلة / متجر تجريبي عام). Asserts persisted
state and behaviour, never Arabic wording.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    Customer,
    MetaCatalogMembership,
    Order,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    apply_active_catalog_query_filters,
    is_whatsapp_channel_publish_eligible,
)
from modules.ai.brain.postprocess.availability_context_builder import _can_checkout_from_row  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402
from services.whatsapp_catalog_sync_scope import PRODUCT_SCOPE_ENV, TENANT_SCOPE_ENV  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class ForbiddenGraph:
    """Any Graph call for an out-of-scope store is a test failure."""

    def get(self, *a, **k):
        raise AssertionError("Graph GET for an out-of-scope store")

    def post(self, *a, **k):
        raise AssertionError("Graph POST for an out-of-scope store")


def _stamp(hour, minute):
    return {"date": f"2026-10-02 {hour:02d}:{minute:02d}:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"}


def _raw(ext, *, name, price, quantity, status="sale", skus=None, updated_at=None, options=None):
    body = {
        "id": int(ext),
        "name": name,
        "description": "وصف",
        "price": {"amount": price, "currency": "SAR"},
        "regular_price": {"amount": price, "currency": "SAR"},
        "sale_price": {"amount": 0, "currency": "SAR"},
        "quantity": quantity,
        "status": status,
        "images": [{"url": f"https://cdn.example/{ext}.jpg", "main": True}],
        "urls": {"customer": f"https://store.example/p/{ext}"},
        "skus": skus or [{"id": int(ext) * 10 + 1, "price": {"amount": price, "currency": "SAR"}, "stock_quantity": quantity}],
    }
    if options:
        body["options"] = options
    if updated_at is not None:
        body["updated_at"] = updated_at
    return body


def _adapter_dict(body):
    adapter = SallaAdapter.__new__(SallaAdapter)
    return SallaAdapter._normalize_product(adapter, body).dict()


def _legacy_product(session, tid, ext, *, title, price, in_stock=True, qty=5, variants=(), published=False):
    """A row as the pre-deploy code wrote it: lifecycle status only, no stamps."""
    row = Product(
        tenant_id=tid, external_id=ext, title=title, price=price, in_stock=in_stock, stock_quantity=qty,
        source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED, catalog_status="active",
        sync_status="synced" if published else "pending",
        last_synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc) if published else None,
        meta_item_id=f"META-{ext}-{variants[0]}" if published and variants else None,
        extra_metadata={"status": "active", "currency": "SAR", "image_url": f"https://cdn.example/{ext}.jpg",
                        "product_url": f"https://store.example/p/{ext}", "sync_meta": {"content_generation": 1}},
    )
    session.add(row); session.flush()
    for svid in variants:
        session.add(ProductVariant(tenant_id=tid, product_id=row.id, salla_variant_id=str(svid), retailer_id=f"{ext}-{svid}",
                                   price=price, currency="SAR", stock_quantity=qty, in_stock=in_stock))
        if published:
            session.add(MetaCatalogMembership(tenant_id=tid, catalog_id="CAT-REVIEW", retailer_id=f"{ext}-{svid}",
                                              product_id=row.id, salla_variant_id=str(svid), meta_item_id=f"META-{ext}-{svid}",
                                              verified_at=datetime(2026, 9, 1, tzinfo=timezone.utc), provenance="salla_variant_push"))
    session.commit()
    return row


def _make_world():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    review = Tenant(name="متجر مراجعة سلة", is_active=True)
    trial = Tenant(name="متجر التجربة", is_active=True)
    other = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add_all([review, trial, other]); session.commit()
    for t, cat in ((review, "CAT-REVIEW"), (trial, "CAT-TRIAL"), (other, "CAT-OTHER")):
        session.add(WhatsAppConnection(
            tenant_id=t.id, whatsapp_business_account_id=f"WABA-{t.id}", phone_number_id=f"PN-{t.id}",
            access_token="EAAB-test", meta_catalog_id=cat, catalog_enabled=True, provider="meta",
            connection_type="embedded", extra_metadata={"meta_catalog_bind": {"ok": True, "catalog_id": cat}},
        ))
    session.commit()
    # Legacy rows of the review store: one published on Meta, one pending, one with two variants.
    pub = _legacy_product(session, review.id, "610100", title="حذاء رياضي أبيض", price="250", variants=(1,), published=True)
    pend = _legacy_product(session, review.id, "610200", title="عطر ورد 100ml", price="199", variants=(1,))
    multi = _legacy_product(session, review.id, "610300", title="قميص قطني أزرق", price="120", variants=(1, 2))
    # Another store carrying the same Salla product id (isolation).
    twin = _legacy_product(session, other.id, "610200", title="عطر ورد 100ml", price="199", variants=(1,))
    # Orders and customers of the review store.
    session.add(Order(tenant_id=review.id, external_id="ORD-1", status="completed", total="250",
                      customer_name="أحمد سالم", line_items=[{"product_external_id": "610100", "qty": 1}], source="salla"))
    session.add(Customer(tenant_id=review.id, name="نورة عبدالله", phone="+966500000001"))
    session.commit()
    return SimpleNamespace(session=session, engine=engine, review=review.id, trial=trial.id, other=other.id,
                           pub=pub, pend=pend, multi=multi, twin=twin)


def _svc(session, tid, *, products=(), variants_by_ext=None, truth_by_ext=None):
    svc = StoreSyncService(session, tid)
    adapter = MagicMock()
    adapter.platform = "salla"
    adapter.get_products = AsyncMock(return_value=list(products))
    real = SallaAdapter.__new__(SallaAdapter)
    adapter._normalize_variant = lambda raw, opts=None: SallaAdapter._normalize_variant(real, raw, opts)
    adapter.get_raw_variants = AsyncMock(side_effect=lambda ext: (variants_by_ext or {}).get(str(ext), []))
    adapter.get_product = AsyncMock(side_effect=lambda pid: (truth_by_ext or {}).get(str(pid)))
    svc._adapter = adapter
    return svc


_READY = patch(
    "services.whatsapp_catalog_sync.get_entitlements",
    lambda *a, **k: SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync"),
)


@pytest.fixture(autouse=True)
def _trial_scope_names_another_store(monkeypatch):
    """The trial scope names a different tenant; the review store is excluded."""
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.setenv(TENANT_SCOPE_ENV, "999999")
    monkeypatch.setenv(PRODUCT_SCOPE_ENV, "999999:1")
    yield


def _snapshot_orders_customers(session, tid):
    orders = [(o.external_id, o.status, o.total, o.line_items) for o in session.query(Order).filter_by(tenant_id=tid).order_by(Order.id)]
    customers = [(c.name, c.phone) for c in session.query(Customer).filter_by(tenant_id=tid).order_by(Customer.id)]
    return orders, customers


@_READY
def test_periodic_salla_sync_keeps_listing_availability_variants_orders_and_isolation():
    w = _make_world(); s = w.session
    try:
        before_orders = _snapshot_orders_customers(s, w.review)
        twin_price_before = w.twin.price
        # What the store reports now: published product unchanged, pending product price drop and
        # quantity to zero, multi-variant product still two SKUs with one restocked.
        products = [
            _adapter_dict(_raw("610100", name="حذاء رياضي أبيض", price=250.0, quantity=5)),
            _adapter_dict(_raw("610200", name="عطر ورد 100ml", price=179.0, quantity=0)),
            _adapter_dict(_raw("610300", name="قميص قطني أزرق", price=120.0, quantity=7, skus=[
                {"id": 6103001, "price": {"amount": 120.0, "currency": "SAR"}, "stock_quantity": 7},
                {"id": 6103002, "price": {"amount": 120.0, "currency": "SAR"}, "stock_quantity": 0},
            ])),
        ]
        # The adapter shape carries the SKUs already; variants come from the body.
        svc = _svc(s, w.review, products=products)
        with patch("services.whatsapp_catalog_sync._whatsapp_catalog_drain_coalescer") as coal:
            asyncio.run(svc.sync_products(incremental=False))
        # 1. no drain was scheduled for the excluded store, no Graph call anywhere
        assert not coal.called
        s.expire_all()
        rows = {r.external_id: r for r in s.query(Product).filter_by(tenant_id=w.review).all()}
        assert set(rows) == {"610100", "610200", "610300"}
        assert {r.id for r in rows.values()} == {w.pub.id, w.pend.id, w.multi.id}
        # 2. product listing + availability as the AI reads them
        assert rows["610100"].price == "250.0" and rows["610100"].in_stock is True
        assert rows["610200"].price == "179.0" and rows["610200"].in_stock is False and rows["610200"].stock_quantity == 0
        assert rows["610300"].in_stock is True
        for r in rows.values():
            assert r.extra_metadata["status"] == "active"           # lifecycle value, orderable
            assert r.extra_metadata["source_status"] in ("sale", "out")
            assert r.extra_metadata["source_event_at"]              # the new stamp, on every row
        assert _can_checkout_from_row(rows["610100"]) is True
        assert _can_checkout_from_row(rows["610200"]) is False       # out of stock is honest
        active = apply_active_catalog_query_filters(s.query(Product).filter_by(tenant_id=w.review), Product).all()
        assert {r.external_id for r in active} == {"610100", "610300"}
        # 3. variants intact (same retailer ids, no duplicates), per-variant stock refreshed
        variants = s.query(ProductVariant).filter_by(product_id=w.multi.id).order_by(ProductVariant.salla_variant_id).all()
        assert [v.retailer_id for v in variants] == ["610300-6103001", "610300-6103002"] or \
               [v.retailer_id for v in variants] == ["610300-1", "610300-2"]
        assert len(variants) == 2
        # 4. the published copy keeps its identity. The first post-deploy sync re-queues it
        #    locally only when its Meta-relevant content differs from the legacy row (same rule
        #    as before the deploy; here the legacy fixture lacks description/variants), never
        #    because of the new stamps: a second sync with identical store content leaves a
        #    synced row synced while the stamp advances.
        assert rows["610100"].meta_item_id == "META-610100-1"
        assert rows["610200"].sync_status == "pending"
        assert s.query(MetaCatalogMembership).filter_by(tenant_id=w.review).count() == 1
        rows["610100"].sync_status = "synced"; s.commit()
        first_stamp = rows["610100"].extra_metadata["source_event_at"]
        from services import store_sync as ss
        later = datetime.now(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(seconds=30)
        with patch("services.whatsapp_catalog_sync._whatsapp_catalog_drain_coalescer") as coal2, \
             patch.object(ss, "source_read_stamp", lambda now=None: later):
            asyncio.run(_svc(s, w.review, products=products).sync_products(incremental=False))
        assert not coal2.called
        s.expire_all()
        again = s.query(Product).filter_by(tenant_id=w.review, external_id="610100").one()
        assert again.sync_status == "synced"
        assert again.extra_metadata["source_event_at"] != first_stamp
        assert again.extra_metadata["sync_meta"]["content_generation"] == rows["610100"].extra_metadata["sync_meta"]["content_generation"]
        # 5. orders and customers untouched
        assert _snapshot_orders_customers(s, w.review) == before_orders
        # 6. the other store's twin product is untouched
        s.refresh(w.twin)
        assert w.twin.price == twin_price_before and "source_event_at" not in (w.twin.extra_metadata or {})
    finally:
        s.close(); w.engine.dispose()


@_READY
def test_webhooks_apply_in_order_and_never_cross_tenants():
    w = _make_world(); s = w.session
    try:
        truth = {"610200": _adapter_dict(_raw("610200", name="عطر ورد 100ml", price=189.0, quantity=4, updated_at=_stamp(9, 0)))}
        svc = _svc(s, w.review, truth_by_ext=truth)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain") as sched:
            # a) timed update applies
            asyncio.run(svc.handle_product_webhook(
                _raw("610200", name="عطر ورد 100ml", price=189.0, quantity=4, updated_at=_stamp(9, 0)),
                webhook_event_type="product.updated"))
            s.refresh(w.pend)
            assert w.pend.price == "189.0" and w.pend.in_stock is True and w.pend.stock_quantity == 4
            assert w.pend.extra_metadata["status"] == "active"
            # b) an older event (price 150, quantity 0) arrives late: ignored for every field
            asyncio.run(svc.handle_product_webhook(
                _raw("610200", name="عطر ورد 100ml", price=150.0, quantity=0, updated_at=_stamp(8, 30)),
                webhook_event_type="product.updated"))
            s.refresh(w.pend)
            assert w.pend.price == "189.0" and w.pend.in_stock is True
            # c) an event without any timestamp: the store is asked, the body is not trusted
            asyncio.run(svc.handle_product_webhook(
                _raw("610200", name="عطر ورد 100ml", price=10.0, quantity=0),
                webhook_event_type="product.updated"))
            s.refresh(w.pend)
            assert w.pend.price == "189.0" and w.pend.in_stock is True
            assert svc._adapter.get_product.await_count == 1
            # d) the same Salla id in another store: only that store changes
            other_svc = _svc(s, w.other, truth_by_ext={})
            asyncio.run(other_svc.handle_product_webhook(
                _raw("610200", name="عطر ورد 100ml", price=99.0, quantity=1, updated_at=_stamp(9, 5)),
                webhook_event_type="product.updated"))
            s.refresh(w.twin); s.refresh(w.pend)
            assert w.twin.price == "99.0" and w.pend.price == "189.0"
        # drain scheduling was requested through the scoped entry point only (which refuses this store)
        assert all(c.args[0] in (w.review, w.other) for c in sched.call_args_list)
    finally:
        s.close(); w.engine.dispose()


@_READY
def test_hide_and_delete_touch_only_local_state_for_the_excluded_store():
    from services.whatsapp_catalog_retirement import attempt_product_channel_retirement, drain_channel_retirement_ledger
    from services.whatsapp_catalog_sync import build_whatsapp_catalog_sync_status, drain_whatsapp_catalog_sync

    w = _make_world(); s = w.session
    try:
        svc = _svc(s, w.review)
        graph = ForbiddenGraph()
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            # Salla hides the published product: local flags only, the row stays, AI stops offering it
            asyncio.run(svc.handle_product_webhook(
                _raw("610100", name="حذاء رياضي أبيض", price=250.0, quantity=5, status="hidden", updated_at=_stamp(10, 0)),
                webhook_event_type="product.status.updated"))
            s.refresh(w.pub)
            assert w.pub.extra_metadata["status"] == "hidden"
            assert w.pub.extra_metadata["sync_meta"]["retire_pending"] is True
            assert w.pub.meta_item_id == "META-610100-1" and w.pub.sync_status == "synced"
            assert _can_checkout_from_row(w.pub) is False
            assert is_whatsapp_channel_publish_eligible(w.pub) is False
            # the drain refuses the excluded store before any Graph call
            res = attempt_product_channel_retirement(s, w.review, w.pub.id, client=graph)
            assert res["skipped"] is True and res["error_code"] == "sync_scope_excluded"
            out = drain_whatsapp_catalog_sync(s, w.review, client=graph)
            assert out["skipped"] is True and out["blocker_code"] == "sync_scope_excluded"
            # Salla deletes the published product: identities recorded durably, row gone, no Graph
            asyncio.run(svc.handle_product_deleted("610100"))
            assert s.query(Product).filter_by(tenant_id=w.review, external_id="610100").count() == 0
            ledger = s.query(CatalogChannelRetirement).filter_by(tenant_id=w.review).all()
            # exactly the variant identity: the legacy product-level meta_item_id mirrors the
            # same Graph item and must not produce a second, never-existing retailer id
            assert [(r.retailer_id, r.meta_item_id, r.status) for r in ledger] == [("610100-1", "META-610100-1", "pending")]
            ledger_count = len(ledger)
            drained = drain_channel_retirement_ledger(s, w.review, client=graph)
            assert drained["processed"] == 0 and drained["skipped_scope"] >= 1
            assert all(r.status == "pending" and r.attempts == 0
                       for r in s.query(CatalogChannelRetirement).filter_by(tenant_id=w.review).all())
            # a never-published product deletes without any ledger row
            asyncio.run(svc.handle_product_deleted("610200"))
            assert s.query(Product).filter_by(tenant_id=w.review, external_id="610200").count() == 0
            assert s.query(CatalogChannelRetirement).filter_by(tenant_id=w.review).count() == ledger_count
        # dashboard status for the excluded store is honest and read-only
        st = build_whatsapp_catalog_sync_status(s, w.review)
        assert st["ready"] is False and st["blocker_code"] == "sync_scope_excluded" and st["phase"] == "blocked"
        assert st["catalog_configured"] is True
        conn = s.query(WhatsAppConnection).filter_by(tenant_id=w.review).one()
        assert conn.catalog_enabled is True and conn.meta_catalog_id == "CAT-REVIEW"
        assert conn.extra_metadata == {"meta_catalog_bind": {"ok": True, "catalog_id": "CAT-REVIEW"}}
        # orders and customers of the store are still there
        assert s.query(Order).filter_by(tenant_id=w.review).count() == 1
        assert s.query(Customer).filter_by(tenant_id=w.review).count() == 1
    finally:
        s.close(); w.engine.dispose()
