"""Platform-wide historical trial reconciliation after WhatsApp connect (WA-1).

Tenant IDs in fixtures are not production special-cases.
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
    Integration,
    Tenant,
    TenantSettings,
    WhatsAppConnection,
)
from core.billing import FREE_TRIAL_DAYS, has_billing_access  # noqa: E402
from core.trial_lifecycle import (  # noqa: E402
    RECONCILE_DECISION_AMBIGUOUS,
    RECONCILE_DECISION_SKIP,
    RECONCILE_ELIGIBLE,
    RECONCILE_SKIP_ALREADY_STARTED,
    RECONCILE_SKIP_AMBIGUOUS_CONNECTED_EVIDENCE,
    RECONCILE_SKIP_AMBIGUOUS_PARTIAL_LIFECYCLE,
    RECONCILE_SKIP_EXPIRED_TRIAL,
    RECONCILE_SKIP_GIFT,
    RECONCILE_SKIP_NO_AUTHORITATIVE_EVIDENCE,
    RECONCILE_SKIP_NO_PHONE_IDENTITY,
    RECONCILE_SKIP_PAID,
    RECONCILE_SKIP_PAID_HISTORY,
    RECONCILE_SKIP_PARTNER_OVERRIDE,
    RECONCILE_SKIP_SALLA_MANAGED,
    RECONCILE_SKIP_STATUS_NOT_ALLOWED,
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_EXPIRED,
    TRIAL_STATUS_PENDING_WHATSAPP,
    classify_missing_trial_after_whatsapp,
    init_new_tenant_trial_state,
    reconcile_missing_trial_after_whatsapp_connect,
    reconcile_missing_trials_after_whatsapp_connect,
    resolve_billing_lifecycle,
    start_trial_on_whatsapp_connect,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)


@pytest.fixture
def db():
    Session = _make_db()
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _tenant(db, *, name="متجر تجريبي عام"):
    t = Tenant(name=name, is_active=True, created_at=datetime.now(timezone.utc).replace(tzinfo=None))
    init_new_tenant_trial_state(t)
    db.add(t)
    db.flush()
    return t


def _wa(
    db,
    tenant_id,
    *,
    status="connected",
    provider="meta",
    connection_type="embedded",
    connection_mode=None,
    connected_days_ago=1,
    phone_number_id="1127027493832697",
    webhook_verified=True,
    access_token="tok-regression",
):
    now = datetime.now(timezone.utc)
    connected_at = (now - timedelta(days=connected_days_ago)).replace(tzinfo=None)
    meta = {}
    if connection_mode:
        meta["connection_mode"] = connection_mode
        meta["smb_sync"] = {
            "history": {"accepted": True},
            "smb_app_state_sync": {"accepted": True},
        }
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        status=status,
        provider=provider,
        connection_type=connection_type,
        phone_number_id=phone_number_id,
        access_token=access_token,
        connected_at=connected_at,
        whatsapp_ai_live_since=now - timedelta(days=connected_days_ago),
        webhook_verified=webhook_verified,
        extra_metadata=meta or None,
    )
    db.add(conn)
    db.flush()
    return conn


class TestHistoricalCoexistenceAndEmbedded:
    def test_coexistence_connected_null_trial_is_repaired(self, db):
        t = _tenant(db, name="متجر تجريبي عام")
        conn = _wa(
            db, t.id,
            status="otp_pending",
            connection_type="embedded",
            connection_mode="coexistence",
            connected_days_ago=1,
        )
        db.commit()

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is True
        assert result["reason"] == RECONCILE_ELIGIBLE
        assert t.first_whatsapp_connected_at == conn.connected_at
        assert t.trial_started_at == conn.connected_at
        assert t.trial_ends_at == (conn.connected_at + timedelta(days=FREE_TRIAL_DAYS))
        assert t.subscription_status == TRIAL_STATUS_ACTIVE
        assert has_billing_access(db, t.id) is True
        lifecycle = resolve_billing_lifecycle(db, t.id, t)
        assert lifecycle["lifecycle_status"] == "trial_active"
        assert lifecycle["trial_pending_whatsapp"] is False

    def test_embedded_successful_connection_null_trial_is_repaired(self, db):
        t = _tenant(db, name="متجر ملابس تجريبي")
        conn = _wa(db, t.id, status="connected", connection_type="embedded", connected_days_ago=2)
        db.commit()

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is True
        assert t.trial_started_at == conn.connected_at
        assert has_billing_access(db, t.id) is True
        assert resolve_billing_lifecycle(db, t.id, t)["lifecycle_status"] == "trial_active"


class TestProtectedLifecyclesUnchanged:
    def test_already_started_trial_unchanged(self, db):
        t = _tenant(db)
        first = datetime.now(timezone.utc) - timedelta(days=3)
        start_trial_on_whatsapp_connect(db, t.id, connected_at=first)
        _wa(db, t.id, connected_days_ago=3)
        db.commit()
        original = (t.trial_started_at, t.trial_ends_at, t.subscription_status)

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_ALREADY_STARTED
        assert (t.trial_started_at, t.trial_ends_at, t.subscription_status) == original

    def test_expired_trial_is_not_restarted(self, db):
        t = _tenant(db)
        now = datetime.now(timezone.utc)
        t.trial_started_at = (now - timedelta(days=20)).replace(tzinfo=None)
        t.trial_ends_at = (now - timedelta(days=6)).replace(tzinfo=None)
        t.subscription_status = TRIAL_STATUS_EXPIRED
        _wa(db, t.id, connected_days_ago=20)
        db.commit()

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_EXPIRED_TRIAL
        assert t.subscription_status == TRIAL_STATUS_EXPIRED
        assert t.trial_started_at is not None

    def test_paid_tenant_unchanged(self, db):
        t = _tenant(db)
        plan = BillingPlan(
            tenant_id=None, slug="starter", name="Starter", description="",
            currency="SAR", price_sar=899, billing_cycle="monthly",
            features=[], limits={},
        )
        db.add(plan)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add(BillingSubscription(
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            started_at=now.replace(tzinfo=None),
            ends_at=(now + timedelta(days=30)).replace(tzinfo=None),
            extra_metadata={"paid_at": now.isoformat()},
        ))
        _wa(db, t.id)
        db.commit()

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_PAID
        assert t.trial_started_at is None
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP

    def test_paid_history_does_not_become_trial(self, db):
        t = _tenant(db)
        plan = BillingPlan(
            tenant_id=None, slug="growth", name="Growth", description="",
            currency="SAR", price_sar=849, billing_cycle="monthly",
            features=[], limits={},
        )
        db.add(plan)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add(BillingSubscription(
            tenant_id=t.id,
            plan_id=plan.id,
            status="expired",
            started_at=(now - timedelta(days=40)).replace(tzinfo=None),
            ends_at=(now - timedelta(days=10)).replace(tzinfo=None),
            extra_metadata={"paid_at": (now - timedelta(days=40)).isoformat()},
        ))
        _wa(db, t.id)
        db.commit()

        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_PAID_HISTORY
        assert t.trial_started_at is None

    def test_salla_managed_unchanged(self, db):
        t = _tenant(db)
        _wa(db, t.id)
        db.add(Integration(
            tenant_id=t.id,
            provider="salla",
            external_store_id="store-gen",
            config={"billing_status": "active"},
            enabled=True,
        ))
        db.commit()
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_SALLA_MANAGED
        assert t.trial_started_at is None

    def test_gift_grant_unchanged(self, db):
        t = _tenant(db)
        _wa(db, t.id)
        now = datetime.now(timezone.utc)
        db.add(TenantSettings(
            tenant_id=t.id,
            extra_metadata={
                "billing": {
                    "manual_gift_grant": {
                        "enabled": True,
                        "ends_at": (now + timedelta(days=10)).isoformat(),
                    }
                }
            },
        ))
        db.commit()
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_GIFT
        assert t.trial_started_at is None


class TestIdempotencyAndEvidence:
    def test_reconnect_does_not_grant_new_trial(self, db):
        t = _tenant(db)
        first = datetime.now(timezone.utc) - timedelta(days=4)
        start_trial_on_whatsapp_connect(db, t.id, connected_at=first)
        _wa(db, t.id, connected_days_ago=4)
        original_end = t.trial_ends_at
        db.commit()

        start_trial_on_whatsapp_connect(db, t.id, connected_at=datetime.now(timezone.utc))
        reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert t.trial_ends_at == original_end

    def test_repeated_reconciliation_is_noop(self, db):
        t = _tenant(db)
        _wa(db, t.id, connected_days_ago=1)
        db.commit()
        first = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        started = t.trial_started_at
        second = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert first["applied"] is True
        assert second["applied"] is False
        assert second["reason"] == RECONCILE_SKIP_ALREADY_STARTED
        assert t.trial_started_at == started

    def test_webhook_only_without_connected_at_is_not_granted(self, db):
        t = _tenant(db)
        db.add(WhatsAppConnection(
            tenant_id=t.id,
            status="pending",
            provider="meta",
            phone_number_id="phone-webhook-only",
            connected_at=None,
            webhook_verified=True,
            last_webhook_received_at=datetime.now(timezone.utc),
        ))
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["decision"] == "skip"
        assert classified["reason"] == RECONCILE_SKIP_NO_AUTHORITATIVE_EVIDENCE
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert t.trial_started_at is None
        assert has_billing_access(db, t.id) is False

    def test_disconnected_without_phone_id_is_not_granted(self, db):
        t = _tenant(db)
        db.add(WhatsAppConnection(
            tenant_id=t.id,
            status="disconnected",
            provider="meta",
            connection_type="embedded",
            phone_number_id=None,
            connected_at=(datetime.now(timezone.utc) - timedelta(days=120)).replace(tzinfo=None),
            webhook_verified=True,
        ))
        db.commit()
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_NO_PHONE_IDENTITY
        assert t.trial_started_at is None

    def test_historical_window_already_elapsed_marks_expired_not_extended(self, db):
        t = _tenant(db)
        _wa(db, t.id, connected_days_ago=FREE_TRIAL_DAYS + 3)
        db.commit()
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is True
        assert t.subscription_status == TRIAL_STATUS_EXPIRED
        assert has_billing_access(db, t.id) is False
        assert resolve_billing_lifecycle(db, t.id, t)["lifecycle_status"] == "trial_expired"

    def test_dry_run_does_not_write(self, db):
        t = _tenant(db)
        _wa(db, t.id)
        db.commit()
        report = reconcile_missing_trials_after_whatsapp_connect(db, dry_run=True)
        db.refresh(t)
        assert report["dry_run"] is True
        assert report["eligible"] == 1
        assert report["applied"] == []
        assert t.trial_started_at is None

    def test_failure_midway_does_not_leave_partial_lifecycle(self, db, monkeypatch):
        t = _tenant(db)
        _wa(db, t.id)
        db.commit()

        def boom(*_a, **_k):
            raise RuntimeError("forced failure")

        monkeypatch.setattr(
            "core.trial_lifecycle.start_trial_on_whatsapp_connect",
            boom,
        )
        report = reconcile_missing_trials_after_whatsapp_connect(db, dry_run=False)
        db.refresh(t)
        assert t.trial_started_at is None
        assert t.first_whatsapp_connected_at is None
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
        assert any(row["reason"] == "skip_reconcile_error" for row in report["skipped"])

    def test_does_not_change_ai_settings(self, db):
        t = _tenant(db)
        _wa(db, t.id)
        db.add(TenantSettings(
            tenant_id=t.id,
            ai_settings={"store_ai_mode": "test", "store_ai_enabled": False},
        ))
        db.commit()
        reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        settings = db.query(TenantSettings).filter_by(tenant_id=t.id).one()
        assert settings.ai_settings["store_ai_mode"] == "test"
        assert settings.ai_settings["store_ai_enabled"] is False

    def test_batch_apply_is_platform_wide_not_named_tenant(self, db):
        a = _tenant(db, name="متجر عطور تجريبي")
        b = _tenant(db, name="متجر أحذية تجريبي")
        _wa(db, a.id, connection_mode="coexistence", status="otp_pending")
        _wa(db, b.id, connection_type="embedded", status="connected", phone_number_id="phone-b")
        db.commit()
        report = reconcile_missing_trials_after_whatsapp_connect(db, dry_run=False)
        assert report["eligible"] == 2
        assert report["dry_run"] is False
        db.refresh(a)
        db.refresh(b)
        assert a.subscription_status == TRIAL_STATUS_ACTIVE
        assert b.subscription_status == TRIAL_STATUS_ACTIVE
        assert has_billing_access(db, a.id) is True
        assert has_billing_access(db, b.id) is True


class TestPositiveAllowlistAndPartialLifecycle:
    def test_pending_lifecycle_with_full_nulls_is_eligible(self, db):
        t = _tenant(db, name="متجر قمصان تجريبي")
        _wa(db, t.id)
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
        assert t.first_whatsapp_connected_at is None
        assert t.trial_started_at is None
        assert t.trial_ends_at is None
        assert classified["decision"] == "apply"
        assert classified["reason"] == RECONCILE_ELIGIBLE

    def test_partial_first_wa_without_trial_dates_is_ambiguous(self, db):
        t = _tenant(db)
        t.first_whatsapp_connected_at = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).replace(tzinfo=None)
        _wa(db, t.id, connected_days_ago=2)
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["decision"] == RECONCILE_DECISION_AMBIGUOUS
        assert classified["reason"] == RECONCILE_SKIP_AMBIGUOUS_PARTIAL_LIFECYCLE
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert t.trial_started_at is None
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP

    def test_partial_trial_ends_without_start_is_ambiguous(self, db):
        t = _tenant(db)
        t.trial_ends_at = (
            datetime.now(timezone.utc) + timedelta(days=10)
        ).replace(tzinfo=None)
        _wa(db, t.id)
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["decision"] == RECONCILE_DECISION_AMBIGUOUS
        assert classified["reason"] == RECONCILE_SKIP_AMBIGUOUS_PARTIAL_LIFECYCLE
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert t.trial_started_at is None

    def test_cancelled_status_is_not_reconciled(self, db):
        t = _tenant(db)
        t.subscription_status = "cancelled"
        _wa(db, t.id)
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["decision"] == RECONCILE_DECISION_SKIP
        assert classified["reason"] == RECONCILE_SKIP_STATUS_NOT_ALLOWED
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert t.trial_started_at is None

    def test_unknown_empty_status_is_not_reconciled(self, db):
        t = _tenant(db)
        t.subscription_status = None
        _wa(db, t.id)
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["reason"] == RECONCILE_SKIP_STATUS_NOT_ALLOWED
        assert reconcile_missing_trial_after_whatsapp_connect(db, t.id)["applied"] is False

    def test_partner_override_is_not_reconciled(self, db):
        t = _tenant(db)
        _wa(db, t.id)
        now = datetime.now(timezone.utc)
        db.add(TenantSettings(
            tenant_id=t.id,
            extra_metadata={
                "billing": {
                    "partner_testing_override": {
                        "enabled": True,
                        "expires_at": (now + timedelta(days=14)).isoformat(),
                    }
                }
            },
        ))
        db.commit()
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert result["reason"] == RECONCILE_SKIP_PARTNER_OVERRIDE
        assert t.trial_started_at is None

    def test_dialog360_connected_at_is_ambiguous_not_auto_repaired(self, db):
        t = _tenant(db, name="متجر مستحضرات تجريبي")
        _wa(
            db, t.id,
            provider="dialog360",
            connection_type="coexistence",
            phone_number_id="d360-phone-1",
        )
        db.commit()
        classified = classify_missing_trial_after_whatsapp(db, t)
        assert classified["decision"] == RECONCILE_DECISION_AMBIGUOUS
        assert classified["reason"] == RECONCILE_SKIP_AMBIGUOUS_CONNECTED_EVIDENCE
        result = reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(t)
        assert result["applied"] is False
        assert t.trial_started_at is None
        assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
        assert has_billing_access(db, t.id) is False

    def test_does_not_rewrite_whatsapp_credentials(self, db):
        t = _tenant(db)
        conn = _wa(
            db, t.id,
            status="otp_pending",
            connection_mode="coexistence",
            access_token="keep-this-token",
            phone_number_id="keep-phone-id",
        )
        db.commit()
        reconcile_missing_trial_after_whatsapp_connect(db, t.id)
        db.refresh(conn)
        assert conn.access_token == "keep-this-token"
        assert conn.phone_number_id == "keep-phone-id"
        assert conn.status == "otp_pending"

    def test_trial_banner_still_keys_off_lifecycle_status(self):
        banner = (REPO_ROOT / "dashboard" / "src" / "components" / "ui" / "TrialBanner.tsx")
        text = banner.read_text(encoding="utf-8")
        assert "if (status.lifecycle_status) return status.lifecycle_status" in text
        assert "trial_pending_whatsapp" in text
