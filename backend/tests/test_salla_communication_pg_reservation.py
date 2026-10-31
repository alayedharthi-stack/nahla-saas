"""Isolated PostgreSQL proof of the actual send and nonce SQL primitives.

Set SALLA_COMM_TEST_PG_URL to a disposable local PostgreSQL database. This
test creates and drops only its own two tables; it never connects to Salla,
Meta, Railway, or a production database.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import os

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import WebhookEvent, SallaCommunicationFunctionNonce
from backend.tests._salla_comm_pg_harness import fresh_database


@pytest.fixture
def isolated_engine():
    with fresh_database() as (url, _name):
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.mark.skipif(not os.getenv("SALLA_COMM_TEST_PG_URL"), reason="isolated local PG URL required")
def test_postgres_one_send_reservation_and_nonce_replay(isolated_engine):
    engine = isolated_engine
    nonce_table = SallaCommunicationFunctionNonce.__table__
    event_table = WebhookEvent.__table__
    event_table.create(engine)
    nonce_table.create(engine)
    try:
        # Migration 0023 establishes this exact production dedupe index;
        # model.create() alone does not reproduce it.
        with engine.begin() as db:
            db.execute(text(
                "CREATE UNIQUE INDEX uq_webhook_events_provider_event "
                "ON webhook_events(provider, external_event_id) "
                "WHERE external_event_id IS NOT NULL"
            ))
        with Session(engine) as db:
            ev = WebhookEvent(
                provider="salla_function", event_type="communication.whatsapp.send",
                external_event_id="synthetic-event", store_id="synthetic-store",
                status="processed", signature_valid=True,
            )
            db.add(ev)
            db.commit()
            event_id = ev.id
        with Session(engine) as db:
            db.add(WebhookEvent(
                provider="salla_function", event_type="communication.whatsapp.send",
                external_event_id="synthetic-event", store_id="synthetic-store",
                status="processed", signature_valid=True,
            ))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

        reserved = Event()
        release = Event()
        second_started = Event()

        def claim(first: bool):
            with Session(engine) as db:
                if not first:
                    second_started.set()
                result = db.execute(
                    update(WebhookEvent).where(
                        WebhookEvent.id == event_id,
                        WebhookEvent.communication_send_state.is_(None),
                    ).values(communication_send_state="sending").returning(WebhookEvent.id)
                ).scalar_one_or_none()
                if first:
                    reserved.set()
                    assert release.wait(4)
                db.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(claim, True)
            assert reserved.wait(4)
            b = pool.submit(claim, False)
            assert second_started.wait(4)
            release.set()
            assert a.result(timeout=4) == event_id
            assert b.result(timeout=4) is None

        with Session(engine) as db:
            assert db.get(WebhookEvent, event_id).communication_send_state == "sending"
            db.add(SallaCommunicationFunctionNonce(nonce="a" * 32))
            db.commit()
        with Session(engine) as db:
            db.add(SallaCommunicationFunctionNonce(nonce="a" * 32))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
    finally:
        nonce_table.drop(engine, checkfirst=True)
        event_table.drop(engine, checkfirst=True)
