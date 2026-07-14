"""Unit-level ingest regression for A1-v3.7 identity hooks (no PostgreSQL)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Customer, Integration, Order, Tenant  # noqa: E402
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_WHATSAPP,
)
from services.order_customer_identity_service import (  # noqa: E402
    apply_external_order_identity_from_salla,
    apply_nahla_internal_order_identity,
    apply_whatsapp_order_identity_unlinked,
)
from services.salla_integration_resolver import ResolvedSallaIntegration  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    t = Tenant(id=1, name="T1")
    session.add(t)
    session.commit()
    yield session
    session.close()


def test_external_ingest_does_not_set_customer_id(db) -> None:
    intg = Integration(
        tenant_id=1, provider="salla", external_store_id="S1",
        config={"api_key": "k"}, enabled=True,
    )
    db.add(intg)
    db.flush()
    order = Order(tenant_id=1, external_id="E1", status="pending", total="10", source="salla")
    db.add(order)
    db.flush()
    resolution = ResolvedSallaIntegration(
        integration_id=intg.id, tenant_id=1, matched_via="test",
    )
    apply_external_order_identity_from_salla(
        db,
        order=order,
        tenant_id=1,
        integration_resolution=resolution,
        order_payload={"customer": {"id": "42"}},
        ingest_source="test",
    )
    assert order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER
    assert order.customer_id is None
    assert order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE
    assert order.customer_link_state == LINK_STATE_UNLINKED


def test_nahla_internal_sets_customer_id(db) -> None:
    cust = Customer(tenant_id=1, name="أحمد")
    db.add(cust)
    db.flush()
    order = Order(tenant_id=1, external_id="NHL1", status="pending", total="10", source="whatsapp")
    db.add(order)
    db.flush()
    apply_nahla_internal_order_identity(
        order, db=db, tenant_id=1, customer_id=cust.id,
    )
    assert order.customer_id == cust.id
    assert order.customer_link_state == LINK_STATE_VERIFIED
    assert order.external_customer_profile_id is None


def test_whatsapp_unlinked_clears_both_axes(db) -> None:
    order = Order(tenant_id=1, external_id="WA1", status="pending", total="10", source="whatsapp")
    db.add(order)
    db.flush()
    apply_whatsapp_order_identity_unlinked(order)
    assert order.order_source_kind == ORDER_SOURCE_WHATSAPP
    assert order.customer_id is None
    assert order.external_customer_profile_id is None
    assert order.customer_link_evidence_class is None
    assert order.external_identity_evidence_class is None


def test_identity_service_never_imports_salla_customer_id_lookup() -> None:
    path = _BACKEND / "services" / "order_customer_identity_service.py"
    src = path.read_text(encoding="utf-8")
    assert ".salla_customer_id" not in src
    assert "upsert_customer_from_order" not in src
    assert "pick_active_salla_integration" not in src
