"""The trial report reads what the runtime actually wrote — on real PostgreSQL.

The judgement rules are proven offline in ``tests/test_commerce_runtime_trial_evidence``.
What cannot be proven there is the part that matters most in production: a
query that names a column or a join wrongly returns *nothing*, and a report
over nothing reads as ``not_observed`` — honest-sounding, and false, because
the rows were there.

So the turns here are written by the runtime's own repositories, not by hand,
and the reader has to find exactly them. Deferred inbounds and effects are
seeded directly: they are read by one ``SELECT`` each against columns this
module would fail to insert into if they drifted.

Merchant-agnostic by construction: the reader sees tenant ids, references and
outcome words, never a catalogue, a product or a phrase.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Iterator, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import contracts as c
from core.commerce_runtime import conversation_link as cl
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.ledgers import LedgerRepository
from scripts.operators import commerce_runtime_trial_evidence as job
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
)

REVISION = "0111"
LIVE = c.Namespace.LIVE
CHANNEL = "wa:connection-trial"
WORKER = "worker-trial"


@pytest.fixture(scope="module")
def database(pg_admin_dsn: str) -> Iterator[Any]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, REVISION)
        yield engine
    finally:
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


class Trial:
    """One merchant's trial, written through the runtime's own repositories."""

    def __init__(self, engine: Any, tenant_id: int) -> None:
        self.engine = engine
        self.tenant_id = tenant_id
        self.repo = LedgerRepository(engine)
        self.session = sessionmaker(bind=engine, expire_on_commit=False)
        self._conversations = 0

    def admit(self, *, message_id: str = "") -> Tuple[Any, Any]:
        self._conversations += 1
        ref = cl.conversation_ref_for(channel="whatsapp", app_conversation_id=self._conversations)
        turn = self.repo.foundation.admit_turn(
            tenant_id=self.tenant_id, namespace=LIVE, conversation_ref=ref,
            channel_connection_ref=CHANNEL,
            provider_message_id=message_id or f"wamid.{uuid.uuid4().hex[:18]}")
        lease = self.repo.foundation.claim(
            tenant_id=self.tenant_id, namespace=LIVE, conversation_id=turn.conversation_id,
            owner_id=WORKER, lease_seconds=120)
        return turn, lease

    def answer(self, turn: Any, lease: Any, *, receipt: str = "accepted",
               provider_message_id: str = "") -> Any:
        sequence = self.repo.reserve_delivery(
            tenant_id=self.tenant_id, namespace=LIVE, conversation_id=turn.conversation_id,
            token=lease.token, turn_id=turn.turn_id,
            intent=lc.DeliveryIntent(kind="text", payload={"body": "…"}))
        attempt = self.repo.reserve_delivery_dispatch(
            tenant_id=self.tenant_id, namespace=LIVE, conversation_id=turn.conversation_id,
            token=lease.token, sequence_id=sequence.sequence_id)
        self.repo.record_delivery_receipt(
            tenant_id=self.tenant_id, namespace=LIVE, conversation_id=turn.conversation_id,
            attempt_id=attempt.attempt_id, kind=receipt,
            provider_message_id=(provider_message_id or f"wamid.OUT{uuid.uuid4().hex[:14]}")
            if receipt == "accepted" else None,
            recorded_by=WORKER)
        return sequence

    def finish(self, turn: Any, lease: Any, *, processing: str = "completed") -> Any:
        """The ledger-derived terminal — the only one a turn with ledger rows may have.

        Transport outcome and customer reach are not supplied here: the
        runtime derives them from what the delivery ledger recorded, which is
        exactly the relationship this report later reads back.
        """
        return self.repo.finalize_turn(
            tenant_id=self.tenant_id, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome=processing)

    def answered(self, *, message_id: str = "") -> Any:
        turn, lease = self.admit(message_id=message_id)
        self.answer(turn, lease)
        self.finish(turn, lease)
        return turn

    def read(self, *, hours: int = 1):
        now = dt.datetime.now(dt.timezone.utc)
        with self.session() as db:
            return job.read_window(db, tenant_id=self.tenant_id,
                                   since=now - dt.timedelta(hours=hours),
                                   until=now + dt.timedelta(hours=hours))


@pytest.fixture()
def trial(database: Any) -> Trial:
    """A merchant of this case's own, so every row read belongs to this case."""
    with database.begin() as conn:
        tenant_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {uuid.uuid4().hex[:8]}"}).scalar_one())
    return Trial(database, tenant_id)


