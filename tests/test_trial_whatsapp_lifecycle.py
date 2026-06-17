"""
tests/test_trial_whatsapp_lifecycle.py
──────────────────────────────────────
P0 subscription lifecycle: trial starts only after WhatsApp connects.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
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
    WhatsAppConnection,
)
from core.billing import (  # noqa: E402
    FREE_TRIAL_DAYS,
    compute_trial_info,
    has_billing_access,
    require_outbound_access,
)
from core.trial_lifecycle import (  # noqa: E402
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_EXPIRED,
    TRIAL_STATUS_PENDING_WHATSAPP,
    audit_tenant_subscription,
    build_billing_status_payload,
    init_new_tenant_trial_state,
    migrate_existing_tenant_trials,
    resolve_billing_lifecycle,
    start_trial_on_whatsapp_connect,
    subscription_period_end,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    return sessionmaker(bind=engine)


@pytest.fixture
def db():
    Session = _make_db()
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, *, name="Store", created_days_ago=30, **kwargs):
    now = datetime.now(timezone.utc)
    t = Tenant(
        name=name,
        is_active=True,
        created_at=(now - timedelta(days=created_days_ago)).replace(tzinfo=None),
        **kwargs,
    )
    init_new_tenant_trial_state(t)
    db.add(t)
    db.flush()
    return t


def _wa_conn(db, tenant_id, *, connected_days_ago=5, status="connected"):
    now = datetime.now(timezone.utc)
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        status=status,
        phone_number_id="phone123",
        provider="meta",
        connected_at=(now - timedelta(days=connected_days_ago)).replace(tzinfo=None),
        whatsapp_ai_live_since=now - timedelta(days=connected_days_ago),
    )
    db.add(conn)
    db.flush()
    return conn


class TestRegistrationDoesNotStartTrial:
    def test_new_tenant_is_pending_whatsapp(self, db):
        t = _tenant(db)
        db.commit()
        info = compute_trial_info(t)
        assert t.trial_started_at is None
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
        assert info["trial_pending_whatsapp"] is True
        assert info["is_trial"] is False
        assert has_billing_access(db, t.id) is False


class TestWhatsAppConnectionStartsTrial:
    def test_connect_starts_trial_once(self, db):
        t = _tenant(db)
        db.commit()
        started = start_trial_on_whatsapp_connect(
            db, t.id, connected_at=datetime.now(timezone.utc),
        )
        db.refresh(t)
        assert started is True
        assert t.trial_started_at is not None
        assert t.trial_ends_at is not None
        assert t.first_whatsapp_connected_at is not None
        assert t.subscription_status == TRIAL_STATUS_ACTIVE
        assert has_billing_access(db, t.id) is True

    def test_reconnect_does_not_reset_trial(self, db):
        t = _tenant(db)
        db.commit()
        first_at = datetime.now(timezone.utc) - timedelta(days=10)
        start_trial_on_whatsapp_connect(db, t.id, connected_at=first_at)
        db.refresh(t)
        original_start = t.trial_started_at
        original_end = t.trial_ends_at
        original_first_wa = t.first_whatsapp_connected_at

        start_trial_on_whatsapp_connect(
            db, t.id, connected_at=datetime.now(timezone.utc),
        )
        db.refresh(t)
        assert t.trial_started_at == original_start
        assert t.trial_ends_at == original_end
        assert t.first_whatsapp_connected_at == original_first_wa


class TestExistingTenantMigration:
    def test_no_whatsapp_becomes_pending(self, db):
        t = _tenant(db)
        t.trial_started_at = (datetime.now(timezone.utc) - timedelta(days=20)).replace(tzinfo=None)
        t.trial_ends_at = (datetime.now(timezone.utc) - timedelta(days=5)).replace(tzinfo=None)
        t.subscription_status = TRIAL_STATUS_ACTIVE
        db.commit()

        changes = migrate_existing_tenant_trials(db)
        db.refresh(t)
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
        assert t.trial_started_at is None
        assert t.trial_ends_at is None
        assert any(c["tenant_id"] == t.id for c in changes)

    def test_connected_whatsapp_anchors_trial_to_first_connection(self, db):
        t = _tenant(db, name="WA Store")
        db.commit()
        first_at = datetime.now(timezone.utc) - timedelta(days=5)
        _wa_conn(db, t.id, connected_days_ago=5)
        db.commit()

        changes = migrate_existing_tenant_trials(db)
        db.refresh(t)
        assert t.trial_started_at is not None
        assert t.first_whatsapp_connected_at is not None
        assert t.subscription_status == TRIAL_STATUS_ACTIVE
        info = compute_trial_info(t)
        assert info["is_trial"] is True
        assert info["trial_days_remaining"] <= FREE_TRIAL_DAYS
        assert any(c["tenant_id"] == t.id for c in changes)


class TestExpiryEnforcement:
    def test_expired_trial_blocks_automated_sends(self, db):
        t = _tenant(db)
        now = datetime.now(timezone.utc)
        t.trial_started_at = (now - timedelta(days=FREE_TRIAL_DAYS + 2)).replace(tzinfo=None)
        t.trial_ends_at = (now - timedelta(days=2)).replace(tzinfo=None)
        t.subscription_status = "trial_expired"
        db.commit()

        assert has_billing_access(db, t.id) is False
        with pytest.raises(HTTPException) as exc:
            require_outbound_access(db, t.id)
        assert exc.value.status_code == 402

    def test_expired_paid_subscription_blocks_automated_sends(self, db):
        t = _tenant(db)
        plan = BillingPlan(
            tenant_id=None, slug="growth", name="Growth", description="",
            currency="SAR", price_sar=849, billing_cycle="monthly",
            features=[], limits={},
        )
        db.add(plan)
        db.flush()
        now = datetime.now(timezone.utc)
        sub = BillingSubscription(
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            started_at=(now - timedelta(days=40)).replace(tzinfo=None),
            ends_at=(now - timedelta(days=10)).replace(tzinfo=None),
        )
        db.add(sub)
        db.commit()

        assert has_billing_access(db, t.id) is False

    def test_dashboard_reads_not_blocked_by_billing_guard(self):
        """Dashboard routes must not call require_outbound_access — spot-check billing status."""
        import inspect
        from routers.billing import get_billing_status
        src = inspect.getsource(get_billing_status)
        assert "require_outbound_access" not in src


class TestBillingLifecycleAuditRegression:
    def test_paid_expired_production_regression_audit_fields(self, db):
        """Regression audit for paid-expired merchant (production example: tenant 33)."""
        now = datetime.now(timezone.utc)
        t = Tenant(
            id=33,
            name="آل عايد للعسل البلدي",
            is_active=True,
            created_at=(now - timedelta(days=45)).replace(tzinfo=None),
            subscription_status=TRIAL_STATUS_EXPIRED,
            trial_started_at=(now - timedelta(days=40)).replace(tzinfo=None),
            trial_ends_at=(now - timedelta(days=26)).replace(tzinfo=None),
            first_whatsapp_connected_at=(now - timedelta(days=40)).replace(tzinfo=None),
        )
        db.add(t)
        db.flush()

        _wa_conn(db, 33, connected_days_ago=40)

        plan = BillingPlan(
            tenant_id=None, slug="growth", name="Growth", description="",
            currency="SAR", price_sar=849, billing_cycle="monthly",
            features=[], limits={},
        )
        db.add(plan)
        db.flush()

        sub = BillingSubscription(
            tenant_id=33,
            plan_id=plan.id,
            status="active",
            started_at=(now - timedelta(days=35)).replace(tzinfo=None),
            ends_at=(now - timedelta(days=5)).replace(tzinfo=None),
            extra_metadata={"paid_at": (now - timedelta(days=35)).isoformat()},
        )
        db.add(sub)
        db.commit()

        report = audit_tenant_subscription(db, 33)
        assert report["found"] is True
        assert report["store_name"] == "آل عايد للعسل البلدي"
        assert report["whatsapp_connected"] is True
        assert report["first_whatsapp_connected_at"] is not None
        assert report["trial_started_at"] is not None
        assert report["trial_ends_at"] is not None
        assert report["subscription_started_at"] is not None
        assert report["subscription_ends_at"] is not None
        assert report["lifecycle_status"] == "paid_expired"
        assert report["trial_expired"] is False
        assert report["subscription_expired"] is True
        assert report["has_paid_subscription_history"] is True
        assert report["ai_auto_replies_allowed"] is False
        assert report["manual_replies_allowed"] is True


class TestSubscriptionPeriod:
    def test_subscription_period_end_is_30_days(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = subscription_period_end(start)
        assert end == start + timedelta(days=30)


class TestBillingLifecycleResolution:
    def test_paid_expired_subscription_takes_priority_over_trial_expiry(self, db):
        """Paid subscription expiry wins over trial expiry when payment history exists."""
        now = datetime.now(timezone.utc)
        t = _tenant(db, name="Paid Then Expired")
        _wa_conn(db, t.id, connected_days_ago=40)
        t.trial_started_at = (now - timedelta(days=40)).replace(tzinfo=None)
        t.trial_ends_at = (now - timedelta(days=26)).replace(tzinfo=None)
        t.subscription_status = TRIAL_STATUS_EXPIRED
        db.flush()

        plan = BillingPlan(
            tenant_id=None, slug="growth", name="Growth", description="",
            currency="SAR", price_sar=849, billing_cycle="monthly",
            features=[], limits={},
        )
        db.add(plan)
        db.flush()

        sub = BillingSubscription(
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            started_at=(now - timedelta(days=35)).replace(tzinfo=None),
            ends_at=(now - timedelta(days=5)).replace(tzinfo=None),
            extra_metadata={"paid_at": (now - timedelta(days=35)).isoformat()},
        )
        db.add(sub)
        db.commit()

        lifecycle = resolve_billing_lifecycle(db, t.id, t, active_sub=None)
        assert lifecycle["lifecycle_status"] == "paid_expired"
        assert lifecycle["trial_expired"] is False
        assert lifecycle["subscription_expired"] is True
        assert lifecycle["has_paid_subscription_history"] is True
        assert "انتهى اشتراكك" in lifecycle["headline_ar"]
        assert "انتهت تجربتك" not in lifecycle["headline_ar"]

    def test_trial_expired_without_payment(self, db):
        now = datetime.now(timezone.utc)
        t = _tenant(db)
        _wa_conn(db, t.id, connected_days_ago=20)
        t.trial_started_at = (now - timedelta(days=20)).replace(tzinfo=None)
        t.trial_ends_at = (now - timedelta(days=6)).replace(tzinfo=None)
        t.subscription_status = TRIAL_STATUS_EXPIRED
        db.commit()

        lifecycle = resolve_billing_lifecycle(db, t.id, t, active_sub=None)
        assert lifecycle["lifecycle_status"] == "trial_expired"
        assert lifecycle["trial_expired"] is True
        assert "انتهت تجربتك" in lifecycle["headline_ar"]

    def test_build_billing_status_payload_includes_lifecycle_fields(self, db):
        now = datetime.now(timezone.utc)
        t = _tenant(db)
        _wa_conn(db, t.id, connected_days_ago=3)
        start_trial_on_whatsapp_connect(db, t.id, connected_at=now - timedelta(days=3))
        db.refresh(t)

        payload = build_billing_status_payload(
            db,
            t.id,
            t,
            active_sub=None,
            conversations_used=5,
            usage_data={
                "conversations_limit": 1000,
                "usage_pct": 0.5,
                "exceeded": False,
            },
            integration_fee_sar=59,
        )
        assert payload["lifecycle_status"] == "trial_active"
        assert payload["is_trial"] is True
        assert "ai_auto_replies_allowed" in payload
        assert payload["manual_replies_allowed"] is True
