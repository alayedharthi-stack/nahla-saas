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


@pytest.mark.parametrize("apply,accepted", [(False, True), (True, True), (True, False)])
def test_operator_claims_before_send_and_records_truth(apply, accepted):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from scripts.operators.recover_unsubscribe_confirmation import recover
    from models import Customer, WhatsAppConnection, Conversation
    row = customer()
    connection = SimpleNamespace(phone_number_id="PH1", status="connected")
    conversation = SimpleNamespace(id=44)
    db = MagicMock()
    rows = {Customer: row, WhatsAppConnection: connection, Conversation: conversation}
    def query(model):
        q = MagicMock()
        q.filter_by.return_value = q
        q.with_for_update.return_value = q
        q.populate_existing.return_value = q
        q.order_by.return_value = q
        q.one_or_none.return_value = q.one.return_value = q.first.return_value = rows[model]
        return q
    db.query.side_effect = query
    async def post(**kwargs):
        assert db.commit.called
        assert row.extra_metadata[CLAIM_KEY]["status"] == "request_started"
        assert kwargs["_unsubscribe_notice"] is True
        if accepted:
            kwargs["_result_sink"].update(wamid="wamid.synthetic", classification="ok")
        return accepted
    with patch("core.automation_send_guard.evaluate_unsubscribe_notice_send",
               return_value=SimpleNamespace(block=False)), patch(
            "routers.whatsapp_webhook._post_wa", new=AsyncMock(side_effect=post)) as wire, patch(
            "core.conversation_engine.StateManager.save_message") as save:
        report = asyncio.run(recover(db, tenant_id=77, customer_id=8,
            phone="966500000123", expected_pending_at=PENDING, apply=apply))
        if not apply:
            assert report["action"] == "would_send"
            wire.assert_not_awaited()
            assert CLAIM_KEY not in row.extra_metadata
        elif accepted:
            assert report["action"] == "accepted"
            assert save.call_args.kwargs["extra_metadata"]["provider_send"]["status"] == "sent"
            assert row.extra_metadata["pending_unsubscribe_prompt_sent_at"]
        else:
            assert report["action"] == "not_confirmed"
            save.assert_not_called()
            assert "pending_unsubscribe_prompt_sent_at" not in row.extra_metadata
        if apply:
            with pytest.raises(ValueError, match="already_sent|already_claimed"):
                check(row)
