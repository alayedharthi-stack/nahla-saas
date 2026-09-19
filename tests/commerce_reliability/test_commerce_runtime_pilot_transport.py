"""What the WhatsApp transport reported, translated for the ledger (no network).

The ledger decides what a send established; this boundary only has to report
what the provider actually said. These cases prove it never turns a refusal,
a timeout or a missing message id into an acceptance.
"""
from __future__ import annotations

import datetime
import threading
from typing import Any, List, Optional, Tuple

import pytest

from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import runtime_entry as entry


def transport_for(answer: Tuple[str, Optional[str], Optional[int]], *, seen: List[Any] = None):
    def send(recipient: str, text: str):
        if seen is not None:
            seen.append((recipient, text))
        return answer
    return entry.whatsapp_text_transport(send, recipient="+966500000001")


def test_an_identified_acceptance_is_the_only_shape_that_becomes_accepted():
    transport = transport_for(("ok", "wamid.OK", 200))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.ACCEPTED and pmid == "wamid.OK"


def test_a_success_without_a_message_id_is_unknown_not_accepted():
    transport = transport_for(("ok", None, 200))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.UNKNOWN and pmid is None


@pytest.mark.parametrize("classification, status, expected", [
    ("blocked", 400, lc.ReceiptKind.REJECTED),
    ("provider_error_field", 403, lc.ReceiptKind.REJECTED),
    ("exception", 500, lc.ReceiptKind.UNKNOWN),
    ("throttled", 429, lc.ReceiptKind.REJECTED),
])
def test_a_refusal_keeps_the_status_the_provider_gave_it(classification, status, expected):
    transport = transport_for((classification, None, status))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == expected and pmid is None


def test_no_status_at_all_is_unknown_and_never_a_proven_rejection():
    transport = transport_for(("timeout", None, None))
    response = transport({"text": "مرحبا"})
    assert response.timed_out is True and response.http_status is None
    kind, _ = lc.classify_send_response(response)
    assert kind == lc.ReceiptKind.UNKNOWN


def test_a_message_id_without_an_ok_classification_is_not_an_acceptance():
    transport = transport_for(("provider_error_field", "wamid.PARTIAL", 400))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.REJECTED and pmid is None


def test_the_transport_is_handed_the_reserved_text_and_the_trusted_recipient():
    seen: List[Any] = []
    transport = transport_for(("ok", "wamid.OK", 200), seen=seen)
    transport({"text": "النص المحجوز", "evidence_refs": []})
    assert seen == [("+966500000001", "النص المحجوز")]


def test_a_reserved_payload_without_text_still_reaches_the_transport_as_empty():
    seen: List[Any] = []
    transport = transport_for(("ok", "wamid.OK", 200), seen=seen)
    transport({"evidence_refs": []})
    assert seen == [("+966500000001", "")]


def test_the_pilot_bounds_how_long_it_waits_for_one_send():
    from services import commerce_runtime_pilot as pilot

    assert 0 < pilot.SEND_WAIT_SECONDS <= 120


# ── The schema probe's cache ─────────────────────────────────────────────────


class _FakeUrl:
    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeEngine:
    def __init__(self, url: str) -> None:
        self.url = _FakeUrl(url)
        self.connects = 0

    def connect(self):
        self.connects += 1
        raise RuntimeError("no database here")


def test_the_schema_probe_is_remembered_per_database_not_per_object(monkeypatch):
    entry.reset_schema_probe()
    first = _FakeEngine("postgresql://u:***@db-a/nahla")
    assert entry.runtime_schema_available(first) is False
    assert entry.runtime_schema_available(first) is False
    assert first.connects == 1                      # probed once, then remembered

    same_database = _FakeEngine("postgresql://u:***@db-a/nahla")
    assert entry.runtime_schema_available(same_database) is False
    assert same_database.connects == 0              # a new object, the same database

    other = _FakeEngine("postgresql://u:***@db-b/nahla")
    assert entry.runtime_schema_available(other) is False
    assert other.connects == 1                      # a different database is probed
    entry.reset_schema_probe()


def test_an_engine_that_cannot_name_itself_is_probed_every_time():
    entry.reset_schema_probe()

    class Nameless(_FakeEngine):
        @property
        def url(self):
            raise RuntimeError("no url")

        @url.setter
        def url(self, value):
            pass

    engine = Nameless("x")
    assert entry.runtime_schema_available(engine) is False
    assert entry.runtime_schema_available(engine) is False
    assert engine.connects == 1                     # keyed by identity as a last resort
    entry.reset_schema_probe()


# ── Complete schema readiness (F9) ───────────────────────────────────────────


