"""Isolated PostgreSQL 18 receipt attribution proof; synthetic data only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import hashlib
import hmac
import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from models import WebhookEvent, WhatsAppConnection
from backend.tests._salla_comm_pg_harness import fresh_database
from core.salla_communication_whatsapp import record_meta_delivery_status
from services.meta_receipt_context import verify_receipt_context
from services.salla_communication_receipt_owner import CommunicationReceiptConflict


def _signed_status(state):
    status = {
        "id": "wamid.synthetic", "status": state,
        "recipient_id": "966555000000",
    }
    raw = json.dumps({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "phone-one"},
        "statuses": [status],
    }}]}]}, separators=(",", ":")).encode()
    secret = "synthetic-meta-receipt-key"
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    context = verify_receipt_context(raw, signature, secret=secret)
    assert context is not None
    return status, context


def _record(db, state):
    status, context = _signed_status(state)
    return record_meta_delivery_status(
        db, message_id="wamid.synthetic", status=state,
        phone_number_id="phone-one", receipt_payload=status,
        receipt_context=context,
    )


@pytest.mark.skipif(not os.getenv("SALLA_COMM_TEST_PG_URL"), reason="isolated PG URL required")
def test_pg18_receipt_owner_conflict_and_serialized_replay():
    with fresh_database() as (url, _name):
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE tenants (id integer PRIMARY KEY)"))
                conn.execute(text("INSERT INTO tenants (id) VALUES (1), (2)"))
            WebhookEvent.__table__.create(engine)
            WhatsAppConnection.__table__.create(engine)
            # Only columns queried by the independent owner resolver are
            # needed; no production migrations or customer data are imported.
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE campaign_send_attempts "
                    "(id integer PRIMARY KEY, provider_message_id text)"
                ))
                conn.execute(text(
                    "CREATE TABLE campaign_send_logs "
                    "(id integer PRIMARY KEY, provider_message_id text)"
                ))
                conn.execute(text(
                    "CREATE TABLE message_events "
                    "(id integer PRIMARY KEY, direction text, metadata jsonb)"
                ))
                conn.execute(text(
                    "CREATE TABLE automation_executions "
                    "(id integer PRIMARY KEY, action_taken jsonb)"
                ))
                conn.execute(text(
                    "CREATE TABLE commerce_lifecycle_notification_ledger "
                    "(id integer PRIMARY KEY, provider_message_id text)"
                ))

            signed_body = json.dumps({
                "event": "communication.whatsapp.send",
                "merchant": "12345678",
                "created_at": "2026-10-29T12:00:00Z",
                "data": {
                    "type": "order.status.updated",
                    "notifiable": ["0555000000"],
                    "content": "Synthetic status",
                    "entity": {"type": "order", "id": 42},
                },
            }, separators=(",", ":"))
            with Session(engine) as db:
                db.add(WhatsAppConnection(
                    tenant_id=1, provider="meta", phone_number_id="phone-one",
                    status="connected",
                ))
                event = WebhookEvent(
                    tenant_id=1, provider="salla_function",
                    event_type="communication.whatsapp.send",
                    store_id="12345678", status="processed",
                    signature_valid=True, raw_body=signed_body,
                    raw_headers={
                        "x-nahla-comm-body-sha256": hashlib.sha256(
                            signed_body.encode()
                        ).hexdigest(),
                    },
                    communication_send_state="accepted",
                    communication_provider_message_id="wamid.synthetic",
                )
                db.add(event)
                db.commit()
                event_id = event.id

            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO campaign_send_logs (id, provider_message_id) "
                    "VALUES (17, 'wamid.synthetic')"
                ))
            with Session(engine) as db:
                with pytest.raises(CommunicationReceiptConflict, match="owner_ambiguous"):
                    _record(db, "delivered")
                db.rollback()
                assert db.get(WebhookEvent, event_id).communication_send_state == "accepted"
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM campaign_send_logs WHERE id = 17"))

            first_locked = Event()
            second_started = Event()
            release_first = Event()

            def receive(first):
                with Session(engine) as db:
                    if not first:
                        second_started.set()
                    result = _record(db, "delivered")
                    if first:
                        first_locked.set()
                        assert release_first.wait(8)
                    db.commit()
                    return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(receive, True)
                assert first_locked.wait(8)
                b = pool.submit(receive, False)
                assert second_started.wait(8)
                assert not b.done()  # first transaction still owns the row lock
                release_first.set()
                assert a.result(timeout=8) == (True, True)
                assert b.result(timeout=8) == (True, False)

            with Session(engine) as db:
                assert _record(db, "read") == (True, True)
                db.commit()
            with Session(engine) as db:
                assert _record(db, "failed") == (True, False)
                assert db.get(WebhookEvent, event_id).communication_send_state == "read"
        finally:
            engine.dispose()
