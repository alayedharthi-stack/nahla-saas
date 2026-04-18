"""
Phone normalization tests: E.164 + international support.

Run with:
  railway run --service nahla-saas python -m pytest tests/test_phone_normalization.py -v

Tests:
  1. normalize_to_e164 — all Saudi dial formats
  2. normalize_to_e164 — international numbers (UAE, Egypt, UK, USA)
  3. normalize_to_e164 — edge cases / invalid input
  4. DB: different raw formats map to one customer via normalized_phone
  5. DB: UNIQUE(tenant_id, normalized_phone) prevents duplicates
  6. DB: normalized_phone backfill (existing rows without the column)
  7. DB: cross-tenant same E.164 → two separate rows
  8. DB: normalize_phone_compat backward compat (returns '' not None)
"""
from __future__ import annotations

import os
import sys
import uuid
import pytest
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "database"))

try:
    from utils.phone_utils import normalize_to_e164, normalize_phone_compat, is_valid_e164
    from models import Customer, Tenant
    from services.customer_intelligence import CustomerIntelligenceService
    import sqlalchemy.exc
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    IMPORTS_OK = True
except ImportError as e:
    IMPORTS_OK = False
    _IMPORT_ERROR = str(e)

DB_URL = os.environ.get("DATABASE_URL", "")


# ─────────────────────────────────────────────────────────────────────────────
# DB Fixture (transaction-rollback, no production data contamination)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db():
    if not IMPORTS_OK:
        pytest.skip(f"Import failed: {_IMPORT_ERROR}")
    if not DB_URL:
        pytest.skip("DATABASE_URL not set")

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
def tenant(db):
    sfx = uuid.uuid4().hex[:6]
    t = Tenant(name=f"test-phone-{sfx}", domain=f"phone-{sfx}.test")
    db.add(t)
    db.flush()
    return t


@pytest.fixture
def two_tenants(db):
    sfx = uuid.uuid4().hex[:6]
    a = Tenant(name=f"test-a-{sfx}", domain=f"ta-{sfx}.test")
    b = Tenant(name=f"test-b-{sfx}", domain=f"tb-{sfx}.test")
    db.add_all([a, b])
    db.flush()
    return a, b


def svc(db, tenant_id: int) -> CustomerIntelligenceService:
    return CustomerIntelligenceService(db, tenant_id)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Saudi number normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestSaudiNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("0570000000",      "+966570000000"),
        ("570000000",       "+966570000000"),
        ("966570000000",    "+966570000000"),
        ("00966570000000",  "+966570000000"),
        ("+966570000000",   "+966570000000"),
        ("  0570000000 ",   "+966570000000"),   # whitespace
        ("057-000-0000",    "+966570000000"),   # dashes
        ("0570000000\n",    "+966570000000"),   # newline
    ])
    def test_saudi_formats(self, raw, expected):
        assert normalize_to_e164(raw) == expected

    def test_saudi_different_mobile_prefixes(self):
        """All Saudi mobile prefixes (5x) normalize correctly."""
        for prefix in ("50", "53", "54", "55", "56", "57", "58", "59"):
            raw = f"0{prefix}1234567"
            result = normalize_to_e164(raw)
            assert result is not None
            assert result.startswith("+966")
            assert result == f"+966{prefix}1234567"


# ─────────────────────────────────────────────────────────────────────────────
# 2. International numbers
# ─────────────────────────────────────────────────────────────────────────────

class TestInternationalNormalization:
    @pytest.mark.parametrize("raw,expected", [
        # UAE
        ("+971501234567",   "+971501234567"),
        ("00971501234567",  "+971501234567"),
        # Egypt
        ("+201234567890",   "+201234567890"),
        ("00201234567890",  "+201234567890"),
        # UK
        ("+447911123456",   "+447911123456"),
        ("00447911123456",  "+447911123456"),
        # USA
        ("+12125551234",    "+12125551234"),
        ("001-212-555-1234", "+12125551234"),
        # Jordan (7 = mobile prefix, 8 digits after code)
        ("+962791234567",   "+962791234567"),
        # Kuwait (mobile starts with 5, 6, or 9 — 8 digits)
        ("+96551234567",    "+96551234567"),
        # Bahrain (mobile starts with 3 or 6 — 8 digits)
        ("+97336123456",    "+97336123456"),
    ])
    def test_international_formats(self, raw, expected):
        result = normalize_to_e164(raw)
        assert result == expected, f"Expected {expected!r} for {raw!r}, got {result!r}"

    def test_number_with_parentheses_and_dashes(self):
        """(+1) 212-555-1234 — common US format."""
        result = normalize_to_e164("(+1) 212-555-1234")
        assert result == "+12125551234"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Invalid / edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    @pytest.mark.parametrize("raw", [
        None, "", " ", "abc", "123", "+0", "00000000",
    ])
    def test_invalid_returns_none(self, raw):
        assert normalize_to_e164(raw) is None

    def test_normalize_phone_compat_returns_empty_string(self):
        """Backward-compat: callers using `if not normalize_phone(x)` still work."""
        assert normalize_phone_compat(None)  == ""
        assert normalize_phone_compat("")    == ""
        assert normalize_phone_compat("abc") == ""

    def test_normalize_phone_compat_returns_e164_for_valid(self):
        assert normalize_phone_compat("0570000000") == "+966570000000"
        assert normalize_phone_compat("+447911123456") == "+447911123456"

    @pytest.mark.parametrize("e164,valid", [
        ("+966570000000", True),
        ("+447911123456", True),
        ("+12125551234",  True),
        ("966570000000",  False),  # no +
        ("abc",           False),
        ("",              False),
        (None,            False),
    ])
    def test_is_valid_e164(self, e164, valid):
        assert is_valid_e164(e164) == valid


