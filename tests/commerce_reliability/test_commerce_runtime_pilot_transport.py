"""What the WhatsApp transport reported, translated for the ledger (no network).

The ledger decides what a send established; this boundary only has to report
what the provider actually said. These cases prove it never turns a refusal,
a timeout or a missing message id into an acceptance.
"""
from __future__ import annotations

import datetime
import threading
import time
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
    """All twelve, and exactly the twelve the runtime reads or writes.

    The handover three are in the set because the runtime cannot decide whether
    to admit a turn without the barrier, and cannot keep an acknowledgement
    without the deferred table. A database holding the first nine would admit
    turns it has nowhere to record acceptance for.
    """
    from core.commerce_runtime import handover_models as hm
    from core.commerce_runtime import models as m
    from core.commerce_runtime.repositories import LEDGER_RELATIONS

    assert len(entry.REQUIRED_RELATIONS) == 12
    assert set(entry.REQUIRED_RELATIONS) == (
        set(LEDGER_RELATIONS)
        | {m.CONVERSATIONS_TABLE, m.TURNS_TABLE, m.TERMINALS_TABLE}
        | set(hm.HANDOVER_TABLES)
    )
    assert set(entry.HANDOVER_RELATIONS) == set(hm.HANDOVER_TABLES)


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


# ── The cleanup claim is arbitration, not a check then a set (F8, re-review) ──


class _CheckThenSetCloser:
    """The arbitration this code used before: an Event, read then set.

    Reproduced here exactly so the regression can show the defect it is about,
    rather than asserting the corrected behaviour against nothing.
    """

    def __init__(self, session: Any, *, between: Any) -> None:
        self._session = session
        self._closed = threading.Event()
        self._between = between

    def close(self, _source: str) -> bool:
        if self._closed.is_set():                   # read …
            return False
        self._between()                             # … and the window in between
        self._closed.set()                          # … then write
        self._session.close()
        return True


class _Interleaved:
    """A lock whose critical section both threads try to enter together.

    Entering waits at a two-party barrier. With real mutual exclusion the second
    thread cannot arrive — the first holds the lock and does not yield inside it
    — so the barrier times out, and that timeout is the proof. Without mutual
    exclusion both threads enter and the damaging interleaving is forced rather
    than hoped for.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.barrier = threading.Barrier(2)
        self.both_entered = False

    def __enter__(self) -> "_Interleaved":
        self._inner.__enter__()
        try:
            self.barrier.wait(timeout=0.5)
            self.both_entered = True
        except threading.BrokenBarrierError:
            pass
        return self

    def __exit__(self, *exc: Any) -> bool:
        return bool(self._inner.__exit__(*exc))


def _race(closer: Any) -> List[bool]:
    """Two threads, released together, both closing the same session."""
    won: List[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def contend(name: str) -> None:
        start.wait(5.0)
        outcome = closer.close(name)
        with lock:
            won.append(outcome)

    threads = [threading.Thread(target=contend, args=(name,)) for name in ("idle", "reaper")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
        assert not thread.is_alive()
    return won


def test_check_then_set_arbitration_really_does_close_the_session_twice():
    """The harness is sensitive: the previous arbitration loses this race."""
    session = _Session()
    window = threading.Barrier(2)

    def both_have_read() -> None:
        # Deterministic: neither thread writes until both have read.
        try:
            window.wait(timeout=2.0)
        except threading.BrokenBarrierError:        # pragma: no cover - would be a hang
            pass

    won = _race(_CheckThenSetCloser(session, between=both_have_read))
    assert won == [True, True]                      # both believed they had won
    assert session.closes == 2                      # and the session was closed twice


def test_the_corrected_claim_admits_exactly_one_closer_under_the_same_interleaving():
    session = _Session()
    interleaved = _Interleaved(threading.Lock())
    won = _race(entry.ExclusiveCloser(session, turn_id=99, lock=interleaved))
    # The second thread could not reach the critical section while the first held
    # it, so the barrier never completed — that is the mutual exclusion.
    assert interleaved.both_entered is False
    assert sorted(won) == [False, True]
    assert session.closes == 1


def test_a_claim_is_won_once_however_many_times_it_is_asked():
    closer = entry.ExclusiveCloser(_Session(), turn_id=1)
    assert [closer.claim() for _ in range(5)] == [True, False, False, False, False]
    assert closer.claimed is True


def test_the_loser_is_never_blocked_by_a_winner_that_hangs():
    """Closing happens outside the claim, so a hanging close blocks nobody."""
    released = threading.Event()

    class _Hanging(_Session):
        def close(self) -> None:
            super().close()
            released.wait(5.0)

    closer = entry.ExclusiveCloser(_Hanging(), turn_id=2)
    winner = threading.Thread(target=lambda: closer.close("winner"))
    winner.start()
    try:
        for _ in range(200):                        # wait until the winner is inside close()
            if closer.claimed:
                break
            time.sleep(0.01)
        assert closer.close("loser") is False       # returns at once, does not block
    finally:
        released.set()
        winner.join(10.0)
    assert not winner.is_alive()


# ── The card sender's receipt ────────────────────────────────────────────────
#
# A sender that cannot report *what the provider said* is not the same sender
# as the two beside it, whatever it shares with them. `_send_cta_url` took no
# result sink and forwarded none to `_post_wa`, so the card factory's own sink
# stayed empty however the send went: an accepted card came back as
# ("ok", None, None) and the ledger, correctly refusing to invent a receipt it
# never saw, recorded a delivery Meta had already taken as unknown. These run
# the real `_send_cta_url` — stubbing it would prove nothing about the seam
# that was broken — and only the provider call underneath it.


import asyncio  # noqa: E402
import contextlib  # noqa: E402
from unittest.mock import patch  # noqa: E402


@contextlib.contextmanager
def _loop_on_another_thread():
    """The pilot schedules its sends onto a loop it does not run on."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _card_answer(post_wa):
    """Drive the real factory the way the pilot does, with one send underneath."""
    from routers import whatsapp_webhook as wh
    from services import commerce_runtime_pilot as pilot

    wire = pilot.WireObservation()
    with _loop_on_another_thread() as loop, patch.object(wh, "_post_wa", post_wa):
        send_card = pilot._send_card_factory("PHONE_ID", 1, None, loop, wire)
        return send_card(
            "+966500000001",
            "القميص القطني الأزرق متوفر بمقاس L.",
            "https://cdn.example.com/shirt.jpg",
            "https://shop.example.com/products/shirt",
            "اطلبه الآن",
        )


