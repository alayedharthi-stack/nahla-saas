"""A store number shared with the merchant's own WhatsApp Business app reaches the model as data.

Meta coexistence keeps the merchant's WhatsApp Business app on the same number
the store answers from, so people who know the merchant personally write to it
too. The platform already records that state on the connection
(``services.meta_coexistence.is_coexistence_mode``); these tests prove the
runtime hands that fact to the model beside the turn, from the connection's own
state and for its own tenant only. Same seam as the assistant name and reply
style: real SQL read and provider serialization, recording inference, no sends.
These tests prove what the model is *given*, never what it writes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

from models import Base, WhatsAppConnection
from services import commerce_runtime_pilot as seam

from test_commerce_runtime_assistant_identity import run_input, sessions  # noqa: F401  (fixtures)

COEXISTENCE = {"connection_mode": "coexistence", "smb_sync": "completed"}


@pytest.fixture
def connections(sessions):  # noqa: F811
    table = WhatsAppConnection.__table__
    swapped = [(column, column.type) for column in table.columns if isinstance(column.type, JSONB)]
    for column, _ in swapped:
        column.type = JSON()
    try:
        Base.metadata.create_all(sessions.kw["bind"], tables=[table])
    finally:
        for column, original in swapped:
            column.type = original

    def connect(tenant_id, *, connection_type="embedded", metadata=None):
        with sessions() as db:
            db.add(WhatsAppConnection(
                tenant_id=tenant_id, status="connected", provider="meta",
                connection_type=connection_type, phone_number_id=f"pn-{tenant_id}",
                extra_metadata=metadata))
            db.commit()

    return connect


def test_a_coexistence_number_is_handed_to_the_model_as_data(
        sessions, run_input, connections):  # noqa: F811
    connections(701, metadata=COEXISTENCE)
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts[seam.SHARED_NUMBER_KEY] is True
    # Beside the turn, never written into the instructions.
    assert seam.SHARED_NUMBER_KEY not in seam._instructions()


@pytest.mark.parametrize("connection_type,metadata", [
    ("embedded", None),
    ("embedded", {}),
    ("direct", {"connection_mode": "cloud_api"}),
    ("embedded", {"connection_mode": "coexistence_pending"}),
    # The provider says the number left the Business app: a stale mode is not the fact.
    ("embedded", {"connection_mode": "coexistence", "is_on_biz_app": False}),
    # The retired column value alone is not how the platform records coexistence.
    ("coexistence", None),
])
def test_a_number_the_platform_does_not_record_as_shared_carries_no_such_fact(
        sessions, run_input, connections, connection_type, metadata):  # noqa: F811
    connections(701, connection_type=connection_type, metadata=metadata)
    with sessions() as db:
        facts = run_input(db, 701)
    assert seam.SHARED_NUMBER_KEY not in facts


def test_a_store_without_a_connection_row_carries_no_such_fact(
        sessions, run_input, connections):  # noqa: F811
    with sessions() as db:
        facts = run_input(db, 701)
    assert seam.SHARED_NUMBER_KEY not in facts


def test_another_tenants_shared_number_never_reaches_this_store(
        sessions, run_input, connections):  # noqa: F811
    connections(701, metadata=None)
    connections(702, metadata=COEXISTENCE)
    with sessions() as db:
        own, other = run_input(db, 701), run_input(db, 702)
    assert seam.SHARED_NUMBER_KEY not in own
    assert other[seam.SHARED_NUMBER_KEY] is True


def test_an_unreadable_connection_leaves_the_fact_out_and_the_turn_still_goes(
        sessions, run_input, connections, monkeypatch):  # noqa: F811
    connections(701, metadata=COEXISTENCE)
    with sessions() as db:
        real_query = db.query

        def unreadable(*args, **kwargs):
            if args and "WhatsAppConnection" in str(args[0]):
                raise RuntimeError("synthetic connection read failure")
            return real_query(*args, **kwargs)
        monkeypatch.setattr(db, "query", unreadable)
        facts = run_input(db, 701)
    assert seam.SHARED_NUMBER_KEY not in facts
    assert facts["channel"] == "whatsapp" and "reply_language" in facts