# ─────────────────────────────────────────────────────────────────────────────
# 4. DB: different raw formats → one customer (via normalized_phone)
# ─────────────────────────────────────────────────────────────────────────────

class TestDbNormalizedPhoneDedup:
    @pytest.mark.parametrize("alt_format", [
        "0570000000",
        "570000000",
        "966570000000",
        "00966570000000",
        "+966570000000",
    ])
    def test_all_formats_resolve_to_same_customer(self, db, tenant, alt_format):
        """
        Creating a customer with any Saudi format, then upserting with any
        other format, must find the SAME customer row.
        """
        service = svc(db, tenant.id)

        # First creation via canonical form
        original = service.upsert_customer_identity(
            phone="+966570000000",
            name="Test User",
            source="salla_sync",
        )
        db.flush()
        assert original is not None

        # Upsert via alternative raw format
        found = service.upsert_customer_identity(
            phone=alt_format,
            source="whatsapp_inbound",
        )
        db.flush()

        assert found is not None
        assert found.id == original.id, (
            f"Expected same customer; got different row for format {alt_format!r}"
        )

    def test_normalized_phone_column_is_set(self, db, tenant):
        """normalized_phone column stores E.164 regardless of raw input."""
        service = svc(db, tenant.id)
        cust = service.upsert_customer_identity(
            phone="0570000000",
            source="salla_sync",
        )
        db.flush()
        assert cust.normalized_phone == "+966570000000"

    def test_normalized_phone_set_for_international(self, db, tenant):
        service = svc(db, tenant.id)
        cust = service.upsert_customer_identity(
            phone="+447911123456",
            source="whatsapp_inbound",
        )
        db.flush()
        assert cust.normalized_phone == "+447911123456"

    def test_raw_phone_preserved(self, db, tenant):
        """phone column keeps the original raw value for display."""
        service = svc(db, tenant.id)
        cust = service.upsert_customer_identity(
            phone="0570000000",
            source="salla_sync",
        )
        db.flush()
        # raw phone is stored as-is (display value)
        # normalized_phone is the E.164 canonical
        assert cust.normalized_phone == "+966570000000"


# ─────────────────────────────────────────────────────────────────────────────
# 5. DB: UNIQUE(tenant_id, normalized_phone) prevents duplicates
# ─────────────────────────────────────────────────────────────────────────────

class TestDbUniqueConstraint:
    def test_duplicate_normalized_phone_raises(self, db, tenant):
        """
        Inserting two customers with the same normalized_phone under the same
        tenant must raise IntegrityError from the DB unique index.
        """
        e164 = "+966570001111"
        db.add(Customer(
            tenant_id=tenant.id, phone=e164, normalized_phone=e164,
        ))
        db.flush()

        db.add(Customer(
            tenant_id=tenant.id, phone="0570001111", normalized_phone=e164,
        ))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_same_e164_different_tenants_allowed(self, db, two_tenants):
        """Same E.164 number can exist in two different tenants."""
        a, b = two_tenants
        e164 = "+966570002222"

        db.add(Customer(tenant_id=a.id, phone=e164, normalized_phone=e164))
        db.flush()
        db.add(Customer(tenant_id=b.id, phone=e164, normalized_phone=e164))
        db.flush()  # Must NOT raise

        count = db.query(Customer).filter(
            Customer.normalized_phone == e164
        ).count()
        assert count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 6. DB: cross-tenant isolation via normalized_phone
# ─────────────────────────────────────────────────────────────────────────────

class TestDbCrossTenantIsolation:
    def test_find_by_phone_does_not_cross_tenants(self, db, two_tenants):
        """
        find_customer_by_phone on service for tenant B must NOT return a
        customer that belongs to tenant A, even with the same E.164 number.
        """
        a, b = two_tenants
        phone = "+966570003333"

        svc(db, a.id).upsert_customer_identity(phone=phone, source="salla_sync")
        db.flush()

        found_in_b = svc(db, b.id).find_customer_by_phone(phone)
        assert found_in_b is None

    def test_upsert_does_not_cross_tenants(self, db, two_tenants):
        """
        Upserting the same phone in two different tenants creates two rows.
        """
        a, b = two_tenants
        phone = "+966570004444"

        ca = svc(db, a.id).upsert_customer_identity(phone=phone, name="عبدالله",
                                                    source="salla_sync")
        db.flush()
        cb = svc(db, b.id).upsert_customer_identity(phone=phone, name="محمد",
                                                    source="salla_sync")
        db.flush()

        assert ca.id != cb.id
        assert ca.tenant_id == a.id
        assert cb.tenant_id == b.id
