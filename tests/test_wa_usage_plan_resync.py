"""tests/test_wa_usage_plan_resync.py
─────────────────────────────────────
Lock-in tests for the WhatsApp-usage plan-limit re-sync logic
(``core.wa_usage._get_or_create_usage`` and ``get_usage_this_month``).

Why this file exists:
    ``WhatsAppUsage.conversations_limit`` is denormalised onto the row
    so the message-throughput hot path doesn't have to JOIN BillingPlan
    on every inbound. The original implementation only wrote that
    column at row-creation time, which produced this production bug:

        Day 1 → tenant on trial. WhatsAppUsage row created with
                conversations_limit = 100.
        Day N → tenant pays for Growth (15,000 / month).
                BillingSubscription flips to active, BillingPlan.limits
                says 15,000.
        Day N → Overview UI still renders "96 / 100, ‫اقتربت من الحد‬‪"
                because the WhatsAppUsage row is frozen at limit=100,
                and ``get_usage_this_month`` reads from the row, not
                from the plan.

    Tenant 33 hit exactly this. The fix: re-sync the limit on every
    ``_get_or_create_usage`` call, also reset the alert flags on
    upgrades. These tests pin both halves so a future refactor cannot
    silently reintroduce the regression.
"""
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
    TRIAL_LIMIT,
    UNLIMITED_LIMIT_SENTINEL,
    _get_or_create_usage,
    _get_plan_limit,
    get_usage_this_month,
)


# ── Fixtures ────────────────────────────────────────────────────────────


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
    s = _make_db()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def tenant(db):
    t = Tenant(name="Tenant 33", subscription_status="active")
    db.add(t); db.commit(); db.refresh(t)
    return t