# ── The reader finds what the runtime wrote ──────────────────────────────────

def test_the_reader_finds_the_turn_the_runtime_admitted_and_answered(trial: Trial) -> None:
    turn = trial.answered(message_id="wamid.INBOUND0001")

    turns, effects, deferred = trial.read()

    assert [t["turn_id"] for t in turns] == [turn.turn_id]
    row = turns[0]
    assert row["provider_message_id"] == "wamid.INBOUND0001"
    assert row["conversation_ref"] == cl.conversation_ref_for(
        channel="whatsapp", app_conversation_id=1)
    assert row["terminal"]["processing_outcome"] == "completed"
    assert row["terminal"]["transport_outcome"] == "accepted"
    assert row["terminal"]["customer_reach"] == "unknown"
    assert len(row["sequences"]) == 1
    attempts = row["sequences"][0]["attempts"]
    assert len(attempts) == 1
    assert [r["kind"] for r in attempts[0]["receipts"]] == ["accepted"]
    assert effects == 0 and deferred == []


def test_the_report_over_those_rows_refuses_nothing_and_masks_the_identifiers(trial: Trial) -> None:
    trial.answered(message_id="wamid.INBOUND0002")
    turns, effects, deferred = trial.read()

    found = job.verdicts(turns, effects_reserved=effects, deferred=deferred)
    assert not job.refused_claims(found)
    assert found["every_admitted_turn_reached_a_terminal"]["verdict"] == job.PROVEN
    assert found["at_most_one_accepted_send_per_reply_intent"]["verdict"] == job.PROVEN
    # Acceptance is reported as acceptance; nothing claims the customer was reached.
    assert found["customer_reach_is_never_claimed_without_a_receipt"]["verdict"] == job.NOT_OBSERVED

    lines = job.render(trial.tenant_id, [job.turn_report(t) for t in turns], found)
    assert not any("wamid.INBOUND0002" in line for line in lines)
    assert any("completed/accepted/reach=unknown" in line for line in lines)


