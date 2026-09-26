from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from scripts.operators.recover_unsubscribe_confirmation import eligibility, CLAIM_KEY

NOW = datetime.now(timezone.utc)
PENDING = (NOW - timedelta(minutes=15)).isoformat()


def customer(**extra):
    return SimpleNamespace(id=8, tenant_id=77, phone="+966500000123", extra_metadata={
        "pending_unsubscribe": True, "pending_unsubscribe_at": PENDING,
        "pending_unsubscribe_expires_at": (NOW + timedelta(hours=23)).isoformat(), **extra})


def check(row, **kwargs):
    return eligibility(row, tenant_id=77, customer_id=8, phone="966500000123", **kwargs)


def test_current_pending_customer_is_eligible():
    assert check(customer(), expected_pending_at=PENDING) == PENDING


@pytest.mark.parametrize("meta,reason", [
    ({"pending_unsubscribe": False}, "not_pending"),
    ({"is_unsubscribed": True}, "not_pending"),
    ({"pending_unsubscribe_expires_at": (NOW - timedelta(seconds=1)).isoformat()}, "not_pending"),
    ({"pending_unsubscribe_prompt_sent_at": NOW.isoformat()}, "prompt_already_sent"),
    ({CLAIM_KEY: {"pending_at": PENDING, "status": "request_started"}}, "already_claimed"),
    ({CLAIM_KEY: {"pending_at": PENDING, "status": "accepted"}}, "already_claimed"),
    ({CLAIM_KEY: {"pending_at": PENDING, "status": "not_confirmed"}}, "already_claimed"),
])
def test_recovery_refuses_changed_or_previously_attempted_state(meta, reason):
    with pytest.raises(ValueError, match=reason):
        check(customer(**meta))


def test_exact_observed_request_is_required():
    with pytest.raises(ValueError, match="state_changed"):
        check(customer(), expected_pending_at=NOW.isoformat())


@pytest.mark.parametrize("field,value", [("tenant_id", 78), ("id", 9), ("phone", "+966500000124")])
def test_recipient_mismatch_is_rejected(field, value):
    row = customer()
    setattr(row, field, value)
    with pytest.raises(ValueError, match="recipient_mismatch"):
        check(row)
