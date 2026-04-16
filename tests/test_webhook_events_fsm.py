"""
tests/test_webhook_events_fsm.py
───────────────────────────────
Exercise the durable webhook_events queue:

  • persist_event stores and returns a row (status=received, attempts=0)
  • duplicate (provider, external_event_id) is folded into the existing row
  • claim_next_batch flips received → processing
  • mark_processed and mark_failed move through the FSM correctly
  • after MAX_ATTEMPTS failures the row lands in dead_letter
  • replay() resets dead_letter → received and clears attempts
  • count_by_status returns the right histogram
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, create_engine, event, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, WebhookEvent  # noqa: E402
from core import webhook_events as we  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # Mirror the migration's partial unique index so dedup-by-external-id works.
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event "
                "ON webhook_events (provider, external_event_id) "
                "WHERE external_event_id IS NOT NULL"
            )
        )
    Session = sessionmaker(bind=engine)
    return Session(), engine


def test_persist_event_creates_received_row():
    db, engine = _make_db()
    try:
        ev = we.persist_event(
            db,
            provider="salla",
            raw_body='{"event":"order.created","data":{"id":1}}',
            headers={"x-salla-signature": "sig"},
            parsed_payload={"event": "order.created"},
            event_type="order.created",
            external_event_id="evt-001",
            store_id="store-1",
        )
        assert ev.id is not None
        assert ev.status == we.STATUS_RECEIVED
        assert ev.attempts == 0
        assert ev.signature_valid is None
        assert ev.next_retry_at is not None
        assert ev.received_at is not None
        assert ev.raw_body and "order.created" in ev.raw_body
    finally:
        db.close()
        engine.dispose()


def test_persist_event_deduplicates_on_provider_and_external_id():
    db, engine = _make_db()
    try:
        first = we.persist_event(
            db,
            provider="salla",
            raw_body='{"id":1}',
            parsed_payload={"id": 1},
            event_type="order.created",
            external_event_id="dup-123",
        )
        second = we.persist_event(
            db,
            provider="salla",
            raw_body='{"id":1,"retry":true}',
            parsed_payload={"id": 1, "retry": True},
            event_type="order.created",
            external_event_id="dup-123",
        )
        assert second.id == first.id, "duplicate external_event_id must return the same row"
        all_rows = db.query(WebhookEvent).all()
        assert len(all_rows) == 1
    finally:
        db.close()
        engine.dispose()


def test_claim_next_batch_flips_received_to_processing():
    db, engine = _make_db()
    try:
        for i in range(3):
            we.persist_event(
                db,
                provider="salla",
                raw_body=f'{{"id":{i}}}',
                parsed_payload={"id": i},
                external_event_id=f"evt-{i}",
                event_type="order.created",
            )

        batch = we.claim_next_batch(db, limit=10)
        assert len(batch) == 3
        for ev in batch:
            assert ev.status == we.STATUS_PROCESSING

        # No more to claim
        assert we.claim_next_batch(db, limit=10) == []
    finally:
        db.close()
        engine.dispose()


def test_mark_processed_terminates_the_event():
    db, engine = _make_db()
    try:
        ev = we.persist_event(
            db,
            provider="salla",
            raw_body='{}',
            external_event_id="proc-1",
            event_type="order.created",
        )
        ev.status = we.STATUS_PROCESSING
        db.commit()

        we.mark_processed(db, ev)
        db.refresh(ev)
        assert ev.status == we.STATUS_PROCESSED
        assert ev.processed_at is not None
        assert ev.last_error is None
    finally:
        db.close()
        engine.dispose()


def test_mark_failed_schedules_retry_then_dead_letters():
    db, engine = _make_db()
    try:
        ev = we.persist_event(
            db,
            provider="salla",
            raw_body='{}',
            external_event_id="fail-1",
            event_type="order.created",
        )
        # simulate claim
        ev.status = we.STATUS_PROCESSING
        db.commit()

        # First N-1 failures → status=failed with next_retry_at set
        for attempt in range(1, we.MAX_ATTEMPTS):
            we.mark_failed(db, ev, RuntimeError(f"boom-{attempt}"))
            db.refresh(ev)
            assert ev.status == we.STATUS_FAILED
            assert ev.attempts == attempt
            assert ev.next_retry_at is not None
            assert ev.last_error and "boom" in ev.last_error

        # Final failure → dead_letter
        we.mark_failed(db, ev, "final")
        db.refresh(ev)
        assert ev.status == we.STATUS_DEAD_LETTER
        assert ev.attempts == we.MAX_ATTEMPTS
        assert ev.next_retry_at is None
    finally:
        db.close()
        engine.dispose()


def test_replay_dead_letter_resets_to_received():
    db, engine = _make_db()
    try:
        ev = we.persist_event(
            db,
            provider="salla",
            raw_body='{}',
            external_event_id="replay-1",
            event_type="order.created",
        )
        ev.status = we.STATUS_DEAD_LETTER
        ev.attempts = we.MAX_ATTEMPTS
        ev.last_error = "previous failure"
        db.commit()

        back = we.replay(db, ev.id)
        assert back is not None
        db.refresh(ev)
        assert ev.status == we.STATUS_RECEIVED
        assert ev.attempts == 0
        assert ev.last_error is None
        assert ev.next_retry_at is not None
    finally:
        db.close()
        engine.dispose()


def test_replay_bulk_moves_all_dead_letter_rows():
    db, engine = _make_db()
    try:
        for i in range(4):
            ev = we.persist_event(
                db,
                provider="salla",
                raw_body='{}',
                external_event_id=f"dlq-{i}",
                event_type="order.created",
            )
            ev.status = we.STATUS_DEAD_LETTER
            ev.attempts = we.MAX_ATTEMPTS
        db.commit()

        n = we.replay_bulk(db, status=we.STATUS_DEAD_LETTER, provider="salla")
        assert n == 4
        remaining = (
            db.query(WebhookEvent)
            .filter(WebhookEvent.status == we.STATUS_DEAD_LETTER)
            .count()
        )
        assert remaining == 0
    finally:
        db.close()
        engine.dispose()


def test_count_by_status_returns_histogram():
    db, engine = _make_db()
    try:
        for i in range(2):
            we.persist_event(
                db, provider="salla", raw_body="{}", external_event_id=f"r-{i}",
            )
        ev = we.persist_event(
            db, provider="salla", raw_body="{}", external_event_id="p-1",
        )
        ev.status = we.STATUS_PROCESSED
        db.commit()

        counts = we.count_by_status(db, provider="salla")
        assert counts[we.STATUS_RECEIVED] == 2
        assert counts[we.STATUS_PROCESSED] == 1
        assert counts[we.STATUS_DEAD_LETTER] == 0
    finally:
        db.close()
        engine.dispose()


def test_persist_event_invalid_signature_stored_as_failed_for_audit():
    db, engine = _make_db()
    try:
        ev = we.persist_event(
            db,
            provider="salla",
            raw_body='{"bad": true}',
            external_event_id="audit-1",
            signature_valid=False,
            initial_status=we.STATUS_FAILED,
            initial_error="invalid_signature",
        )
        assert ev.status == we.STATUS_FAILED
        assert ev.signature_valid is False
        assert ev.last_error == "invalid_signature"
        # failed events are not immediately retried without an admin action,
        # because next_retry_at was not set for the failed-initial path.
        assert ev.next_retry_at is None
    finally:
        db.close()
        engine.dispose()