def test_a_turn_left_open_is_read_as_open_and_refuses_the_claim(trial: Trial) -> None:
    answered = trial.answered()
    open_turn, _lease = trial.admit()

    turns, effects, deferred = trial.read()

    by_id = {t["turn_id"]: t for t in turns}
    assert by_id[answered.turn_id]["terminal"] is not None
    assert by_id[open_turn.turn_id]["terminal"] is None
    entry = job.verdicts(turns, effects_reserved=effects,
                         deferred=deferred)["every_admitted_turn_reached_a_terminal"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [open_turn.turn_id]


def test_an_unknown_send_is_read_as_unknown_and_never_as_an_answer(trial: Trial) -> None:
    turn, lease = trial.admit()
    trial.answer(turn, lease, receipt="unknown")
    trial.finish(turn, lease, processing="failed")

    turns, effects, deferred = trial.read()

    assert turns[0]["terminal"]["transport_outcome"] == "unknown"
    report = job.turn_report(turns[0])
    assert report["accepted_sends"] == 0 and report["receipt_kinds"] == ["unknown"]
    found = job.verdicts(turns, effects_reserved=effects, deferred=deferred)
    assert found["no_unknown_send_was_reported_completed"]["verdict"] == job.PROVEN
    assert not job.refused_claims(found)


# ── Scope: one merchant, one window ──────────────────────────────────────────

def test_another_merchants_turn_is_never_in_this_merchants_report(database: Any,
                                                                  trial: Trial) -> None:
    with database.begin() as conn:
        other_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {uuid.uuid4().hex[:8]}"}).scalar_one())
    other = Trial(database, other_id)
    mine = trial.answered()
    theirs = other.answered()

    turns, _effects, _deferred = trial.read()

    assert [t["turn_id"] for t in turns] == [mine.turn_id]
    assert theirs.turn_id not in {t["turn_id"] for t in turns}


def test_a_turn_outside_the_stated_window_is_not_in_the_report(trial: Trial) -> None:
    trial.answered()
    now = dt.datetime.now(dt.timezone.utc)

    with trial.session() as db:
        turns, _effects, _deferred = job.read_window(
            db, tenant_id=trial.tenant_id,
            since=now - dt.timedelta(days=2), until=now - dt.timedelta(days=1))

    assert turns == []
    # And a window that saw nothing claims nothing.
    found = job.verdicts(turns, effects_reserved=0, deferred=[])
    assert found["every_admitted_turn_reached_a_terminal"]["verdict"] == job.NOT_OBSERVED


def test_a_window_larger_than_a_trial_is_refused_rather_than_truncated(trial: Trial,
                                                                       monkeypatch) -> None:
    monkeypatch.setattr(job, "MAX_TURNS_INSPECTED", 1)
    trial.answered()
    trial.answered()

    with pytest.raises(ValueError, match="window_exceeds_trial_size"):
        trial.read()


# ── The two tables read by one statement each ────────────────────────────────

def test_a_deferred_inbound_is_read_with_its_disposition(trial: Trial) -> None:
    with trial.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO commerce_runtime_deferred_inbound
                (tenant_id, namespace, channel_connection_ref, phone_number_id, recipient,
                 provider_message_id, payload, reason, state)
            VALUES (:t, 'live', :ch, '1231731153362611', '966500000001',
                    :pmid, '{}'::jsonb, 'drain_buffered', 'pending')
        """), {"t": trial.tenant_id, "ch": CHANNEL, "pmid": f"wamid.{uuid.uuid4().hex[:18]}"})

    _turns, _effects, deferred = trial.read()

    assert len(deferred) == 1 and deferred[0]["disposition"] is None
    entry = job.verdicts([], effects_reserved=0,
                         deferred=deferred)["every_deferred_inbound_is_accounted_for"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [deferred[0]["id"]]


def test_a_reserved_commerce_effect_is_counted_and_refuses_the_claim(trial: Trial) -> None:
    turn, lease = trial.admit()
    trial.repo.reserve_effect(
        tenant_id=trial.tenant_id, namespace=LIVE, conversation_id=turn.conversation_id,
        token=lease.token, turn_id=turn.turn_id,
        intent=lc.EffectIntent(action_type="order_cancel", idempotency_key=uuid.uuid4().hex,
                               payload={"order": "SO-1"}))

    _turns, effects, deferred = trial.read()

    assert effects == 1
    entry = job.verdicts([], effects_reserved=effects,
                         deferred=deferred)["no_commerce_write_was_reserved"]
    assert entry["verdict"] == job.REFUSED


def test_runtime_resolved_inbound_needs_no_operator_disposition(trial: Trial) -> None:
    # Database-shaped reader control, not proof of the upstream handling
    # validator. resolve_inbound uses this state with disposition left NULL.
    with trial.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO commerce_runtime_deferred_inbound
                (tenant_id, namespace, channel_connection_ref, phone_number_id, recipient,
                 provider_message_id, payload, reason, state, resolved_at)
            VALUES (:t, 'live', :ch, 'phone-trial', '966500000001',
                    :pmid, '{}'::jsonb, 'accepted', 'resolved', clock_timestamp())
        """), {"t": trial.tenant_id, "ch": CHANNEL,
                "pmid": f"wamid.{uuid.uuid4().hex[:18]}"})

    _turns, _effects, deferred = trial.read()
    assert len(deferred) == 1
    assert deferred[0]["state"] == "resolved"
    assert deferred[0]["disposition"] is None
    entry = job.verdicts([], deferred=deferred)["every_deferred_inbound_is_accounted_for"]
    assert entry["verdict"] == job.PROVEN


