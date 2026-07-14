"""Regression: legacy Customer sync preserved alongside A1 profile side effect."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import (  # noqa: E402
    Base,
    Customer,
    ExternalCustomerProfile,
    Integration,
    Order,
    Tenant,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_UNLINKED,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
)
from services.order_customer_identity_service import (  # noqa: E402
    apply_external_order_identity_from_salla,
)
from services.salla_integration_resolver import ResolvedSallaIntegration  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402


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
    session.add(Tenant(id=1, name="متجر تجريبي عام"))
    session.commit()
    yield session
    session.close()


class _FakeSallaCustomerAdapter:
    platform = "salla"

    async def get_customers(self, *, updated_since=None):
        return [
            {
                "id": "SC-100",
                "first_name": "أحمد",
                "last_name": "سالم",
                "email": "ahmad@example.com",
                "mobile": "966500000001",
                "city": "الرياض",
                "country": "SA",
            }
        ]


def _seed_integration(db) -> Integration:
    intg = Integration(
        tenant_id=1,
        provider="salla",
        external_store_id="STORE-L1",
        config={"api_key": "k", "store_id": "STORE-L1"},
        enabled=True,
    )
    db.add(intg)
    db.flush()
    return intg


def test_sync_customers_with_integration_still_creates_legacy_customer(db) -> None:
    """1. Salla customer sync still creates/updates legacy Customer."""
    intg = _seed_integration(db)
    service = StoreSyncService(
        db,
        tenant_id=1,
        integration_connection_id=intg.id,
        adapter=_FakeSallaCustomerAdapter(),
    )

    synced = asyncio.run(service.sync_customers())

    assert synced == 1
    legacy = (
        db.query(Customer)
        .filter(Customer.tenant_id == 1, Customer.salla_customer_id == "SC-100")
        .one()
    )
    assert legacy.email == "ahmad@example.com"
    assert legacy.extra_metadata["salla_id"] == "SC-100"


def test_sync_customers_with_integration_also_upserts_a1_profile(db) -> None:
    """2. A1 ExternalCustomerProfile upsert runs in parallel."""
    intg = _seed_integration(db)
    service = StoreSyncService(
        db,
        tenant_id=1,
        integration_connection_id=intg.id,
        adapter=_FakeSallaCustomerAdapter(),
    )

    asyncio.run(service.sync_customers())

    profile = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.tenant_id == 1,
            ExternalCustomerProfile.integration_connection_id == intg.id,
            ExternalCustomerProfile.external_customer_ref == "SC-100",
        )
        .one()
    )
    assert profile.profile_state == "active"
    assert profile.demographics.get("name") == "أحمد سالم"


def test_external_order_resolution_does_not_read_salla_customer_id(db) -> None:
    """3. A1 external order resolution never reads Customer.salla_customer_id."""
    path = _BACKEND / "services" / "order_customer_identity_service.py"
    src = path.read_text(encoding="utf-8")
    assert ".salla_customer_id" not in src

    intg = _seed_integration(db)
    legacy = Customer(tenant_id=1, name="عميل قديم", salla_customer_id="SC-100")
    db.add(legacy)
    db.flush()
    order = Order(
        tenant_id=1,
        external_id="ORD-L3",
        status="pending",
        total="100",
        source="salla",
    )
    db.add(order)
    db.flush()

    original_query = db.query

    def _spy_query(*entities, **kwargs):
        if entities and entities[0] is Customer:
            raise AssertionError("A1 external order path must not query Customer")
        return original_query(*entities, **kwargs)

    with patch.object(db, "query", side_effect=_spy_query):
        apply_external_order_identity_from_salla(
            db,
            order=order,
            tenant_id=1,
            integration_resolution=ResolvedSallaIntegration(
                integration_id=intg.id,
                tenant_id=1,
                matched_via="test",
            ),
            order_payload={"customer": {"id": "SC-100"}},
            ingest_source="test",
        )

    assert order.customer_id is None
    assert order.order_source_kind == ORDER_SOURCE_EXTERNAL_PROVIDER
    assert order.external_identity_evidence_class == EVIDENCE_AUTHORITATIVE


def test_a1_profile_failure_does_not_block_legacy_customer_sync(db) -> None:
    """4. A1 profile path failure must not block legacy Customer sync."""
    intg = _seed_integration(db)
    service = StoreSyncService(
        db,
        tenant_id=1,
        integration_connection_id=intg.id,
        adapter=_FakeSallaCustomerAdapter(),
    )

    with patch(
        "services.external_customer_profile_service.upsert_profile_from_salla_customer_sync",
        side_effect=RuntimeError("a1_profile_down"),
    ):
        synced = asyncio.run(service.sync_customers())

    assert synced == 1
    legacy = (
        db.query(Customer)
        .filter(Customer.tenant_id == 1, Customer.salla_customer_id == "SC-100")
        .one()
    )
    assert legacy.name == "أحمد سالم"
    assert (
        db.query(ExternalCustomerProfile)
        .filter(ExternalCustomerProfile.tenant_id == 1)
        .count()
        == 0
    )


def test_legacy_customer_sync_failure_does_not_make_a1_claim_canonical_link(db) -> None:
    """5. Legacy Customer sync failure must not let A1 claim canonical customer link."""
    intg = _seed_integration(db)
    service = StoreSyncService(
        db,
        tenant_id=1,
        integration_connection_id=intg.id,
        adapter=_FakeSallaCustomerAdapter(),
    )

    with patch.object(
        service,
        "_upsert_legacy_customer_from_salla_sync_payload",
        side_effect=RuntimeError("legacy_down"),
    ):
        synced = asyncio.run(service.sync_customers())

    assert synced == 0
    assert db.query(Customer).filter(Customer.tenant_id == 1).count() == 0

    profile = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.tenant_id == 1,
            ExternalCustomerProfile.external_customer_ref == "SC-100",
        )
        .one()
    )
    assert profile is not None

    order = Order(
        tenant_id=1,
        external_id="ORD-L5",
        status="pending",
        total="50",
        source="salla",
    )
    db.add(order)
    db.flush()
    apply_external_order_identity_from_salla(
        db,
        order=order,
        tenant_id=1,
        integration_resolution=ResolvedSallaIntegration(
            integration_id=intg.id,
            tenant_id=1,
            matched_via="test",
        ),
        order_payload={"customer": {"id": "SC-100"}},
        ingest_source="test",
    )
    assert order.customer_id is None
    assert order.customer_link_state == LINK_STATE_UNLINKED
    assert order.external_customer_profile_id == profile.id
