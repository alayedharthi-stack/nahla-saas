"""Subscription-period WhatsApp usage scoping.

When a merchant renews and receives a new BillingSubscription row, usage
must reset for the new period while lifetime totals remain available.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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
    ConversationLog,
    MessageEvent,
    Tenant,
    TenantSettings,
    WhatsAppUsage,
)
from core.wa_usage import (  # noqa: E402
    _get_or_create_usage,
    _usage_period_context,
    get_current_period_usage,
    get_daily_activity_metrics,
    get_lifetime_conversations,
    get_local_day_bounds_utc_naive,
    get_today_billable_conversations_count,
    get_today_conversations_count,
    get_today_in_period_billable_count,
    get_today_messages_count,
    get_usage_audit_snapshot,
    get_usage_this_month,
    track_conversation,
)


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
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture
def db():
    session = _make_db()
    try:
        yield session
    finally:
        session.close()


def _seed_tenant(db, *, slug: str = "growth") -> tuple[Tenant, BillingPlan, BillingSubscription]:
    tenant = Tenant(name="Test Store", subscription_status="active")
    db.add(tenant)
    db.flush()

    plan = BillingPlan(
        tenant_id=None,
        slug=slug,
        name="Growth",
        currency="SAR",
        price_sar=1699,
        billing_cycle="monthly",
        limits={"conversations_per_month": 15000},
    )
    db.add(plan)
    db.flush()

    now = datetime.now(timezone.utc)
    sub = BillingSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status="active",
        started_at=(now - timedelta(days=2)).replace(tzinfo=None),
        ends_at=(now + timedelta(days=28)).replace(tzinfo=None),
    )
    db.add(sub)
    db.commit()
    db.refresh(tenant)
    db.refresh(sub)
    return tenant, plan, sub


class TestSubscriptionPeriodUsage:
    def test_new_subscription_period_starts_fresh_counter(self, db):
        tenant, _plan, sub1 = _seed_tenant(db)

        old_row = WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub1.id,
            year=2026,
            month=6,
            service_conversations_used=900,
            marketing_conversations_used=50,
            conversations_limit=15000,
        )
        db.add(old_row)

        sub2 = BillingSubscription(
            tenant_id=tenant.id,
            plan_id=sub1.plan_id,
            status="active",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ends_at=(datetime.now(timezone.utc) + timedelta(days=30)).replace(tzinfo=None),
        )
        db.add(sub2)
        db.commit()

        ctx = _usage_period_context(db, tenant.id)
        assert ctx["mode"] == "subscription"
        assert ctx["subscription_id"] == sub2.id

        row = _get_or_create_usage(db, tenant.id, ctx)
        assert row.subscription_id == sub2.id
        assert row.service_conversations_used == 0
        assert row.marketing_conversations_used == 0

        usage = get_usage_this_month(db, tenant.id)
        assert usage["conversations_used"] == 0
        assert usage["period_mode"] == "subscription"
        assert usage["subscription_id"] == sub2.id

    def test_lifetime_total_includes_previous_periods(self, db):
        tenant, _plan, sub = _seed_tenant(db)

        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000001",
            conversation_started_at=datetime(2026, 1, 5, 12, 0, 0),
            source="inbound",
            category="service",
        ))
        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000002",
            conversation_started_at=datetime(2026, 6, 10, 12, 0, 0),
            source="inbound",
            category="service",
        ))
        db.commit()

        assert get_lifetime_conversations(db, tenant.id) == 2
        usage = get_usage_this_month(db, tenant.id)
        assert usage["lifetime_conversations_used"] == 2
        assert usage["conversations_used"] == 0

    def test_trial_falls_back_to_calendar_month(self, db):
        tenant = Tenant(name="Trial Store", subscription_status="trial")
        db.add(tenant)
        db.commit()

        ctx = _usage_period_context(db, tenant.id)
        assert ctx["mode"] == "calendar"

        row = _get_or_create_usage(db, tenant.id, ctx)
        assert row.subscription_id is None

    def test_reconcile_heals_zero_counter_from_conversation_logs(self, db):
        """Regression: fresh subscription-period row must not show 0 when logs exist."""
        tenant, _plan, sub = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        period_start = (now - timedelta(hours=1)).replace(tzinfo=None)

        sub.started_at = period_start
        db.add(
            WhatsAppUsage(
                tenant_id=tenant.id,
                subscription_id=sub.id,
                year=now.year,
                month=now.month,
                service_conversations_used=0,
                marketing_conversations_used=0,
                conversations_limit=15000,
            )
        )
        for i in range(3):
            db.add(ConversationLog(
                tenant_id=tenant.id,
                customer_phone=f"+96650000000{i}",
                conversation_started_at=now.replace(tzinfo=None),
                source="inbound",
                category="service",
            ))
        db.commit()

        usage = get_current_period_usage(db, tenant.id)
        assert usage["conversations_used"] == 3
        assert usage["current_period_conversations_used"] == 3
        assert usage["today_conversations_count"] == 3
        assert usage["period_mode"] == "subscription"
        assert usage["subscription_id"] == sub.id

    def test_track_conversation_increments_subscription_scoped_row(self, db):
        tenant, _plan, sub = _seed_tenant(db)

        result = track_conversation(
            db, tenant.id, "+966500000099",
            source="inbound", category="service",
        )
        assert result.counted is True
        assert result.used_total == 1

        row = (
            db.query(WhatsAppUsage)
            .filter(
                WhatsAppUsage.tenant_id == tenant.id,
                WhatsAppUsage.subscription_id == sub.id,
            )
            .one()
        )
        assert row.service_conversations_used == 1

        usage = get_usage_this_month(db, tenant.id)
        assert usage["conversations_used"] == 1
        assert usage["subscription_id"] == sub.id

    def test_billing_and_usage_alias_return_same_period_values(self, db):
        tenant, _plan, sub = _seed_tenant(db)
        now = datetime.now(timezone.utc)

        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000010",
            conversation_started_at=now.replace(tzinfo=None),
            source="inbound",
            category="marketing",
        ))
        db.add(
            WhatsAppUsage(
                tenant_id=tenant.id,
                subscription_id=sub.id,
                year=now.year,
                month=now.month,
                service_conversations_used=0,
                marketing_conversations_used=0,
                conversations_limit=15000,
            )
        )
        db.commit()

        direct = get_current_period_usage(db, tenant.id)
        alias = get_usage_this_month(db, tenant.id)
        assert direct["conversations_used"] == alias["conversations_used"] == 1
        assert direct["today_conversations_count"] == get_today_conversations_count(db, tenant.id)


class TestConversationSemantics:
    def test_conversation_log_is_billable_window_not_message(self, db):
        tenant = Tenant(name="Semantics Store", subscription_status="trial")
        db.add(tenant)
        db.commit()

        track_conversation(db, tenant.id, "+966500000001", source="inbound", category="service")
        track_conversation(db, tenant.id, "+966500000001", source="inbound", category="service")

        assert db.query(ConversationLog).filter(ConversationLog.tenant_id == tenant.id).count() == 1

    def test_merchant_local_day_differs_from_utc_day(self, db):
        tenant = Tenant(name="TZ Store", subscription_status="trial")
        db.add(tenant)
        db.flush()
        db.add(TenantSettings(tenant_id=tenant.id, store_settings={"timezone": "Asia/Riyadh"}))
        db.commit()

        # 2026-06-18 22:30 UTC = 2026-06-19 01:30 Riyadh (next local day)
        ref = datetime(2026, 6, 18, 22, 30, 0, tzinfo=timezone.utc)
        start, end = get_local_day_bounds_utc_naive(db, tenant.id, ref=ref)

        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000001",
            conversation_started_at=datetime(2026, 6, 18, 22, 0, 0),
            source="inbound",
            category="service",
        ))
        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000002",
            conversation_started_at=datetime(2026, 6, 18, 20, 0, 0),
            source="inbound",
            category="service",
        ))
        db.commit()

        # Only the 22:00 UTC log falls in Riyadh-local June 19
        from core.wa_usage import count_conversations_in_window  # noqa: E402

        in_local_day = count_conversations_in_window(db, tenant.id, start, end)
        assert in_local_day == 1

    def test_today_in_period_is_subset_of_period_and_today(self, db):
        from unittest.mock import patch  # noqa: PLC0415

        tenant, _plan, sub = _seed_tenant(db)
        fixed_now = datetime(2026, 6, 18, 16, 0, 0, tzinfo=timezone.utc)
        sub.started_at = datetime(2026, 6, 18, 12, 0, 0)
        db.commit()

        for hour, phone in ((8, "+966500000001"), (10, "+966500000002"), (14, "+966500000003")):
            db.add(ConversationLog(
                tenant_id=tenant.id,
                customer_phone=phone,
                conversation_started_at=datetime(2026, 6, 18, hour, 0, 0),
                source="inbound",
                category="service",
            ))
        db.commit()

        with patch("core.wa_usage._utcnow", return_value=fixed_now):
            ctx = _usage_period_context(db, tenant.id)
            usage = get_current_period_usage(db, tenant.id)
            today_in_period = get_today_in_period_billable_count(db, tenant.id, ctx)
            today_billable = get_today_billable_conversations_count(db, tenant.id)

        assert today_in_period == 1
        assert today_billable == 3
        assert usage["current_period_conversations_used"] == 1
        assert usage["today_pre_renewal_conversations_count"] == 2

    def test_messages_and_conversations_are_separate_metrics(self, db):
        tenant = Tenant(name="Msg Store", subscription_status="trial")
        db.add(tenant)
        db.commit()

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000001",
            conversation_started_at=now_naive,
            source="inbound",
            category="service",
        ))
        for _ in range(5):
            db.add(MessageEvent(tenant_id=tenant.id, direction="inbound", body="hi"))
        db.commit()

        assert get_today_billable_conversations_count(db, tenant.id) == 1
        assert get_today_messages_count(db, tenant.id) == 5

    def test_daily_activity_matches_usage_today_fields(self, db):
        tenant, _plan, _sub = _seed_tenant(db)
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000099",
            conversation_started_at=now_naive,
            source="inbound",
            category="service",
        ))
        db.commit()

        activity = get_daily_activity_metrics(db, tenant.id, "today")
        usage = get_current_period_usage(db, tenant.id)
        assert activity["conversations"] == usage["today_billable_conversations_count"]
        assert activity["metric_kind_conversations"] == "billable_conversation_windows"

    def test_audit_snapshot_exposes_raw_counts(self, db):
        tenant, _plan, sub = _seed_tenant(db)
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(ConversationLog(
            tenant_id=tenant.id,
            customer_phone="+966500000010",
            conversation_started_at=now_naive,
            source="inbound",
            category="service",
        ))
        db.commit()

        audit = get_usage_audit_snapshot(db, tenant.id)
        assert audit["subscription_id"] == sub.id
        assert audit["conversation_log_today_count"] >= 1
        assert audit["conversation_log_period_count"] >= 1
        assert "semantics" in audit
