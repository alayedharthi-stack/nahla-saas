"""The merchant sync status never reveals other tenants' trial scope.

``GET /whatsapp-sync/status`` is served to any merchant. With a limited write
scope configured (``NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS`` /
``_PRODUCT_IDS``) it must tell a store only whether a scope is active,
whether that store is in it and, when products are limited, that store's own
allowed product ids. Other tenants' ids and their products never appear.

Generic merchant data (متجر تجريبي عام, متجر تجريبي ثانٍ).
"""
from __future__ import annotations

import json

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import Base, Tenant, WhatsAppConnection
from services.whatsapp_catalog_sync import build_whatsapp_catalog_sync_status
from services.whatsapp_catalog_sync_scope import (
    PRODUCT_SCOPE_ENV,
    TENANT_SCOPE_ENV,
    scope_description,
    tenant_scope_status,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def test_tenant_scope_status_lists_only_the_callers_own_products(monkeypatch):
    monkeypatch.setenv(TENANT_SCOPE_ENV, "41,52")
    monkeypatch.setenv(PRODUCT_SCOPE_ENV, "41:7001,41:7002,52:9900")
    assert tenant_scope_status(41) == {"active": True, "tenant_in_scope": True, "products_limited": True,
                                       "product_ids": [7001, 7002]}
    assert tenant_scope_status(52) == {"active": True, "tenant_in_scope": True, "products_limited": True,
                                       "product_ids": [9900]}
    outsider = tenant_scope_status(63)
    assert outsider == {"active": True, "tenant_in_scope": False, "products_limited": True, "product_ids": []}
    for tid, view in ((41, tenant_scope_status(41)), (52, tenant_scope_status(52)), (63, outsider)):
        text = json.dumps(view)
        for other in {"41", "52", "7001", "7002", "9900"} - {str(tid)} - {str(p) for p in view["product_ids"]}:
            assert other not in text, (tid, other)
    # the operator description still carries the full scope
    assert scope_description()["tenant_ids"] == [41, 52]


def test_tenant_scope_status_without_a_scope(monkeypatch):
    monkeypatch.delenv(TENANT_SCOPE_ENV, raising=False)
    monkeypatch.delenv(PRODUCT_SCOPE_ENV, raising=False)
    assert tenant_scope_status(41) == {"active": False, "tenant_in_scope": True, "products_limited": False,
                                       "product_ids": []}
    monkeypatch.setenv(TENANT_SCOPE_ENV, "41")
    assert tenant_scope_status(41) == {"active": True, "tenant_in_scope": True, "products_limited": False,
                                       "product_ids": []}


def test_merchant_status_payload_carries_no_other_tenant_ids(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        a = Tenant(name="متجر تجريبي عام", is_active=True)
        b = Tenant(name="متجر تجريبي ثانٍ", is_active=True)
        session.add_all([a, b])
        session.commit()
        for tenant, catalog in ((a, "CAT-GENERIC-001"), (b, "CAT-GENERIC-002")):
            session.add(WhatsAppConnection(
                tenant_id=tenant.id, whatsapp_business_account_id=f"WABA-{tenant.id}",
                phone_number_id=f"PN-{tenant.id}", access_token="EAAB-test", meta_catalog_id=catalog,
                catalog_enabled=True, extra_metadata={},
            ))
        session.commit()
        secret_product = 987654321
        monkeypatch.setenv(TENANT_SCOPE_ENV, str(b.id))
        monkeypatch.setenv(PRODUCT_SCOPE_ENV, f"{b.id}:{secret_product}")
        status_a = build_whatsapp_catalog_sync_status(session, a.id)
        assert status_a["sync_scope"] == {"active": True, "tenant_in_scope": False, "products_limited": True,
                                          "product_ids": []}
        assert str(secret_product) not in json.dumps(status_a, default=str)
        status_b = build_whatsapp_catalog_sync_status(session, b.id)
        assert status_b["sync_scope"]["product_ids"] == [secret_product]
        assert status_b["sync_scope"]["tenant_in_scope"] is True
    finally:
        session.close(); engine.dispose()