def _accepting_provider(wamid: str = "wamid.CARD", status: int = 200):
    calls = []

    async def post_wa(phone_id, payload, _tenant_id=None, _db=None, _result_sink=None,
                      **kwargs):
        calls.append(payload)
        if _result_sink is not None:
            _result_sink.update({
                "classification": "ok",
                "wamid": wamid,
                "http_status": status,
                "duplicate_suppressed": False,
                "response_body": {"messages": [{"id": wamid}]},
            })
        return True

    return post_wa, calls


def test_an_accepted_card_carries_the_provider_receipt_back_to_the_pilot():
    post_wa, calls = _accepting_provider()
    assert _card_answer(post_wa) == ("ok", "wamid.CARD", 200)
    # The one send really was the card, not a text fallback beside it.
    assert len(calls) == 1
    assert calls[0]["interactive"]["type"] == "cta_url"
    assert calls[0]["interactive"]["header"]["image"]["link"].startswith("https://")


def test_the_runtime_records_an_accepted_card_as_accepted_not_unknown():
    """The seam end to end: provider 200 + wamid must reach the ledger as ACCEPTED."""
    post_wa, _calls = _accepting_provider()
    answer = _card_answer(post_wa)

    transport = entry.whatsapp_reply_transport(
        lambda recipient, text: ("ok", "wamid.TEXT", 200),
        lambda recipient, text, rows, button: ("ok", "wamid.LIST", 200),
        recipient="+966500000001",
        send_card=lambda recipient, text, image_url, button_url, button_label: answer,
    )
    response = transport({
        "text": "القميص القطني الأزرق متوفر بمقاس L.",
        "card": {
            "image_url": "https://cdn.example.com/shirt.jpg",
            "button_url": "https://shop.example.com/products/shirt",
            "button_label": "اطلبه الآن",
        },
    })
    kind, pmid = lc.classify_send_response(response)
    assert kind == lc.ReceiptKind.ACCEPTED and pmid == "wamid.CARD"
    assert response.timed_out is False


def test_a_dead_connection_on_a_card_stays_unknown_and_is_never_a_rejection():
    """The provider may have taken it before the socket died: never auto-resend."""
    async def post_wa(phone_id, payload, _tenant_id=None, _db=None, _result_sink=None,
                      **kwargs):
        if _result_sink is not None:
            _result_sink.update({
                "classification": "exception",
                "wamid": None,
                "http_status": None,
                "duplicate_suppressed": False,
                "error_text": "ReadTimeout: provider did not answer",
            })
        return False

    classification, wamid, status = _card_answer(post_wa)
    assert (classification, wamid, status) == ("exception", None, None)
    kind, pmid = lc.classify_send_response(entry._sent(classification, wamid, status))
    assert kind == lc.ReceiptKind.UNKNOWN and pmid is None


def test_a_card_send_that_reports_nothing_at_all_is_unknown_not_accepted():
    """No receipt is no proof. The old behaviour on *every* card, now only here."""
    async def post_wa(phone_id, payload, _tenant_id=None, _db=None, _result_sink=None,
                      **kwargs):
        return True

    classification, wamid, status = _card_answer(post_wa)
    assert (classification, wamid, status) == ("ok", None, None)
    kind, pmid = lc.classify_send_response(entry._sent(classification, wamid, status))
    assert kind == lc.ReceiptKind.UNKNOWN and pmid is None