# ── The report writes nothing ────────────────────────────────────────────────

def test_shadow_work_cannot_contaminate_the_live_trial(trial: Trial) -> None:
    live = trial.answered()
    shadow = trial.repo.foundation.admit_turn(
        tenant_id=trial.tenant_id, namespace=c.Namespace.SHADOW,
        conversation_ref=f"shadow-trial-{uuid.uuid4().hex}",
        channel_connection_ref=CHANNEL, provider_message_id=f"wamid.{uuid.uuid4().hex}")
    owner = trial.repo.foundation.claim(
        tenant_id=trial.tenant_id, namespace=c.Namespace.SHADOW,
        conversation_id=shadow.conversation_id, owner_id=WORKER, lease_seconds=120)
    trial.repo.reserve_effect(
        tenant_id=trial.tenant_id, namespace=c.Namespace.SHADOW,
        conversation_id=shadow.conversation_id, token=owner.token, turn_id=shadow.turn_id,
        intent=lc.EffectIntent(action_type="order_cancel", idempotency_key=uuid.uuid4().hex,
                               payload={"order": "synthetic-shadow-order"}))
    with trial.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO commerce_runtime_deferred_inbound
                (tenant_id, namespace, channel_connection_ref, phone_number_id, recipient,
                 provider_message_id, payload, reason, state)
            VALUES (:t, 'shadow', :ch, 'phone-trial', '966500000001',
                    :pmid, '{}'::jsonb, 'accepted', 'pending')
        """), {"t": trial.tenant_id, "ch": CHANNEL,
                "pmid": f"wamid.{uuid.uuid4().hex[:18]}"})
    turns, effects, deferred = trial.read()
    assert [row["turn_id"] for row in turns] == [live.turn_id]
    assert effects == 0
    assert deferred == []


def test_report_snapshot_survives_completion_on_an_independent_connection(trial: Trial) -> None:
    turn, lease = trial.admit()
    now = dt.datetime.now(dt.timezone.utc)
    bounds = {"tenant_id": trial.tenant_id, "since": now - dt.timedelta(hours=1),
              "until": now + dt.timedelta(hours=1)}
    with trial.session() as db:
        assert job._read_only(db) is True
        assert db.execute(text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
        before, _, _ = job.read_window(db, **bounds)
        assert before[0]["turn_id"] == turn.turn_id
        assert before[0]["terminal"] is None

        # The repository uses its own engine transaction while the reporting
        # session retains its independent, already-established snapshot.
        trial.answer(turn, lease)
        trial.finish(turn, lease)
        during, _, _ = job.read_window(db, **bounds)
        assert during == before
        db.rollback()

    after, _, _ = trial.read()
    assert after[0]["terminal"]["transport_outcome"] == "accepted"
    assert len(after[0]["sequences"]) == 1


def test_the_report_leaves_the_database_exactly_as_it_found_it(trial: Trial) -> None:
    turn = trial.answered()

    def counts() -> dict:
        with trial.engine.begin() as conn:
            return {table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
                    for table in ("commerce_runtime_turns", "commerce_runtime_turn_terminals",
                                  "commerce_runtime_delivery_sequences",
                                  "commerce_runtime_delivery_attempts",
                                  "commerce_runtime_delivery_receipts")}

    before = counts()
    with trial.session() as db:
        assert job._read_only(db) is True
        assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        now = dt.datetime.now(dt.timezone.utc)
        job.read_window(db, tenant_id=trial.tenant_id, since=now - dt.timedelta(hours=1),
                        until=now + dt.timedelta(hours=1))
        db.rollback()

    assert counts() == before
    assert turn.turn_id  # the turn that was read is still the one that was written
