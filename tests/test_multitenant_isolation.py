"""
Multi-tenant isolation tests.

Run with:
  railway run --service nahla-saas python -m pytest tests/test_multitenant_isolation.py -v

Covers:
  1. Same external_store_id cannot create a second tenant (integration constraint)
  2. Legacy config->>'store_id' rows are found and repaired without duplication
  3. Same phone in two tenants → two separate customer rows (no cross-tenant merge)
  4. Same phone in same tenant → recognized as one customer after normalization
  5. acquisition_channel is set correctly on first creation and not overwritten
  6. salla_customer_id column is populated and used for lookup
  7. last_interaction_at is updated only for whatsapp_inbound
  8. Phone normalization correctness for all Saudi dial formats
"""
from __future__ import annotations

import os
import sys
import uuid
import pytest
from datetime import datetime, timezone

import sqlalchemy.exc
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "database"))

try:
    from models import Base, Customer, Integration, Tenant
    from services.customer_intelligence import (
        CustomerIntelligenceService,
        normalize_phone,
    )
    IMPORTS_OK = True
except ImportError as e:
    IMPORTS_OK = False
    _IMPORT_ERROR = str(e)

# ---------------------------------------------------------------------------
# DB fixture — wraps each test in a SAVEPOINT that rolls back after the test,
# leaving production data intact. Requires DATABASE_URL in environment.
# ---------------------------------------------------------------------------

DB_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture(scope="function")
def db():
    """
    PostgreSQL session wrapped in a transaction that is always rolled back.
    Tests must use flush() not commit() — all changes stay invisible to
    production data and other tests.
    """
    if not IMPORTS_OK:
        pytest.skip(f"Import failed: {_IMPORT_ERROR}")
    if not DB_URL:
        pytest.skip("DATABASE_URL not set — skipping DB tests")

    engine  = create_engine(DB_URL)
    conn    = engine.connect()
    trans   = conn.begin()

    Session = sessionmaker(bind=conn)
    session = Session()

    yield session

    session.close()
    try:
        trans.rollback()
    except Exception:
        pass
    conn.close()
    engine.dispose()


@pytest.fixture
def two_tenants(db):
    """Two isolated test tenants with auto-assigned IDs and unique names."""
    suffix = uuid.uuid4().hex[:6]
    a = Tenant(name=f"test-nakhl-{suffix}", domain=f"nakhl-{suffix}.test")
    b = Tenant(name=f"test-honey-{suffix}", domain=f"honey-{suffix}.test")
    db.add_all([a, b])
    db.flush()
    return a, b


def make_svc(db, tenant_id: int) -> CustomerIntelligenceService:
    return CustomerIntelligenceService(db, tenant_id)




def make_svc(db, tenant_id: int) -> CustomerIntelligenceService:
    return CustomerIntelligenceService(db, tenant_id)


# ---------------------------------------------------------------------------
# 1. Integration: same store_id can't be claimed by two tenants
# ---------------------------------------------------------------------------

class TestIntegrationUniqueness:
    def test_same_store_id_raises_on_duplicate(self, db, two_tenants):
        """
        Inserting the same (provider, external_store_id) for a second tenant
        must raise an IntegrityError at the DB level.
        (PostgreSQL UNIQUE constraint; SQLite UNIQUE constraint both enforce this.)
        """
        import sqlalchemy.exc
        a, b = two_tenants
        db.add(Integration(
            provider="salla", external_store_id="STORE_X",
            tenant_id=a.id, enabled=True, config={},
        ))
        db.flush()

        db.add(Integration(
            provider="salla", external_store_id="STORE_X",
            tenant_id=b.id, enabled=True, config={},
        ))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_null_store_id_allows_multiple(self, db, two_tenants):
        """Multiple integrations with NULL external_store_id are fine."""
        a, b = two_tenants
        db.add_all([
            Integration(provider="salla", external_store_id=None,
                        tenant_id=a.id, enabled=False, config={}),
            Integration(provider="salla", external_store_id=None,
                        tenant_id=b.id, enabled=False, config={}),
        ])
        db.flush()  # Should NOT raise
        count = db.query(Integration).filter_by(
            provider="salla", external_store_id=None,
        ).filter(Integration.tenant_id.in_([a.id, b.id])).count()
        assert count == 2