@pytest.fixture
def starter_plan(db):
    p = BillingPlan(
        tenant_id=None, slug="starter", name="Starter",
        description="Starter", currency="SAR", price_sar=449,
        billing_cycle="monthly", features=[],
        limits={"conversations_per_month": 5000},
        extra_metadata={"name_ar": "الأساسية"},
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


@pytest.fixture
def growth_plan(db):
    p = BillingPlan(
        tenant_id=None, slug="growth", name="Growth",
        description="Growth", currency="SAR", price_sar=849,
        billing_cycle="monthly", features=[],
        limits={"conversations_per_month": 15000},
        extra_metadata={"name_ar": "النمو"},
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


@pytest.fixture
def scale_plan(db):
    p = BillingPlan(
        tenant_id=None, slug="scale", name="Scale",
        description="Scale", currency="SAR", price_sar=1299,
        billing_cycle="monthly", features=[],
        limits={"conversations_per_month": -1},  # unlimited
        extra_metadata={"name_ar": "التوسع"},
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


def _activate(db, tenant, plan):
    sub = BillingSubscription(
        tenant_id=tenant.id, plan_id=plan.id, status="active",
        started_at=datetime.now(timezone.utc),
        auto_renew=True, extra_metadata={},
    )
    db.add(sub); db.commit(); db.refresh(sub)
    return sub


# ── _get_plan_limit ─────────────────────────────────────────────────────


class TestGetPlanLimit:
    def test_no_active_sub_returns_trial_limit(self, db, tenant):
        assert _get_plan_limit(db, tenant.id) == TRIAL_LIMIT

    def test_growth_returns_15000(self, db, tenant, growth_plan):
        _activate(db, tenant, growth_plan)
        assert _get_plan_limit(db, tenant.id) == 15000

    def test_starter_returns_5000(self, db, tenant, starter_plan):
        _activate(db, tenant, starter_plan)
        assert _get_plan_limit(db, tenant.id) == 5000

    def test_scale_unlimited_returns_sentinel(self, db, tenant, scale_plan):
        _activate(db, tenant, scale_plan)
        assert _get_plan_limit(db, tenant.id) == UNLIMITED_LIMIT_SENTINEL

    def test_pending_payment_does_not_grant_plan_limit(
        self, db, tenant, growth_plan,
    ):
        # Pending sub MUST NOT raise the limit — only active subs.
        sub = BillingSubscription(
            tenant_id=tenant.id, plan_id=growth_plan.id,
            status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={},
        )
        db.add(sub); db.commit()
        assert _get_plan_limit(db, tenant.id) == TRIAL_LIMIT


# ── Re-sync on existing rows ────────────────────────────────────────────


class TestUsageLimitResync:
    """The production bug pinned here: tenant 33 had a WhatsAppUsage
    row with conversations_limit=100 (created during trial). After
    Growth was activated, every fetch was returning 100 instead of
    15,000 — Overview UI rendered "96/100" forever."""

    def test_existing_row_resyncs_when_plan_upgraded(
        self, db, tenant, growth_plan,
    ):
        # Phase 1: trial row already exists with the old limit.
        now = datetime.now(timezone.utc)
        stale = WhatsAppUsage(
            tenant_id=tenant.id, year=now.year, month=now.month,
            service_conversations_used=96,
            marketing_conversations_used=0,
            conversations_limit=100,        # the stale value
            alert_80_sent=True,             # already fired during trial
            alert_100_sent=False,
        )
        db.add(stale); db.commit()

        # Phase 2: merchant upgrades to Growth — new billing period.
        sub = _activate(db, tenant, growth_plan)

        # Phase 3: paid period gets a fresh counter keyed by subscription_id.
        row = _get_or_create_usage(db, tenant.id)
        assert row.subscription_id == sub.id
        assert row.conversations_limit == 15000
        assert row.alert_80_sent is False
        assert row.alert_100_sent is False
        assert row.service_conversations_used == 0

    def test_downgrade_keeps_alert_flags(self, db, tenant, starter_plan):
        # Reverse case — merchant downgrades. We should NOT clear the
        # alert flags, otherwise they'd get spammed with old "you
        # exceeded the limit" warnings against the lower ceiling.
        now = datetime.now(timezone.utc)
        sub = _activate(db, tenant, starter_plan)
        existing = WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub.id,
            year=now.year, month=now.month,
            service_conversations_used=8000,
            marketing_conversations_used=0,
            conversations_limit=15000,      # was on Growth
            alert_80_sent=True,
            alert_100_sent=False,
        )
        db.add(existing); db.commit()

        row = _get_or_create_usage(db, tenant.id)
        assert row.conversations_limit == 5000
        # Flags preserved.
        assert row.alert_80_sent is True

    def test_no_change_when_limit_already_matches(
        self, db, tenant, growth_plan,
    ):
        sub = _activate(db, tenant, growth_plan)
        now = datetime.now(timezone.utc)
        existing = WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub.id,
            year=now.year, month=now.month,
            service_conversations_used=10,
            marketing_conversations_used=5,
            conversations_limit=15000,      # already correct
            alert_80_sent=False,
            alert_100_sent=False,
        )
        db.add(existing); db.commit()

        row = _get_or_create_usage(db, tenant.id)
        assert row.conversations_limit == 15000
        # Counters untouched.
        assert row.service_conversations_used == 10
        assert row.marketing_conversations_used == 5


# ── get_usage_this_month: API surface ───────────────────────────────────


class TestGetUsageThisMonth:
    """Pin the contract of the dict the Overview / Billing pages read."""

    def test_growth_active_shows_15000_limit(
        self, db, tenant, growth_plan,
    ):
        sub = _activate(db, tenant, growth_plan)
        # Pre-existing subscription-period row that needs limit re-sync.
        now = datetime.now(timezone.utc)
        db.add(WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub.id,
            year=now.year, month=now.month,
            service_conversations_used=96,
            marketing_conversations_used=0,
            conversations_limit=100,
            alert_80_sent=True, alert_100_sent=False,
        ))
        db.commit()

        usage = get_usage_this_month(db, tenant.id)
        assert usage["conversations_limit"]    == 15000
        assert usage["conversations_used"]     == 96
        # 96/15000 ≈ 0.6%, so all the "near limit" / "exceeded" flags
        # must be False — this is the Overview-page contract that broke
        # for tenant 33.
        assert usage["unlimited"]              is False
        assert usage["near_limit"]             is False
        assert usage["warning_70"]             is False
        assert usage["warning_90"]             is False
        assert usage["exceeded"]               is False
        assert usage["marketing_blocked"]      is False
        assert usage["emergency_stop"]         is False
        assert usage["usage_pct"] < 1.0

    def test_scale_unlimited_renders_unlimited_true(
        self, db, tenant, scale_plan,
    ):
        sub = _activate(db, tenant, scale_plan)
        now = datetime.now(timezone.utc)
        db.add(WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub.id,
            year=now.year, month=now.month,
            service_conversations_used=50000,
            marketing_conversations_used=0,
            conversations_limit=100,        # stale
            alert_80_sent=False, alert_100_sent=False,
        ))
        db.commit()

        usage = get_usage_this_month(db, tenant.id)
        assert usage["unlimited"]              is True
        # Per the API contract documented in dashboard/src/api/billing.ts,
        # ``conversations_limit == -1`` signals unlimited to the frontend.
        assert usage["conversations_limit"]    == -1
        assert usage["usage_pct"]              == 0.0
        assert usage["exceeded"]               is False
        assert usage["marketing_blocked"]      is False
        assert usage["emergency_stop"]         is False

    def test_no_active_sub_falls_back_to_trial(self, db, tenant):
        usage = get_usage_this_month(db, tenant.id)
        assert usage["conversations_limit"] == TRIAL_LIMIT
        assert usage["unlimited"]           is False

    def test_growth_at_warning_threshold(self, db, tenant, growth_plan):
        # Sanity: the warning thresholds still work against the new ceiling.
        sub = _activate(db, tenant, growth_plan)
        now = datetime.now(timezone.utc)
        db.add(WhatsAppUsage(
            tenant_id=tenant.id,
            subscription_id=sub.id,
            year=now.year, month=now.month,
            service_conversations_used=14000,   # ~93% of 15000
            marketing_conversations_used=0,
            conversations_limit=100,            # stale; will be re-synced
            alert_80_sent=False, alert_100_sent=False,
        ))
        db.commit()

        usage = get_usage_this_month(db, tenant.id)
        assert usage["conversations_limit"] == 15000
        assert 90.0 <= usage["usage_pct"] < 100.0
        assert usage["warning_90"] is True
        assert usage["exceeded"]   is False