def test_the_runtime_requires_every_relation_it_actually_uses():
    from core.commerce_runtime import models as m
    from core.commerce_runtime.repositories import LEDGER_RELATIONS

    assert len(entry.REQUIRED_RELATIONS) == 9
    assert set(entry.REQUIRED_RELATIONS) == set(LEDGER_RELATIONS) | {
        m.CONVERSATIONS_TABLE, m.TURNS_TABLE, m.TERMINALS_TABLE}


class _SchemaEngine:
    """An engine that reports a chosen subset of the relations as present."""

    def __init__(self, present) -> None:
        self.url = _FakeUrl("postgresql://u:***@db-schema-" + str(abs(hash(tuple(present)))) + "/n")
        self._present = set(present)
        self.probes = 0

    def connect(self):
        engine = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, _stmt, params):
                engine.probes += 1
                name = str(params["t"]).split(".")[-1]
                return _Scalar(name in engine._present)

        return _Conn()


class _Scalar:
    def __init__(self, value) -> None:
        self._value = value

    def scalar(self):
        return self._value


@pytest.mark.parametrize("present, available", [
    (entry.REQUIRED_RELATIONS, True),
    ((), False),
    (entry.REQUIRED_RELATIONS[:3], False),          # foundation only
    (entry.REQUIRED_RELATIONS[3:], False),          # ledgers only
    (entry.REQUIRED_RELATIONS[:8], False),          # one missing
])
def test_a_partial_schema_is_never_available(present, available):
    entry.reset_schema_probe()
    assert entry.runtime_schema_available(_SchemaEngine(present)) is available
    entry.reset_schema_probe()


def test_a_database_missing_only_the_terminals_table_is_refused():
    """The shape the previous check passed: turns and ledgers present, terminals absent."""
    entry.reset_schema_probe()
    present = [n for n in entry.REQUIRED_RELATIONS if n != "commerce_runtime_turn_terminals"]
    assert entry.runtime_schema_available(_SchemaEngine(present)) is False
    entry.reset_schema_probe()


# ── Abandoned tool session ownership (F8) ────────────────────────────────────


class _Session:
    """A session that says whether it was closed, when, and how often."""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self.closed.set()


def _binding() -> Any:
    from core.commerce_runtime import agent_live_tools as alt
    from core.commerce_runtime import conversation_link as cl

    return alt.LiveToolBinding(
        context=object(),
        link=cl.TrustedConversationLink(tenant_id=7, namespace="live", channel="wa",
                                        app_conversation_id=41, runtime_conversation_id=9,
                                        conversation_ref="wa:v1:conv:41"),
    )


def test_a_turn_whose_tools_all_returned_closes_its_session_on_its_own_thread():
    binding, session = _binding(), _Session()
    binding.enter()
    binding.leave()
    assert entry._retire_tool_session(binding, session, turn_id=1) is None
    assert session.closed.is_set()


def test_a_session_an_abandoned_call_may_still_hold_is_not_closed_under_it():
    binding, session = _binding(), _Session()
    binding.enter()                                   # a call that never comes back
    binding.abandon("the tool did not answer within 1.0s")
    thread = entry._retire_tool_session(binding, session, turn_id=2, reap_seconds=5.0)
    assert thread is not None
    assert not session.closed.wait(0.3)               # still held, so still open
    binding.leave()                                   # the abandoned call finally returns
    assert session.closed.wait(5.0)                   # and only then is it closed
    thread.join(5.0)
    assert not thread.is_alive()


def test_a_call_that_never_returns_leaves_the_session_alone_rather_than_breaking_it():
    binding, session = _binding(), _Session()
    binding.enter()
    binding.abandon("the tool did not answer within 1.0s")
    thread = entry._retire_tool_session(binding, session, turn_id=3, reap_seconds=0.2)
    thread.join(5.0)
    assert not thread.is_alive()
    assert not session.closed.is_set()


def test_the_session_is_still_closed_after_the_reaper_has_given_up_waiting():
    """The reaper is a timely closer, not the owner. Its deadline passing must
    not leave the session with nobody responsible for it."""
    binding, session = _binding(), _Session()
    binding.enter()
    binding.abandon("the tool did not answer within 1.0s")
    thread = entry._retire_tool_session(binding, session, turn_id=5, reap_seconds=0.2)
    thread.join(5.0)
    assert not thread.is_alive()                 # the bounded wait is over
    assert not session.closed.is_set()           # and the call still holds it

    binding.leave()                              # the abandoned call finally ends
    assert session.closed.wait(5.0)              # and closing it is still someone's job


def test_a_session_is_closed_exactly_once_however_the_two_owners_race():
    binding, session = _binding(), _Session()
    binding.enter()
    binding.abandon("timeout")
    thread = entry._retire_tool_session(binding, session, turn_id=6, reap_seconds=5.0)
    binding.leave()
    assert session.closed.wait(5.0)
    thread.join(5.0)
    assert session.closes == 1


