"""Synthetic owner-claim checks; does not connect to a database or Meta."""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from services.salla_communication_receipt_owner import competing_receipt_owner


class CompilingQuery:
    def __init__(self, column, hit):
        self.column = column
        self.hit = hit
        self.predicates = ()

    def filter(self, *predicates):
        self.predicates = predicates
        return self

    def first(self):
        # Compile the real model expressions for PostgreSQL without executing.
        statement = select(self.column).where(*self.predicates)
        str(statement.compile(dialect=postgresql.dialect()))
        return (1,) if self.column.class_.__name__ == self.hit else None


class CompilingSession:
    def __init__(self, hit=None):
        self.hit = hit
        self.seen = []

    def query(self, column):
        self.seen.append(column.class_.__name__)
        return CompilingQuery(column, self.hit)


def test_all_existing_non_communication_sender_families_block_a_colliding_id():
    families = (
        "CampaignSendAttempt", "CampaignSendLog", "MessageEvent",
        "AutomationExecution", "CommerceLifecycleNotificationLedger",
    )
    for family in families:
        db = CompilingSession(family)
        assert competing_receipt_owner(db, "wamid.synthetic") is True
        assert db.seen == list(families)
    db = CompilingSession()
    assert competing_receipt_owner(db, "wamid.synthetic") is False
    assert db.seen == list(families)
    assert competing_receipt_owner(db, "") is True


@pytest.mark.asyncio
async def test_communication_projection_commits_before_legacy_ledger(monkeypatch):
    from routers import whatsapp_webhook as route

    class FakeDb:
        committed = False
        rolled_back = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    db = FakeDb()
    monkeypatch.setattr(route, "get_db", lambda: iter((db,)))
    calls = []

    def accepted(_db, **kwargs):
        calls.append(kwargs)
        return True, True

    monkeypatch.setattr(
        "core.salla_communication_whatsapp.record_meta_delivery_status", accepted,
    )
    await route._handle_message_status(
        {"id": "wamid.synthetic", "status": "delivered", "recipient_id": "966555000000"},
        receipt_context=object(), phone_number_id="phone-one",
    )
    assert len(calls) == 1 and calls[0]["phone_number_id"] == "phone-one"
    assert db.committed and not db.rolled_back and db.closed


@pytest.mark.asyncio
async def test_owner_conflict_rolls_back_before_legacy_ledger(monkeypatch):
    from routers import whatsapp_webhook as route
    from services.salla_communication_receipt_owner import CommunicationReceiptConflict

    class FakeDb:
        committed = False
        rolled_back = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    db = FakeDb()
    monkeypatch.setattr(route, "get_db", lambda: iter((db,)))

    def conflict(_db, **_kwargs):
        raise CommunicationReceiptConflict("synthetic_owner_collision")

    monkeypatch.setattr(
        "core.salla_communication_whatsapp.record_meta_delivery_status", conflict,
    )
    await route._handle_message_status(
        {"id": "wamid.synthetic", "status": "delivered", "recipient_id": "966555000000"},
        receipt_context=object(), phone_number_id="phone-one",
    )
    assert not db.committed and db.rolled_back and db.closed
