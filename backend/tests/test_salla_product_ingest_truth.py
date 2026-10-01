"""Salla → Nahla product truth: money objects, availability, source status, duplicate and late events.

Generic merchant data (متجر تجريبي عام). Asserts persisted state and queue
behaviour, never Arabic wording.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Product, ProductVariant, Tenant, WhatsAppConnection  # noqa: E402
from core.catalog import is_hidden_at_source, is_whatsapp_channel_publish_eligible  # noqa: E402
from services.store_sync import StoreSyncService, _normalise_product  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            whatsapp_business_account_id="WABA-1",
            phone_number_id="PN-1",
            access_token="EAAB-test",
            meta_catalog_id="CAT-GENERIC-001",
            catalog_enabled=True,
            extra_metadata={},
        )
    )
    session.commit()
    return session, tenant.id, engine


def _raw_webhook_product(**overrides):
    body = {
        "id": 900100,
        "name": "عطر ورد 100ml",
        "description": "عطر زهري",
        "price": {"amount": 199.0, "currency": "SAR"},
        "sale_price": {"amount": 0, "currency": "SAR"},
        "regular_price": {"amount": 199.0, "currency": "SAR"},
        "quantity": 0,
        "status": "sale",
        "images": [{"url": "https://cdn.example/rose.jpg", "main": True}],
        "urls": {"customer": "https://store.example/p/rose"},
        "skus": [{"id": 77001, "price": {"amount": 199.0, "currency": "SAR"}, "stock_quantity": 0}],
    }
    body.update(overrides)
    return body


# ── Normaliser truth ──────────────────────────────────────────────────────

def test_webhook_money_objects_never_become_dict_text():
    norm = _normalise_product(_raw_webhook_product())
    assert norm["price"] == "199.0"
    assert norm["regular_price"] == "199.0"
    assert norm["currency"] == "SAR"
    assert "{" not in norm["price"]


def test_webhook_quantity_zero_is_out_of_stock_and_unlimited_is_in_stock():
    assert _normalise_product(_raw_webhook_product(quantity=0))["in_stock"] is False
    assert _normalise_product(_raw_webhook_product(quantity=5))["in_stock"] is True
    assert _normalise_product(_raw_webhook_product(quantity=0, unlimited_quantity=True))["in_stock"] is True
    # adapter-normalised payloads keep their explicit flag
    assert _normalise_product({"id": "1", "title": "x", "price": "5", "in_stock": False, "quantity": 9})["in_stock"] is False


def test_source_status_carried_and_hidden_blocks_channel_publish():
    hidden = _normalise_product(_raw_webhook_product(status="hidden"))
    assert hidden["status"] == "hidden"
    row = Product(
        tenant_id=1, external_id="900100", title="عطر ورد 100ml", price="199", source="salla",
        catalog_status="active", extra_metadata={"status": "hidden"},
    )
    assert is_hidden_at_source(row) is True
    assert is_whatsapp_channel_publish_eligible(row) is False
    row.extra_metadata = {"status": "sale"}
    assert is_whatsapp_channel_publish_eligible(row) is True
    native = Product(tenant_id=1, title="x", price="5", source="manual", catalog_status="active", extra_metadata={"status": "hidden"})
    assert is_hidden_at_source(native) is False


def test_adapter_product_carries_source_status():
    adapter = SallaAdapter.__new__(SallaAdapter)
    normalized = SallaAdapter._normalize_product(adapter, _raw_webhook_product(status={"slug": "hidden"}))
    assert normalized.status == "hidden"
    assert normalized.in_stock is False
    assert normalized.price == 199.0


# ── Webhook flow on real rows ─────────────────────────────────────────────

def _svc(session, tenant_id):
    svc = StoreSyncService(session, tenant_id)
    adapter = MagicMock()
    adapter.platform = "salla"
    adapter.get_raw_variants = AsyncMock(return_value=[
        {"id": 77001, "price": {"amount": 199.0, "currency": "SAR"}, "quantity": 0},
    ])
    real = SallaAdapter.__new__(SallaAdapter)
    adapter._normalize_variant = lambda raw, opts=None: SallaAdapter._normalize_variant(real, raw, opts)
    svc._adapter = adapter
    return svc


def test_created_then_duplicate_event_marks_pending_once():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(), webhook_event_type="product.created"))
        row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
        assert row.price == "199.0"
        assert row.in_stock is False
        assert row.sync_status == "pending"
        gen1 = row.extra_metadata["sync_meta"]["content_generation"]
        variants = session.query(ProductVariant).filter_by(product_id=row.id).all()
        assert [v.salla_variant_id for v in variants] == ["77001"]
        assert variants[0].retailer_id == "900100-77001"
        # same event delivered twice (Salla retries): no new generation
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(), webhook_event_type="product.created"))
        session.refresh(row)
        assert row.extra_metadata["sync_meta"]["content_generation"] == gen1
    finally:
        session.close(); engine.dispose()


def test_price_image_and_stock_updates_bump_generation_and_hidden_requests_retirement():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(quantity=3), webhook_event_type="product.created"))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            gen = row.extra_metadata["sync_meta"]["content_generation"]
            # price change
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 179.0, "currency": "SAR"}),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "179.0"
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 1
            # image change
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 179.0, "currency": "SAR"},
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.image.updated",
            ))
            session.refresh(row)
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 2
            # stock to zero
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, price={"amount": 179.0, "currency": "SAR"},
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.quantity.low",
            ))
            session.refresh(row)
            assert row.in_stock is False
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 3
            assert is_whatsapp_channel_publish_eligible(row) is True  # out of stock still publishes availability
            # pretend it was published, then Salla hides it
            row.sync_status = "synced"
            row.last_synced_at = datetime.now(timezone.utc)
            session.commit()
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, status="hidden", price={"amount": 179.0, "currency": "SAR"},
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.status.updated",
            ))
            session.refresh(row)
            assert row.extra_metadata["status"] == "hidden"
            assert row.sync_status == "synced"
            assert row.extra_metadata["sync_meta"]["retire_pending"] is True
            assert row.extra_metadata["sync_meta"]["retire_reason"] == "source_hidden"
    finally:
        session.close(); engine.dispose()


def test_late_stale_event_is_corrected_by_the_next_store_sync():
    """A delayed webhook may overwrite with stale data; the hourly full sync
    (same upsert path, fresh Salla read) re-marks the row with the truth."""
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}),
                webhook_event_type="product.created",
            ))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            # late event carrying the old price arrives after the new one
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 120.0, "currency": "SAR"}),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "120.0"
            gen_after_stale = row.extra_metadata["sync_meta"]["content_generation"]
            # periodic store sync applies Salla's current truth through the shared upsert
            from services.store_sync import _normalise_product as norm

            result = svc._apply_normalised_product(
                norm(_raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"})), "salla",
            )
            session.commit()
            session.refresh(row)
            assert result["action"] == "updated"
            assert row.price == "150.0"
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen_after_stale + 1
            assert row.sync_status == "pending"
    finally:
        session.close(); engine.dispose()