def test_a_refused_card_keeps_the_status_the_provider_gave_it():
    async def post_wa(phone_id, payload, _tenant_id=None, _db=None, _result_sink=None,
                      **kwargs):
        if _result_sink is not None:
            _result_sink.update({
                "classification": "non_2xx",
                "wamid": None,
                "http_status": 400,
                "duplicate_suppressed": False,
            })
        return False

    classification, wamid, status = _card_answer(post_wa)
    assert (classification, wamid, status) == ("non_2xx", None, 400)
    kind, _pmid = lc.classify_send_response(entry._sent(classification, wamid, status))
    assert kind == lc.ReceiptKind.REJECTED


def test_a_deduplicated_card_is_still_an_identified_acceptance():
    """The send path answers a repeat with the prior wamid; that is a receipt."""
    async def post_wa(phone_id, payload, _tenant_id=None, _db=None, _result_sink=None,
                      **kwargs):
        if _result_sink is not None:
            _result_sink.update({
                "classification": "ok",
                "wamid": "wamid.PRIOR",
                "http_status": 200,
                "duplicate_suppressed": True,
            })
        return True

    assert _card_answer(post_wa) == ("ok", "wamid.PRIOR", 200)


# ── Why a turn was text-only ─────────────────────────────────────────────────
#
# Production turns 24 and 25 (tenant 1, conversation 9, 2026-09-23 18:30) each
# reported `delivery_kind=text, choice_rows=0` and nothing else. That is the
# outcome, not the reason: a model that never asked for a selector and one that
# asked and had it withheld — an unobserved product, a missing photo, a
# plain-http link — produce byte-identical summaries. The loop decides both and
# records them; these prove they reach the turn report, so the next live round
# is self-diagnosing instead of needing a transcript.


def _report_for(events):
    """A TurnReport built the way `_after_loop` builds one, from loop events."""
    import dataclasses

    from core.commerce_runtime import agent_contracts as ac

    made = [ac.LoopEvent(step_no=i, kind=kind, detail=detail)
            for i, (kind, detail) in enumerate(events, start=1)]
    accepted = next((e for e in reversed(made) if e.kind == "reply_accepted"), None)
    detail = dict(getattr(accepted, "detail", None) or {})
    return dataclasses.replace(
        entry.TurnReport(reason="handled", tenant_id=1, conversation_id=9),
        choices_outcome=str(detail.get("choices") or "") or None,
        card_outcome=str(detail.get("card") or "") or None,
    )


def test_a_text_only_turn_says_the_model_never_asked_for_either_shape():
    report = _report_for([("reply_accepted", {"kind": "text", "choices": "not_requested",
                                              "card": "not_requested"})])
    assert report.choices_outcome == "not_requested"
    assert report.card_outcome == "not_requested"
    fields = report.as_log_fields()
    assert fields["choices_outcome"] == "not_requested" and fields["card_outcome"] == "not_requested"


def test_a_withheld_card_is_told_apart_from_a_card_never_asked_for():
    """The distinction the production summaries could not make."""
    from core.commerce_runtime import reply_card as rcard

    asked_and_withheld = _report_for([("reply_accepted", {"kind": "text", "choices": "not_requested",
                                                          "card": rcard.NO_IMAGE})])
    never_asked = _report_for([("reply_accepted", {"kind": "text", "choices": "not_requested",
                                                   "card": rcard.NOT_REQUESTED})])
    assert asked_and_withheld.choice_rows == never_asked.choice_rows == 0
    assert asked_and_withheld.card_outcome == rcard.NO_IMAGE
    assert never_asked.card_outcome == rcard.NOT_REQUESTED
    assert asked_and_withheld.card_outcome != never_asked.card_outcome


def test_each_card_withhold_reason_survives_to_the_log_line():
    from core.commerce_runtime import reply_card as rcard

    for reason in (rcard.NOT_OBSERVED, rcard.NO_IMAGE, rcard.NO_LINK,
                   rcard.INSECURE_LINK, rcard.NO_LABEL, rcard.SELECTOR_PREFERRED):
        report = _report_for([("reply_accepted", {"kind": "text", "choices": "not_requested",
                                                  "card": reason})])
        assert report.as_log_fields()["card_outcome"] == reason


def test_a_turn_with_no_accepted_reply_claims_no_reason_at_all():
    """A loop that never accepted a reply has nothing to say about its shape,
    and says nothing rather than reporting a reason it did not reach."""
    report = _report_for([("verification_failed", {"problems": ["card_without_evidence"]})])
    assert report.choices_outcome is None and report.card_outcome is None