# ---------------------------------------------------------------------------
# 2. Legacy config->>'store_id' rows are found and repaired
# ---------------------------------------------------------------------------

class TestLegacyStoreIdRepair:
    def test_find_by_config_store_id_and_repair(self, db, two_tenants):
        """
        If external_store_id is NULL but config has store_id, the lookup must
        find the correct tenant and set external_store_id for future lookups.
        """
        a, _ = two_tenants
        store_suffix = uuid.uuid4().hex[:6]  # unique per run to avoid constraint clash
        test_store_id = f"TEST_{store_suffix}"
        legacy_integration = Integration(
            provider="salla",
            external_store_id=None,   # ← legacy: not set yet
            tenant_id=a.id,
            enabled=True,
            config={"store_id": test_store_id, "store_name": "النخيل"},
        )
        db.add(legacy_integration)
        db.flush()

        # Simulate the token-login lookup: first check external_store_id column
        found_by_column = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.external_store_id == test_store_id,
        ).first()
        assert found_by_column is None  # Not found yet (legacy row)

        # Then fallback to JSONB config
        found_by_jsonb = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.config["store_id"].as_string() == test_store_id,
        ).first()
        assert found_by_jsonb is not None
        assert found_by_jsonb.tenant_id == a.id

        # Repair
        found_by_jsonb.external_store_id = test_store_id
        db.flush()

        # Now fast path works
        found_fast = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.external_store_id == test_store_id,
        ).first()
        assert found_fast is not None
        assert found_fast.tenant_id == a.id


# ---------------------------------------------------------------------------
# 3. Same phone in two tenants → two separate customer rows
# ---------------------------------------------------------------------------

class TestCrossTenantPhoneIsolation:
    def test_same_phone_two_tenants(self, db, two_tenants):
        """
        Phone +966501234567 at tenant A and tenant B must be two separate rows.
        No cross-tenant merge should occur.
        """
        a, b = two_tenants
        phone = "+966501234567"

        svc_a = make_svc(db, a.id)
        svc_b = make_svc(db, b.id)

        cust_a = svc_a.upsert_customer_identity(phone=phone, name="عبدالله",
                                                source="whatsapp_inbound")
        db.flush()
        cust_b = svc_b.upsert_customer_identity(phone=phone, name="محمد",
                                                source="salla_sync")
        db.flush()

        assert cust_a is not None
        assert cust_b is not None
        assert cust_a.id != cust_b.id            # Different rows
        assert cust_a.tenant_id == a.id
        assert cust_b.tenant_id == b.id
        assert cust_a.name == "عبدالله"
        assert cust_b.name == "محمد"             # Not overwritten

    def test_find_by_phone_is_tenant_scoped(self, db, two_tenants):
        """
        find_customer_by_phone must NOT return a customer from a different tenant.
        """
        a, b = two_tenants
        phone = "+966509999888"

        svc_a = make_svc(db, a.id)
        svc_b = make_svc(db, b.id)

        cust_a = svc_a.upsert_customer_identity(phone=phone, source="salla_sync")
        db.flush()

        found_in_b = svc_b.find_customer_by_phone(phone)
        assert found_in_b is None   # Must NOT find tenant A's customer


# ---------------------------------------------------------------------------
# 4. Same phone, same tenant → one customer after normalization
# ---------------------------------------------------------------------------

class TestSameTenantPhoneNormalization:
    @pytest.mark.parametrize("raw_phone", [
        "0501234567",
        "501234567",
        "+966501234567",
        "00966501234567",
        "966501234567",
    ])
    def test_normalized_forms_resolve_to_same_customer(self, db, two_tenants, raw_phone):
        """All dial formats for 0501234567 must resolve to the same customer row."""
        a, _ = two_tenants
        svc = make_svc(db, a.id)

        # Create with canonical form first
        canonical = svc.upsert_customer_identity(
            phone="0501234567", name="سعد", source="salla_sync",
        )
        db.flush()

        # Now upsert with an alternative format
        found = svc.upsert_customer_identity(phone=raw_phone, source="whatsapp_inbound")
        db.flush()

        assert found is not None
        assert found.id == canonical.id   # Same row, not a duplicate


