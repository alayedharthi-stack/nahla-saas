"""Salla customer sync / webhook → durable address candidates.

Identity binding for address attachment is the provider customer id under
an active store connection — never a name match, never a phone fallback.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from models import (  # noqa: E402
    Base,
    Customer,
    CustomerAddress,
    CustomerAddressProvenance,
    Integration,
    Tenant,
)
from services.store_sync import StoreSyncService  # noqa: E402

SALLA_ID = "SC-200"
CITY = "الرياض"
STREET = "حي النرجس، شارع 10"


@pytest.fixture()
def db():
    """SQLite session over the real ORM metadata.

    The JSONB→JSON swap is reverted right after ``create_all``: leaving it
    in place would strip ``.astext`` from ``Customer.metadata`` and break
    the production query paths under test.
    """
    engine = create_engine("sqlite:///:memory:")
    swapped: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                swapped.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, original in swapped:
        col.type = original
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=1, name="متجر تجريبي عام"))
    session.commit()
    yield session
    session.close()


def _customer_payload(**overrides) -> dict:
    payload = {
        "id": SALLA_ID,
        "first_name": "نورة",
        "last_name": "عبدالله",
        "email": "noura@example.com",
        "mobile": "966500000200",
        "city": CITY,
        "country": "SA",
        "location": STREET,
        "updated_at": "2026-09-01T10:00:00Z",
    }
    payload.update(overrides)
    return payload


class _FakeAdapter:
    platform = "salla"

    def __init__(self, payloads):
        self._payloads = payloads

    async def get_customers(self, *, updated_since=None):
        return list(self._payloads)


def _seed_integration(db) -> Integration:
    intg = Integration(
        tenant_id=1, provider="salla", external_store_id="STORE-A1",
        config={"api_key": "k", "store_id": "STORE-A1"}, enabled=True,
    )
    db.add(intg)
    db.flush()
    return intg


def _service(db, intg, payloads) -> StoreSyncService:
    return StoreSyncService(
        db, tenant_id=1, integration_connection_id=intg.id,
        adapter=_FakeAdapter(payloads),
    )


def test_customer_sync_persists_the_profile_address_as_a_candidate(db):
    intg = _seed_integration(db)
    asyncio.run(_service(db, intg, [_customer_payload()]).sync_customers())
    db.commit()

    customer = db.query(Customer).filter_by(salla_customer_id=SALLA_ID).one()
    row = db.query(CustomerAddress).filter_by(customer_id=customer.id).one()
    assert row.city == CITY
    assert row.address_text == STREET
    # A free-text profile location is never a national short address.
    assert row.saudi_national_address is None

    prov = db.query(CustomerAddressProvenance).one()
    assert prov.source == "salla_customer_profile"
    assert prov.source_ref == SALLA_ID
    assert prov.integration_connection_id == intg.id
    assert prov.source_country == "SA"
    assert prov.selection_state == "candidate"
    assert prov.selected_at is None


def test_customer_sync_address_candidate_is_idempotent(db):
    intg = _seed_integration(db)
    for _ in range(3):
        asyncio.run(_service(db, intg, [_customer_payload()]).sync_customers())
        db.commit()
    assert db.query(CustomerAddress).count() == 1
    assert db.query(CustomerAddressProvenance).count() == 1


def test_customer_sync_without_address_fields_writes_no_address(db):
    intg = _seed_integration(db)
    payload = _customer_payload(city=None, country=None, location="null")
    asyncio.run(_service(db, intg, [payload]).sync_customers())
    db.commit()
    assert db.query(Customer).filter_by(salla_customer_id=SALLA_ID).count() == 1
    assert db.query(CustomerAddress).count() == 0


def test_customer_webhook_projects_the_top_level_address(db):
    intg = _seed_integration(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    asyncio.run(service.handle_customer_webhook(_customer_payload()))
    db.commit()

    customer = db.query(Customer).filter_by(salla_customer_id=SALLA_ID).one()
    row = db.query(CustomerAddress).filter_by(customer_id=customer.id).one()
    assert row.city == CITY
    assert row.address_text == STREET
    prov = db.query(CustomerAddressProvenance).one()
    assert prov.selection_state == "candidate"


def test_customer_webhook_repeat_is_idempotent(db):
    intg = _seed_integration(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    for _ in range(2):
        asyncio.run(service.handle_customer_webhook(_customer_payload()))
        db.commit()
    assert db.query(CustomerAddress).count() == 1
    assert db.query(CustomerAddressProvenance).count() == 1


# ── Identity binding failures ───────────────────────────────────────────

def test_unlinked_salla_customer_gets_no_address(db):
    intg = _seed_integration(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "customer_not_linked"
    assert db.query(CustomerAddress).count() == 0


def test_name_match_alone_never_authorises_attachment(db):
    """A same-named customer with no provider id is not this customer."""
    intg = _seed_integration(db)
    db.add(Customer(tenant_id=1, name="نورة عبدالله", phone="966500000200",
                    normalized_phone="+966500000200"))
    db.commit()
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "customer_not_linked"
    assert db.query(CustomerAddress).count() == 0


def test_duplicate_provider_identity_cannot_exist(db):
    """The schema forbids two local customers sharing one provider id."""
    intg = _seed_integration(db)
    db.add(Customer(tenant_id=1, phone="966500000200",
                    normalized_phone="+966500000200", salla_customer_id=SALLA_ID))
    db.commit()
    db.add(Customer(tenant_id=1, phone="966500000201",
                    normalized_phone="+966500000201", salla_customer_id=SALLA_ID))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert intg.id is not None


def test_ambiguous_provider_identity_fails_safe(db):
    """If the uniqueness guarantee were ever lost, attachment refuses."""
    intg = _seed_integration(db)
    db.add(Customer(tenant_id=1, phone="966500000200",
                    normalized_phone="+966500000200", salla_customer_id=SALLA_ID))
    db.commit()
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    duplicates = db.query(Customer).all() * 2

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def all(self):
            return duplicates

    real_query = db.query

    def _query(*entities, **kwargs):
        if entities and entities[0] is Customer:
            return _Query()
        return real_query(*entities, **kwargs)

    with patch.object(db, "query", side_effect=_query):
        customer, reason = service._resolve_customer_for_address_attachment(SALLA_ID)
    assert customer is None
    assert reason == "ambiguous_customer_identity"
    assert db.query(CustomerAddress).count() == 0


def test_conflicting_provider_id_never_attaches_to_another_customer(db):
    intg = _seed_integration(db)
    db.add(Customer(tenant_id=1, phone="966500000200",
                    normalized_phone="+966500000200", salla_customer_id="SC-OTHER"))
    db.commit()
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "customer_not_linked"
    assert db.query(CustomerAddress).count() == 0


def test_payload_without_provider_id_is_rejected(db):
    intg = _seed_integration(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(
        _customer_payload(id="")
    )
    assert reason == "missing_salla_customer_id"
    assert db.query(CustomerAddress).count() == 0


def test_another_tenants_customer_is_never_attached(db):
    intg = _seed_integration(db)
    db.add(Tenant(id=2, name="متجر آخر"))
    db.flush()
    db.add(Customer(tenant_id=2, phone="966500000200",
                    normalized_phone="+966500000200", salla_customer_id=SALLA_ID))
    db.commit()
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "customer_not_linked"
    assert db.query(CustomerAddress).count() == 0


def _linked_customer(db):
    db.add(Customer(tenant_id=1, phone="966500000200",
                    normalized_phone="+966500000200", salla_customer_id=SALLA_ID))
    db.commit()


def test_connection_from_another_tenant_refuses_the_write(db):
    """A verified Salla connection is a PREREQUISITE, not optional provenance.

    Importing anyway and merely leaving ``integration_connection_id`` NULL
    would let a payload borrow authority its store binding never granted.
    """
    _seed_integration(db)
    db.add(Tenant(id=2, name="متجر آخر"))
    db.flush()
    foreign = Integration(tenant_id=2, provider="salla", external_store_id="STORE-B",
                          config={}, enabled=True)
    db.add(foreign)
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=foreign.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "connection_not_found_for_tenant"
    assert db.query(CustomerAddress).count() == 0
    assert db.query(CustomerAddressProvenance).count() == 0


def test_absent_connection_refuses_the_write(db):
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=None)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "no_active_salla_connection"
    assert db.query(CustomerAddress).count() == 0


def test_disabled_connection_refuses_the_write(db):
    intg = Integration(tenant_id=1, provider="salla", external_store_id="STORE-D",
                       config={}, enabled=False)
    db.add(intg)
    db.flush()
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "connection_disabled"
    assert db.query(CustomerAddress).count() == 0


def test_non_salla_connection_refuses_the_write(db):
    intg = Integration(tenant_id=1, provider="shopify", external_store_id="STORE-S",
                       config={}, enabled=True)
    db.add(intg)
    db.flush()
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "connection_provider_mismatch"
    assert db.query(CustomerAddress).count() == 0


def test_verified_connection_is_recorded_on_the_write(db):
    intg = _seed_integration(db)
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    reason = service._persist_salla_profile_address_candidate(_customer_payload())
    db.commit()
    assert reason == "created"
    prov = db.query(CustomerAddressProvenance).one()
    assert prov.integration_connection_id == intg.id


def test_same_provider_identity_cannot_borrow_another_store_context(db):
    """One Salla customer id under a store this tenant does not own."""
    _seed_integration(db)
    db.add(Tenant(id=2, name="متجر آخر"))
    db.flush()
    other_store = Integration(tenant_id=2, provider="salla", external_store_id="STORE-Z",
                              config={}, enabled=True)
    db.add(other_store)
    _linked_customer(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=other_store.id)
    assert service._persist_salla_profile_address_candidate(
        _customer_payload()
    ) == "connection_not_found_for_tenant"
    db.commit()
    assert db.query(CustomerAddress).count() == 0


def test_out_of_order_webhook_events_keep_the_newer_address(db):
    intg = _seed_integration(db)
    service = StoreSyncService(db, tenant_id=1, integration_connection_id=intg.id)
    asyncio.run(service.handle_customer_webhook(
        _customer_payload(location="عنوان جديد", updated_at="2026-09-09T10:00:00Z")
    ))
    db.commit()
    asyncio.run(service.handle_customer_webhook(
        _customer_payload(location="عنوان قديم", updated_at="2026-09-01T10:00:00Z")
    ))
    db.commit()
    rows = db.query(CustomerAddress).all()
    assert len(rows) == 1
    assert rows[0].address_text == "عنوان جديد"


# ── R3: the store that owns the customer owns its address ───────────────

def _second_store(db, *, external_store_id="STORE-B1", enabled=True):
    intg = Integration(
        tenant_id=1, provider="salla", external_store_id=external_store_id,
        config={"api_key": "k", "store_id": external_store_id}, enabled=enabled,
    )
    db.add(intg)
    db.flush()
    return intg


def test_a_second_store_cannot_rewrite_the_first_stores_address(db):
    """Two enabled connections, one tenant, the same provider reference.

    An enabled connection existing is not the same as that connection
    owning this customer. Without ownership, store B's import silently
    became store A's address — one row, B's content, B's connection id.
    """
    store_a = _seed_integration(db)
    store_b = _second_store(db)
    _linked_customer(db)

    first = StoreSyncService(db, tenant_id=1, integration_connection_id=store_a.id)
    assert first._persist_salla_profile_address_candidate(
        _customer_payload(location="حي النرجس، شارع 10")
    ) == "created"
    db.commit()

    second = StoreSyncService(db, tenant_id=1, integration_connection_id=store_b.id)
    assert second._persist_salla_profile_address_candidate(
        _customer_payload(city="جدة", location="حي الشاطئ، شارع 7",
                          updated_at="2026-09-09T10:00:00Z")
    ) == "source_owned_by_another_connection"
    db.commit()

    rows = db.query(CustomerAddress).all()
    assert len(rows) == 1
    assert rows[0].address_text == "حي النرجس، شارع 10"
    prov = db.query(CustomerAddressProvenance).one()
    assert prov.integration_connection_id == store_a.id


def test_the_owning_store_keeps_refreshing_its_own_address(db):
    """The positive side: ownership must not freeze the real owner out."""
    store_a = _seed_integration(db)
    _second_store(db)
    _linked_customer(db)

    service = StoreSyncService(db, tenant_id=1, integration_connection_id=store_a.id)
    assert service._persist_salla_profile_address_candidate(
        _customer_payload(location="حي النرجس، شارع 10")
    ) == "created"
    db.commit()
    assert service._persist_salla_profile_address_candidate(
        _customer_payload(location="حي النرجس، شارع 12",
                          updated_at="2026-09-09T10:00:00Z")
    ) == "updated"
    db.commit()

    rows = db.query(CustomerAddress).all()
    assert len(rows) == 1
    assert rows[0].address_text == "حي النرجس، شارع 12"


def test_ownership_transfers_when_the_previous_store_is_disconnected(db):
    """Replacement and reconnection still work — explicitly, not silently."""
    store_a = _seed_integration(db)
    store_b = _second_store(db)
    _linked_customer(db)

    first = StoreSyncService(db, tenant_id=1, integration_connection_id=store_a.id)
    assert first._persist_salla_profile_address_candidate(
        _customer_payload(location="حي النرجس، شارع 10")
    ) == "created"
    db.commit()

    store_a.enabled = False
    db.add(store_a)
    db.commit()

    second = StoreSyncService(db, tenant_id=1, integration_connection_id=store_b.id)
    assert second._persist_salla_profile_address_candidate(
        _customer_payload(city="جدة", location="حي الشاطئ، شارع 7",
                          updated_at="2026-09-09T10:00:00Z")
    ) == "created"
    db.commit()

    # The previous store's approved content is preserved, not overwritten.
    texts = {r.address_text for r in db.query(CustomerAddress).all()}
    assert texts == {"حي النرجس، شارع 10", "حي الشاطئ، شارع 7"}
    owners = {
        p.integration_connection_id
        for p in db.query(CustomerAddressProvenance).all()
    }
    assert owners == {store_a.id, store_b.id}


def test_a_customer_mapped_to_another_store_is_never_attached(db):
    """The mapping, not the reference, decides whose customer this is."""
    from models import ExternalCustomerProfile  # noqa: PLC0415

    store_a = _seed_integration(db)
    store_b = _second_store(db)
    _linked_customer(db)
    db.add(ExternalCustomerProfile(
        tenant_id=1, identity_namespace="salla",
        integration_connection_id=store_a.id,
        external_customer_ref=SALLA_ID,
    ))
    db.commit()

    service = StoreSyncService(db, tenant_id=1, integration_connection_id=store_b.id)
    assert service._persist_salla_profile_address_candidate(
        _customer_payload()
    ) == "customer_owned_by_another_connection"
    db.commit()
    assert db.query(CustomerAddress).count() == 0


def test_the_mapped_store_still_attaches_normally(db):
    from models import ExternalCustomerProfile  # noqa: PLC0415

    store_a = _seed_integration(db)
    _linked_customer(db)
    db.add(ExternalCustomerProfile(
        tenant_id=1, identity_namespace="salla",
        integration_connection_id=store_a.id,
        external_customer_ref=SALLA_ID,
    ))
    db.commit()

    service = StoreSyncService(db, tenant_id=1, integration_connection_id=store_a.id)
    assert service._persist_salla_profile_address_candidate(
        _customer_payload()
    ) == "created"
    db.commit()
    assert db.query(CustomerAddress).count() == 1