def test_a_broken_close_never_takes_down_the_thread_that_finished_the_call():
    class _Angry(_Session):
        def close(self) -> None:
            super().close()
            raise RuntimeError("the pool is gone")

    binding, session = _binding(), _Angry()
    binding.enter()
    binding.abandon("timeout")
    entry._retire_tool_session(binding, session, turn_id=7, reap_seconds=0.1)
    binding.leave()                              # must not raise
    assert session.closed.wait(5.0)


def test_the_reaper_runs_as_a_daemon_so_it_can_never_hold_the_process_open():
    binding, session = _binding(), _Session()
    binding.enter()
    binding.abandon("timeout")
    thread = entry._retire_tool_session(binding, session, turn_id=4, reap_seconds=0.1)
    assert thread.daemon is True
    thread.join(5.0)


# ── A refused redispatch establishes nothing it cannot read (F10) ────────────


class _BlockedLedgers:
    """Refuses every reservation, and answers about receipts as configured."""

    def __init__(self, receipts: Any) -> None:
        self._receipts = receipts

    def reserve_delivery_dispatch(self, **kwargs: Any) -> Any:
        from core.commerce_runtime import ledger_contracts as lc_

        sequence = type("Seq", (), {"sequence_id": kwargs["sequence_id"]})()
        raise lc_.DeliveryDispatchBlocked(lc_.DispatchBlock.ALREADY_CONFIRMED, sequence)

    def list_delivery_receipts(self, **kwargs: Any) -> Any:
        if isinstance(self._receipts, BaseException):
            raise self._receipts
        return list(self._receipts)


def _dispatch_against(ledgers: Any) -> Any:
    from core.commerce_runtime import contracts as c_
    from core.commerce_runtime import delivery_dispatch as dd_

    token = c_.OwnershipToken(owner_id="o", fence=1, epoch=1, tenant_id=1,
                              namespace="live", conversation_id=1)
    return dd_.dispatch_reserved_delivery(
        ledgers=ledgers, tenant_id=1, namespace="live", conversation_id=1, token=token,
        sequence_id=1, transport=lambda payload: None, recorded_by="test")


def test_receipts_that_cannot_be_read_establish_nothing():
    from core.commerce_runtime import delivery_dispatch as dd_

    outcome = _dispatch_against(_BlockedLedgers(RuntimeError("database unavailable")))
    assert outcome.status == dd_.NOT_ATTEMPTED and outcome.reused_outcome is False


def test_a_reservation_with_no_established_outcome_is_still_not_attempted():
    from core.commerce_runtime import delivery_dispatch as dd_

    outcome = _dispatch_against(_BlockedLedgers([]))
    assert outcome.status == dd_.NOT_ATTEMPTED and outcome.provider_message_id is None


def _receipt(kind: str, *, wamid: Any = None, attempt: int = 1, receipt_id: int = 1) -> Any:
    from core.commerce_runtime import ledger_contracts as lc_

    return lc_.DeliveryReceiptRecord(
        receipt_id=receipt_id, attempt_id=attempt, sequence_id=1, receipt_no=1, kind=kind,
        provider_message_id=wamid, evidence={}, recorded_by="t",
        recorded_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))


def test_an_accepted_receipt_without_a_message_id_is_not_reused_as_an_acceptance():
    from core.commerce_runtime import delivery_dispatch as dd_
    from core.commerce_runtime import ledger_contracts as lc_

    outcome = _dispatch_against(_BlockedLedgers([_receipt(lc_.ReceiptKind.ACCEPTED.value)]))
    assert outcome.status == dd_.SENT_ACCEPTED and outcome.delivered is False
    assert outcome.provider_message_id is None


def test_an_unknown_receipt_outranks_a_rejection_on_the_same_reservation():
    """Unknown is the one outcome a later call must not resolve either way."""
    from core.commerce_runtime import delivery_dispatch as dd_
    from core.commerce_runtime import ledger_contracts as lc_

    outcome = _dispatch_against(_BlockedLedgers([
        _receipt(lc_.ReceiptKind.REJECTED.value, attempt=1, receipt_id=1),
        _receipt(lc_.ReceiptKind.UNKNOWN.value, attempt=2, receipt_id=2),
    ]))
    assert outcome.status == dd_.SENT_UNKNOWN and outcome.reused_outcome is True


def test_an_acceptance_outranks_a_later_failure_receipt():
    """A failure after acceptance is reach evidence about a message that was sent."""
    from core.commerce_runtime import delivery_dispatch as dd_
    from core.commerce_runtime import ledger_contracts as lc_

    outcome = _dispatch_against(_BlockedLedgers([
        _receipt(lc_.ReceiptKind.ACCEPTED.value, wamid="wamid.X", receipt_id=1),
        _receipt(lc_.ReceiptKind.FAILED.value, attempt=1, receipt_id=2),
    ]))
    assert outcome.status == dd_.SENT_ACCEPTED and outcome.provider_message_id == "wamid.X"