# ---------------------------------------------------------------------------
# 5. acquisition_channel is set on first creation
# ---------------------------------------------------------------------------

class TestAcquisitionChannel:
    def test_whatsapp_inbound_channel(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966501111111",
            source="whatsapp_inbound",
        )
        db.flush()
        assert cust.acquisition_channel == "whatsapp_inbound"

    def test_salla_sync_channel(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966502222222",
            source="salla_sync",
        )
        db.flush()
        assert cust.acquisition_channel == "salla_sync"

    def test_channel_not_overwritten_on_update(self, db, two_tenants):
        """Once set, acquisition_channel must NOT change when the customer is updated."""
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966503333333",
            source="salla_sync",       # ← first creation: salla_sync
        )
        db.flush()
        assert cust.acquisition_channel == "salla_sync"

        # Now same customer contacts via WhatsApp
        cust2 = svc.upsert_customer_identity(
            phone="+966503333333",
            source="whatsapp_inbound",  # ← update event
        )
        db.flush()
        assert cust2.id == cust.id                           # Same customer
        assert cust2.acquisition_channel == "salla_sync"     # Channel unchanged


# ---------------------------------------------------------------------------
# 6. salla_customer_id lookup and repair
# ---------------------------------------------------------------------------

class TestSallaCustomerId:
    def test_create_with_salla_id(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966504444444",
            external_id="SALLA_CUST_001",
            source="salla_sync",
        )
        db.flush()
        assert cust.salla_customer_id == "SALLA_CUST_001"

    def test_find_by_salla_id_returns_correct_tenant(self, db, two_tenants):
        a, b = two_tenants
        svc_a = make_svc(db, a.id)
        svc_b = make_svc(db, b.id)

        cust_a = svc_a.upsert_customer_identity(
            external_id="SALLA_CUST_999", source="salla_sync",
        )
        db.flush()

        # Tenant B's service must NOT find tenant A's salla customer
        found_in_b = svc_b._find_customer_by_external_id("SALLA_CUST_999")
        assert found_in_b is None

    def test_salla_id_not_duplicated_within_tenant(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)

        cust1 = svc.upsert_customer_identity(
            phone="+966505555555",
            external_id="SALLA_CUST_DUP",
            source="salla_sync",
        )
        db.flush()

        # Second call with same external_id → must return same customer, no new row
        cust2 = svc.upsert_customer_identity(
            external_id="SALLA_CUST_DUP",
            source="salla_sync",
        )
        db.flush()

        assert cust1.id == cust2.id
        assert db.query(Customer).filter_by(
            tenant_id=a.id, salla_customer_id="SALLA_CUST_DUP"
        ).count() == 1


# ---------------------------------------------------------------------------
# 7. last_interaction_at updated only for WhatsApp
# ---------------------------------------------------------------------------

class TestLastInteractionAt:
    def test_set_on_whatsapp_inbound(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966506666666",
            source="whatsapp_inbound",
            seen_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc),
        )
        db.flush()
        assert cust.last_interaction_at is not None

    def test_not_set_for_salla_sync(self, db, two_tenants):
        a, _ = two_tenants
        svc  = make_svc(db, a.id)
        cust = svc.upsert_customer_identity(
            phone="+966507777777",
            source="salla_sync",
        )
        db.flush()
        assert cust.last_interaction_at is None


# ---------------------------------------------------------------------------
# 8. phone normalization correctness
# ---------------------------------------------------------------------------

class TestPhoneNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("0501234567",       "+966501234567"),
        ("501234567",        "+966501234567"),
        ("966501234567",     "+966501234567"),
        ("00966501234567",   "+966501234567"),
        ("+966501234567",    "+966501234567"),
        ("  0501234567 ",    "+966501234567"),
        # normalize_phone returns "" (falsy) for empty / invalid inputs
        ("",                 ""),
        (None,               ""),
    ])
    def test_normalize_phone(self, raw, expected):
        result = normalize_phone(raw)
        assert result == expected

    @pytest.mark.parametrize("raw", ["", None, "123", "abc"])
    def test_invalid_phone_is_falsy(self, raw):
        """Any invalid phone must produce a falsy result."""
        assert not normalize_phone(raw)
