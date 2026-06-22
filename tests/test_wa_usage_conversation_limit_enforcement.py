"""Enforcement tests for monthly conversation quota (check_limit)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
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

from models import (  # noqa: E402
    Base,
    BillingPlan,
    BillingSubscription,
    Tenant,
    WhatsAppUsage,
)
from core.wa_usage import (  # noqa: E402
    REASON_CONVERSATION_LIMIT,
    REASON_MARKETING_BLOCKED,
    UNLIMITED_LIMIT_SENTINEL,
    check_limit,
    conversation_quota_category_for_operation,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture
def db():
    session = _make_db()
    try:
        yield session
    finally:
        session.close()


def _seed(db, *, limit: int = 200, used_service: int = 0, used_marketing: int = 0):
    tenant = Tenant(name="Quota Store", subscription_status="active")
    db.add(tenant)
    db.flush()

    plan = BillingPlan(
        tenant_id=None,
        slug="starter",
        name="Starter",
        currency="SAR",
        price_sar=899,
        billing_cycle="monthly",
        limits={"conversations_per_month": limit},
    )
    db.add(plan)
    db.flush()

    sub = BillingSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status="active",
        started_at=datetime.now(timezone.utc),
        auto_renew=True,
        extra_metadata={},
    )
    db.add(sub)
    db.flush()

    now = datetime.now(timezone.utc)
    usage = WhatsAppUsage(
        tenant_id=tenant.id,
        subscription_id=sub.id,
        year=now.year,
        month=now.month,
        service_conversations_used=used_service,
        marketing_conversations_used=used_marketing,
        conversations_limit=limit,
    )
    db.add(usage)
    db.commit()
    return tenant, sub, usage


class TestCheckLimitEnforcement:
    def test_service_allowed_below_limit(self, db):
        tenant, *_ = _seed(db, limit=200, used_service=150)
        result = check_limit(db, tenant.id, category="service")
        assert result.allowed is True
        assert result.reason == "ok"

    def test_service_blocked_at_limit(self, db):
        tenant, *_ = _seed(db, limit=200, used_service=200)
        result = check_limit(db, tenant.id, category="service")
        assert result.allowed is False
        assert result.reason == REASON_CONVERSATION_LIMIT
        assert result.used_total == 200
        assert result.limit == 200

    def test_marketing_blocked_at_limit(self, db):
        tenant, *_ = _seed(db, limit=200, used_service=100, used_marketing=100)
        result = check_limit(db, tenant.id, category="marketing")
        assert result.allowed is False
        assert result.reason == REASON_MARKETING_BLOCKED

    def test_unlimited_plan_never_blocks(self, db):
        tenant = Tenant(name="Scale Store", subscription_status="active")
        db.add(tenant)
        db.flush()
        plan = BillingPlan(
            tenant_id=None,
            slug="scale",
            name="Scale",
            currency="SAR",
            price_sar=2999,
            billing_cycle="monthly",
            limits={"conversations_per_month": -1},
        )
        db.add(plan)
        db.flush()
        sub = BillingSubscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status="active",
            started_at=datetime.now(timezone.utc),
            auto_renew=True,
            extra_metadata={},
        )
        db.add(sub)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add(
            WhatsAppUsage(
                tenant_id=tenant.id,
                subscription_id=sub.id,
                year=now.year,
                month=now.month,
                service_conversations_used=50_000,
                marketing_conversations_used=0,
                conversations_limit=UNLIMITED_LIMIT_SENTINEL,
            )
        )
        db.commit()

        result = check_limit(db, tenant.id, category="service")
        assert result.allowed is True
        assert result.reason == "ok"

    def test_operation_category_mapping(self):
        assert conversation_quota_category_for_operation("send_template") == "marketing"
        assert conversation_quota_category_for_operation("send_message") == "service"
