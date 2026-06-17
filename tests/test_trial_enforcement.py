"""
test_trial_enforcement.py
─────────────────────────
Verify the unified trial computation (compute_trial_info) and confirm
it is the single source of truth for billing enforcement.

Trial no longer falls back to tenant.created_at — see test_trial_whatsapp_lifecycle.py.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.billing import (
    FREE_TRIAL_DAYS,
    compute_trial_info,
    has_active_trial,
    has_billing_access,
)


def _make_tenant(
    *,
    created_at: datetime,
    trial_ends_at=None,
    trial_started_at=None,
    subscription_status=None,
):
    return SimpleNamespace(
        id=1,
        created_at=created_at,
        trial_ends_at=trial_ends_at,
        trial_started_at=trial_started_at,
        subscription_status=subscription_status or "trial_pending_whatsapp",
        first_whatsapp_connected_at=None,
    )


class TestTrialActive:
    def test_compute_trial_info_active(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=100),
            trial_started_at=now - timedelta(days=3),
            trial_ends_at=now + timedelta(days=7),
            subscription_status="trial_active",
        )
        info = compute_trial_info(tenant)
        assert info["is_trial"] is True
        assert info["trial_expired"] is False
        assert info["trial_days_remaining"] > 0

    def test_pending_whatsapp_is_not_active_trial(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=5),
            subscription_status="trial_pending_whatsapp",
        )
        info = compute_trial_info(tenant)
        assert info["is_trial"] is False
        assert info["trial_pending_whatsapp"] is True
        assert info["trial_expired"] is False


class TestTrialExpired:
    def test_compute_trial_info_expired(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=30),
            trial_started_at=now - timedelta(days=20),
            trial_ends_at=now - timedelta(days=2),
            subscription_status="trial_expired",
        )
        info = compute_trial_info(tenant)
        assert info["is_trial"] is False
        assert info["trial_expired"] is True
        assert info["trial_days_remaining"] == 0

    def test_created_at_fallback_removed(self):
        """Ancient created_at without trial_started_at → pending, not expired."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=FREE_TRIAL_DAYS + 30),
            subscription_status="trial_pending_whatsapp",
        )
        info = compute_trial_info(tenant)
        assert info["trial_pending_whatsapp"] is True
        assert info["trial_expired"] is False


class TestUnifiedCallChain:
    def test_has_active_trial_uses_compute_trial_info(self):
        import inspect
        src = inspect.getsource(has_active_trial)
        assert "compute_trial_info" in src

    def test_has_billing_access_uses_has_active_trial(self):
        import inspect
        src = inspect.getsource(has_billing_access)
        assert "has_active_trial" in src

    def test_billing_status_uses_compute_trial_info(self):
        import inspect
        from routers.billing import get_billing_status
        src = inspect.getsource(get_billing_status)
        assert "compute_trial_info" in src

    def test_webhook_ai_guard_uses_has_billing_access(self):
        from pathlib import Path
        webhook_src = Path(__file__).resolve().parents[1] / "backend" / "routers" / "whatsapp_webhook.py"
        text = webhook_src.read_text(encoding="utf-8")
        assert "has_billing_access" in text
