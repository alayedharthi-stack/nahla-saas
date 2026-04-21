"""
test_trial_enforcement.py
─────────────────────────
Verify the unified trial computation (compute_trial_info) and confirm
it is the single source of truth for all five consumers:

  1. GET /billing/status  → frontend TrialBanner
  2. has_billing_access() → automation engine guard
  3. has_billing_access() → webhook AI-reply guard
  4. require_billing_access() → campaigns guard
  5. require_billing_access() → automations router guard

Three scenarios tested:
  A. Trial active             → is_trial=True,  days_left > 0
  B. Trial expired            → is_trial=False, trial_expired=True
  C. Fallback (no trial_ends_at) → computes from created_at
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.billing import (
    FREE_TRIAL_DAYS,
    compute_trial_info,
    has_active_trial,
    has_billing_access,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_tenant(
    *,
    created_at: datetime,
    trial_ends_at=None,
    trial_started_at=None,
):
    """Fake Tenant object with the three relevant datetime fields."""
    return SimpleNamespace(
        id=1,
        created_at=created_at,
        trial_ends_at=trial_ends_at,
        trial_started_at=trial_started_at,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Scenario A: Trial active
# ══════════════════════════════════════════════════════════════════════════════

class TestTrialActive:
    """trial_ends_at is in the future → trial is active."""

    def test_compute_trial_info_active(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=100),
            trial_ends_at=now + timedelta(days=7),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True
        assert info["trial_expired"] is False
        assert info["trial_days_remaining"] > 0
        assert info["trial_days_remaining"] <= 8  # 7 full days + partial today

    def test_compute_trial_info_active_from_started_at(self):
        """trial_started_at set 3 days ago, no trial_ends_at → 11 days left."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=100),
            trial_started_at=now - timedelta(days=3),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True
        assert info["trial_days_remaining"] == FREE_TRIAL_DAYS - 3

    def test_banner_shows_trial(self):
        """billing/status returns is_trial=True → banner shows."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=100),
            trial_ends_at=now + timedelta(days=5),
        )
        info = compute_trial_info(tenant)
        # Simulate what billing/status does (sub is None)
        is_trial = info["is_trial"]
        trial_expired = info["trial_expired"]

        assert is_trial is True
        assert trial_expired is False


# ══════════════════════════════════════════════════════════════════════════════
# Scenario B: Trial expired
# ══════════════════════════════════════════════════════════════════════════════

class TestTrialExpired:
    """trial_ends_at is in the past → trial expired."""

    def test_compute_trial_info_expired(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=30),
            trial_ends_at=now - timedelta(days=2),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is False
        assert info["trial_expired"] is True
        assert info["trial_days_remaining"] == 0

    def test_compute_trial_info_expired_from_created_at(self):
        """No trial_ends_at, created_at was 20 days ago → expired."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=FREE_TRIAL_DAYS + 5),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is False
        assert info["trial_expired"] is True
        assert info["trial_days_remaining"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Scenario C: Fallback — no trial_ends_at, uses created_at
# ══════════════════════════════════════════════════════════════════════════════

class TestTrialFallback:
    """No trial_ends_at set → computes from trial_started_at or created_at."""

    def test_fallback_to_created_at_active(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=5),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True
        assert info["trial_days_remaining"] == FREE_TRIAL_DAYS - 5

    def test_fallback_to_created_at_expired(self):
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=FREE_TRIAL_DAYS + 1),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is False
        assert info["trial_expired"] is True

    def test_trial_started_at_takes_priority_over_created_at(self):
        """trial_started_at is more recent than created_at → uses it."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=100),
            trial_started_at=now - timedelta(days=2),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True
        assert info["trial_days_remaining"] == FREE_TRIAL_DAYS - 2

    def test_trial_ends_at_overrides_everything(self):
        """Even if created_at is ancient, trial_ends_at in future = active."""
        now = datetime.now(timezone.utc)
        tenant = _make_tenant(
            created_at=now - timedelta(days=365),
            trial_started_at=now - timedelta(days=365),
            trial_ends_at=now + timedelta(days=10),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True
        assert info["trial_days_remaining"] > 0

    def test_naive_datetime_handled(self):
        """DB stores naive UTC datetimes — must still work."""
        now_naive = datetime.utcnow()
        tenant = _make_tenant(
            created_at=now_naive - timedelta(days=5),
            trial_ends_at=now_naive + timedelta(days=3),
        )
        info = compute_trial_info(tenant)

        assert info["is_trial"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Verify the call chain is unified
# ══════════════════════════════════════════════════════════════════════════════

class TestUnifiedCallChain:
    """
    Verify that has_active_trial and has_billing_access delegate
    to compute_trial_info, not a separate implementation.
    """

    def test_has_active_trial_uses_compute_trial_info(self):
        """
        If compute_trial_info says is_trial=True,
        has_active_trial must agree (they share the function).
        We can verify by checking the source code:
          has_active_trial → compute_trial_info(tenant)["is_trial"]
        """
        import inspect
        src = inspect.getsource(has_active_trial)
        assert "compute_trial_info" in src

    def test_has_billing_access_uses_has_active_trial(self):
        import inspect
        src = inspect.getsource(has_billing_access)
        assert "has_active_trial" in src

    def test_billing_status_uses_compute_trial_info(self):
        """billing/status endpoint must use compute_trial_info."""
        import inspect
        from routers.billing import get_billing_status
        src = inspect.getsource(get_billing_status)
        assert "compute_trial_info" in src

    def test_automation_engine_uses_has_billing_access(self):
        import inspect
        from core.automation_engine import process_pending_events
        src = inspect.getsource(process_pending_events)
        assert "has_billing_access" in src

    def test_campaigns_uses_require_billing_access(self):
        import inspect
        from routers.campaigns import create_campaign
        src = inspect.getsource(create_campaign)
        assert "require_billing_access" in src

    def test_webhook_ai_guard_uses_has_billing_access(self):
        """The webhook AI-reply path must call has_billing_access."""
        from pathlib import Path
        webhook_src = Path(__file__).resolve().parents[1] / "backend" / "routers" / "whatsapp_webhook.py"
        text = webhook_src.read_text(encoding="utf-8")
        assert "has_billing_access" in text
