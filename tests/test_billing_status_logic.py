from datetime import datetime, timedelta, timezone

from core.billing import FREE_TRIAL_DAYS


def _trial_days_remaining(created_at: datetime, now: datetime) -> int:
    return max(0, FREE_TRIAL_DAYS - (now - created_at).days)


def test_trial_days_remaining_positive_before_expiry():
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(days=3)

    remaining = _trial_days_remaining(created_at, now)

    assert remaining == FREE_TRIAL_DAYS - 3
    assert remaining > 0


def test_trial_days_remaining_zero_after_expiry():
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(days=FREE_TRIAL_DAYS + 5)

    remaining = _trial_days_remaining(created_at, now)

    assert remaining == 0
