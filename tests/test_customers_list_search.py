"""tests/test_customers_list_search.py
────────────────────────────────────
Contract tests for ``GET /customers`` phone/name search discovery.

Pins the May 2026 fix: ``normalized_phone`` and E.164-aware input
normalization so ``+966…``, ``966…``, and ``05…`` find the same row.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Customer, CustomerProfile, Tenant  # noqa: E402

TARGET_E164 = "+966506569015"
TARGET_RAW = "966506569015"
TARGET_LOCAL = "0506569015"
TARGET_NAME = "عايد حسين الحارثي"
OTHER_PHONE = "+966500000099"


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    return sessionmaker(bind=engine), engine


def _call_list(engine, tenant_id: int, **query_kwargs):
    from routers import customers as customers_router

    Session = sessionmaker(bind=engine)
    db = Session()
    original_resolve = customers_router.resolve_tenant_id
    customers_router.resolve_tenant_id = lambda request, db=None: tenant_id  # type: ignore

    class _FakeReq:
        headers: dict = {}
        cookies: dict = {}
        state = type("S", (), {})()

    defaults = dict(
        search="",
        segment="",
        manual_segment="",
        marketing_opt_out=None,
        test_recipient=None,
        page=1,
        per_page=50,
    )
    defaults.update(query_kwargs)

    try:
        return asyncio.run(
            customers_router.list_customers(request=_FakeReq(), db=db, **defaults)
        )
    finally:
        customers_router.resolve_tenant_id = original_resolve
        db.close()


def _seed_target_customer(db, tenant_id: int) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        name=TARGET_NAME,
        phone=TARGET_RAW,
        normalized_phone=TARGET_E164,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_other_customer(db, tenant_id: int) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        name="عميل آخر",
        phone="966500000099",
        normalized_phone=OTHER_PHONE,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture
def tenant_db():
    Session, engine = _make_db()
    db = Session()
    t = Tenant(name="SearchTenant", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    target = _seed_target_customer(db, t.id)
    other = _seed_other_customer(db, t.id)
    tenant_id = t.id
    target_id = target.id
    other_id = other.id
    db.close()
    yield engine, tenant_id, target_id, other_id
    engine.dispose()


class TestCustomersListPhoneSearch:
    def test_search_plus_e164_finds_normalized_row(self, tenant_db):
        engine, tenant_id, target_id, _ = tenant_db
        result = _call_list(engine, tenant_id, search=TARGET_E164)
        assert result["total"] == 1
        assert result["customers"][0]["id"] == target_id

    def test_search_without_plus_still_finds(self, tenant_db):
        engine, tenant_id, target_id, _ = tenant_db
        result = _call_list(engine, tenant_id, search=TARGET_RAW)
        assert result["total"] == 1
        assert result["customers"][0]["id"] == target_id

    def test_search_local_05_form_finds_same_customer(self, tenant_db):
        engine, tenant_id, target_id, _ = tenant_db
        result = _call_list(engine, tenant_id, search=TARGET_LOCAL)
        assert result["total"] == 1
        assert result["customers"][0]["id"] == target_id

    def test_name_search_still_works(self, tenant_db):
        engine, tenant_id, target_id, _ = tenant_db
        result = _call_list(engine, tenant_id, search="عايد حسين")
        assert result["total"] == 1
        assert result["customers"][0]["id"] == target_id

    def test_unrelated_phone_does_not_match_target(self, tenant_db):
        engine, tenant_id, target_id, other_id = tenant_db
        result = _call_list(engine, tenant_id, search=OTHER_PHONE)
        assert result["total"] == 1
        assert result["customers"][0]["id"] == other_id
        assert result["customers"][0]["id"] != target_id


class TestCustomersListSearchWithSegmentFilter:
    def test_segment_filter_still_applies_with_phone_search(self, tenant_db):
        engine, tenant_id, target_id, _ = tenant_db
        Session = sessionmaker(bind=engine)
        db = Session()
        db.add(
            CustomerProfile(
                tenant_id=tenant_id,
                customer_id=target_id,
                segment="lead",
                customer_status="lead",
                rfm_segment="lead",
            )
        )
        db.commit()
        db.close()

        # Target is lead-only — VIP segment must exclude them even when
        # the phone search would otherwise match.
        result = _call_list(
            engine, tenant_id, search=TARGET_RAW, segment="vip",
        )
        assert result["total"] == 0
        assert result["customers"] == []
