"""The owner pilot end to end, on real PostgreSQL, with no model and no network.

Each case runs the real ``run_commerce_runtime_turn`` — real admission, real
ownership, the real agent loop, the real Anthropic *adapter*, the real trusted
read context over the merchant's own rows, the real delivery ledger — against a
disposable database migrated with the repository's own chain to ``0109``. Only
two things are doubles: the Anthropic HTTP call (a scripted
``call_single_step``) and the WhatsApp transport (a scripted ``SendResponse``).

What these prove is the part that must not be wrong in production: one inbound
message is answered at most once, a send that failed or is unknown is never
recorded as success, an uncertain send is never blindly retried, and a turn
another tenant did not admit is invisible to it. They establish no model
quality and no WhatsApp delivery.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips, and that skip is reported, never counted.
"""
from __future__ import annotations

import contextlib
import dataclasses
import threading
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import contracts as c
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import runtime_entry as entry
from core.commerce_runtime.agent_loop import AgentLoop
from core.commerce_runtime.ledgers import LedgerRepository
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
)

REVISION = "0109"
PHONE = "+966500000001"
QUESTION = "عندكم حذاء رياضي أبيض؟"
PRODUCT_TITLE = "حذاء رياضي أبيض"


# ── Doubles ──────────────────────────────────────────────────────────────────


class ScriptedAnthropic:
    """A stand-in for the HTTP call only. The real adapter still runs above it."""

    def __init__(self, answers: List[Dict[str, Any]]) -> None:
        self._answers = list(answers)
        self.calls: List[Dict[str, Any]] = []

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if not self._answers:
            return {"provider": "anthropic", "model": "scripted", "status": "sdk_error",
                    "stop_reason": None, "blocks": [], "usage": None, "error": "script_exhausted"}
        return self._answers.pop(0)


def step(blocks: List[Dict[str, Any]], *, stop_reason: str = "tool_use") -> Dict[str, Any]:
    return {"provider": "anthropic", "model": "scripted-model", "status": "ok",
            "stop_reason": stop_reason, "blocks": blocks,
            "usage": {"input_tokens": 100, "output_tokens": 20}, "request_id": "req"}


def tool_use(call_id: str, name: str, **arguments: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def reply(text_body: str, *, refs: Tuple[str, ...] = (), commerce: bool = False,
          call_id: str = "r1") -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": ap.REPLY_TOOL_NAME,
            "input": {"text": text_body, "evidence_refs": list(refs),
                      "claims_commerce_facts": commerce}}


@dataclasses.dataclass
class Transport:
    """A scripted WhatsApp transport that records every call it received."""

    responses: List[Any]
    sent: List[Mapping] = dataclasses.field(default_factory=list)  # type: ignore[type-arg]

    def __call__(self, payload: Any) -> lc.SendResponse:
        self.sent.append(dict(payload))
        if not self.responses:
            raise AssertionError("the runtime dispatched more sends than the test scripted")
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def accepted(message_id: str = "wamid.ACCEPTED") -> lc.SendResponse:
    return lc.SendResponse(http_status=200, body={"messages": [{"id": message_id}]})


def rejected() -> lc.SendResponse:
    return lc.SendResponse(http_status=400, body={"error": {"code": "131026"}})


def timed_out() -> lc.SendResponse:
    return lc.SendResponse(http_status=None, body={}, timed_out=True)


# ── Harness ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Reservation:
    turn_id: int
    sequence_id: int


@dataclasses.dataclass
class Pilot:
    engine: Any
    session_factory: Any
    ledgers: LedgerRepository
    tenant_a: int
    tenant_b: int
    connection_id: int
    conversation_id: int
    customer_id: int
    product_id: int

    def run(self, *, answers: List[Dict[str, Any]], transport: Transport,
            provider_message_id: Optional[str] = None, tenant: Optional[int] = None,
            question: str = QUESTION,
            budget: Optional[ac.LoopBudget] = None) -> entry.TurnReport:
        return entry.run_commerce_runtime_turn(
            engine=self.engine,
            session_factory=self.session_factory,
            tenant_id=tenant or self.tenant_a,
            conversation_id=self.conversation_id,
            conversation_ref=f"wa:{PHONE}:{self.conversation_id}",
            connection_ref=f"wa:{self.connection_id}",
            connection_id=str(self.connection_id),
            customer_id=self.customer_id,
            normalized_customer_phone=PHONE,
            provider_message_id=provider_message_id or ("wamid." + uuid.uuid4().hex),
            inbound_text=question,
            inbound_metadata={"source": "test"},
            transport=transport,
            instructions="EXISTING-INSTRUCTIONS",
            budget=budget or ac.LoopBudget(max_steps=3, max_tool_calls=4, tool_timeout_seconds=10.0,
                                           provider_timeout_seconds=15.0, deadline_seconds=45.0),
            anthropic_provider=ScriptedAnthropic(answers),
        )

    def receipts(self, turn_id: int, *, tenant: Optional[int] = None) -> List[Any]:
        sequence = self.ledgers.get_delivery_sequence(
            tenant_id=tenant or self.tenant_a, namespace=entry.NAMESPACE, turn_id=turn_id)
        if sequence is None:
            return []
        return list(self.ledgers.list_delivery_receipts(
            tenant_id=tenant or self.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=sequence.conversation_id, sequence_id=sequence.sequence_id))

    def attempts(self, turn_id: int) -> List[Any]:
        sequence = self.ledgers.get_delivery_sequence(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE, turn_id=turn_id)
        if sequence is None:
            return []
        return list(self.ledgers.list_delivery_attempts(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=sequence.conversation_id, sequence_id=sequence.sequence_id))

    def terminal(self, turn_id: int, *, tenant: Optional[int] = None) -> Any:
        return self.ledgers.foundation.get_terminal(
            tenant_id=tenant or self.tenant_a, namespace=entry.NAMESPACE, turn_id=turn_id)

    @contextlib.contextmanager
    def owned(self, *, turn_id: Optional[int] = None, owner: str = "pilot-test") -> Any:
        """Hold a real lease for the block, and give it back afterwards."""
        lease = self.ledgers.foundation.claim(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=self.conversation_id, owner_id=owner, lease_seconds=60, turn_id=turn_id)
        token = c.OwnershipToken(owner_id=owner, fence=lease.fence, epoch=lease.epoch,
                                 tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
                                 conversation_id=self.conversation_id)
        try:
            yield token
        finally:
            try:
                self.ledgers.foundation.release(
                    tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
                    conversation_id=self.conversation_id, token=token)
            except c.CommerceRuntimeError:
                pass

    def reserve_reply(self, body: str) -> Any:
        """Admit a turn and reserve one delivery intent, leaving the turn open."""
        from core.commerce_runtime import agent_scripted as sp
        from tests.commerce_reliability.agent_fixture_catalog import build_registry

        admitted = self.ledgers.foundation.admit_turn(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_ref=f"wa:{PHONE}:{self.conversation_id}",
            channel_connection_ref=f"wa:{self.connection_id}",
            provider_message_id="wamid." + uuid.uuid4().hex, payload={"text": QUESTION})
        with self.owned(turn_id=admitted.turn_id, owner="reserver") as token:
            outcome = AgentLoop(self.ledgers, build_registry()).run_turn(
                tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
                conversation_id=admitted.conversation_id, turn_id=admitted.turn_id, token=token,
                provider=sp.ScriptedReasoningProvider([sp.reply(body)]))
        assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
        return Reservation(turn_id=admitted.turn_id, sequence_id=outcome.delivery_sequence_id)


def _seed(engine, *, label: str) -> int:
    with engine.begin() as conn:
        return int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {label} {uuid.uuid4().hex[:8]}"},
        ).scalar_one())


@pytest.fixture(scope="module")
def pilot(pg_admin_dsn: str) -> Any:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, REVISION)
        tenant_a = _seed(engine, label="أ")
        tenant_b = _seed(engine, label="ب")
        with engine.begin() as conn:
            connection_id = int(conn.execute(
                text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                     "VALUES (:t, :p, 'connected') RETURNING id"),
                {"t": tenant_a, "p": "1555000111"},
            ).scalar_one())
            customer_id = int(conn.execute(
                text("INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                     "VALUES (:t, :p, :p, :n) RETURNING id"),
                {"t": tenant_a, "p": PHONE, "n": "نورة عبدالله"},
            ).scalar_one())
            conversation_id = int(conn.execute(
                text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                     "VALUES (:t, :c, :e, 'active') RETURNING id"),
                {"t": tenant_a, "c": customer_id, "e": PHONE},
            ).scalar_one())
            product_id = int(conn.execute(
                text("INSERT INTO products (tenant_id, external_id, title, description, price, "
                     "in_stock, stock_quantity) VALUES (:t, :x, :ti, :d, 199, true, 4) RETURNING id"),
                {"t": tenant_a, "x": "SKU-" + uuid.uuid4().hex[:8], "ti": PRODUCT_TITLE,
                 "d": "حذاء رياضي قطني"},
            ).scalar_one())
        yield Pilot(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                    ledgers=LedgerRepository(engine), tenant_a=tenant_a, tenant_b=tenant_b,
                    connection_id=connection_id, conversation_id=conversation_id,
                    customer_id=customer_id, product_id=product_id)
    finally:
        entry.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


@pytest.fixture(autouse=True)
def _fresh_probe() -> Any:
    entry.reset_schema_probe()
    yield
    entry.reset_schema_probe()


# ── The answered turn ────────────────────────────────────────────────────────


def test_an_owner_turn_reasons_dispatches_and_completes_on_an_identified_send(pilot):
    transport = Transport([accepted("wamid.OK1")])
    report = pilot.run(answers=[step([reply("تمام، جاري التحقق", call_id="r1")])], transport=transport)

    assert report.reason == entry.HANDLED
    assert report.loop_status == ac.LoopStatus.PENDING_DELIVERY.value
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert report.provider_message_id == "wamid.OK1"
    assert report.replied is True
    assert report.processing_outcome == c.ProcessingOutcome.COMPLETED.value
    assert report.transport_outcome == c.TransportOutcome.ACCEPTED.value
    assert report.customer_reach == c.CustomerReach.UNKNOWN.value   # acceptance is not reach
    assert len(transport.sent) == 1


def test_the_text_that_is_sent_is_the_text_the_ledger_reserved(pilot):
    transport = Transport([accepted("wamid.OK2")])
    report = pilot.run(answers=[step([reply("النص المرسل", call_id="r1")])], transport=transport)
    sequence = pilot.ledgers.get_delivery_sequence(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE, turn_id=report.turn_id)
    assert sequence is not None
    assert sequence.intent_payload["text"] == "النص المرسل"
    assert transport.sent[0]["text"] == "النص المرسل"
    assert report.reply_text == "النص المرسل"


def test_the_real_read_tools_reach_the_merchant_s_own_catalogue(pilot):
    ref = f"catalog:product:{pilot.product_id}"
    transport = Transport([accepted("wamid.OK3")])
    report = pilot.run(
        answers=[
            step([tool_use("t1", "catalog_search", query="حذاء")]),
            step([reply("متوفر", refs=(ref,), commerce=True, call_id="r1")]),
        ],
        transport=transport,
    )
    assert report.tools_called == ("catalog_search",)
    assert ref in report.evidence_refs
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert report.input_tokens == 200 and report.output_tokens == 40


def test_a_draft_citing_evidence_that_was_never_observed_is_refused_before_any_send(pilot):
    transport = Transport([])
    report = pilot.run(
        answers=[
            step([reply("السعر ٩٩", refs=("catalog:product:999999",), commerce=True, call_id="r1")]),
            step([reply("السعر ٩٩", refs=("catalog:product:999999",), commerce=True, call_id="r2")]),
            step([reply("السعر ٩٩", refs=("catalog:product:999999",), commerce=True, call_id="r3")]),
        ],
        transport=transport,
    )
    assert report.loop_status == ac.LoopStatus.STOPPED.value
    assert report.stop_reason == ac.StopReason.VERIFICATION_FAILED.value
    assert transport.sent == []
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert report.transport_outcome == c.TransportOutcome.NOT_ATTEMPTED.value


# ── One reply per inbound message ────────────────────────────────────────────


def test_a_redelivered_inbound_message_never_answers_twice(pilot):
    pmid = "wamid." + uuid.uuid4().hex
    first_transport = Transport([accepted("wamid.FIRST")])
    first = pilot.run(answers=[step([reply("الأولى", call_id="r1")])],
                      transport=first_transport, provider_message_id=pmid)
    second_transport = Transport([])
    second = pilot.run(answers=[step([reply("الثانية", call_id="r1")])],
                       transport=second_transport, provider_message_id=pmid)

    assert first.dispatch_status == dd.SENT_ACCEPTED and len(first_transport.sent) == 1
    assert second.turn_id == first.turn_id and second.duplicate_inbound is True
    assert second.reason == entry.ALREADY_TERMINAL
    assert second_transport.sent == []
    assert len(pilot.attempts(first.turn_id)) == 1


def test_re_entry_reuses_the_reserved_intent_instead_of_composing_a_second_reply(pilot):
    """A turn whose reply was reserved but not dispatched is resumed, not redone."""
    pmid = "wamid." + uuid.uuid4().hex
    admitted = pilot.ledgers.foundation.admit_turn(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        conversation_ref=f"wa:{PHONE}:{pilot.conversation_id}",
        channel_connection_ref=f"wa:{pilot.connection_id}", provider_message_id=pmid,
        payload={"text": QUESTION, "metadata": {}})
    lease = pilot.ledgers.foundation.claim(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        conversation_id=admitted.conversation_id, owner_id="pre-reserver", lease_seconds=60,
        turn_id=admitted.turn_id)
    token = c.OwnershipToken(owner_id="pre-reserver", fence=lease.fence, epoch=lease.epoch,
                             tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
                             conversation_id=admitted.conversation_id)
    from tests.commerce_reliability.agent_fixture_catalog import build_registry

    loop = AgentLoop(pilot.ledgers, build_registry())
    from core.commerce_runtime import agent_scripted as sp

    reserved = loop.run_turn(tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
                             conversation_id=admitted.conversation_id, turn_id=admitted.turn_id,
                             token=token, provider=sp.ScriptedReasoningProvider([sp.reply("المحجوزة")]))
    assert reserved.status == ac.LoopStatus.PENDING_DELIVERY.value
    pilot.ledgers.foundation.release(tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
                                     conversation_id=admitted.conversation_id, token=token)

    transport = Transport([accepted("wamid.RESUMED")])
    report = pilot.run(answers=[step([reply("نص مختلف تمامًا", call_id="r1")])],
                       transport=transport, provider_message_id=pmid)

    assert report.turn_id == admitted.turn_id and report.duplicate_inbound is True
    assert report.reused_delivery is True
    assert report.delivery_sequence_id == reserved.delivery_sequence_id
    assert transport.sent[0]["text"] == "المحجوزة"       # the reserved reply, not a new one
    assert len(transport.sent) == 1
    assert report.processing_outcome == c.ProcessingOutcome.COMPLETED.value


def test_two_concurrent_invocations_of_one_turn_send_exactly_once(pilot):
    pmid = "wamid." + uuid.uuid4().hex
    lock = threading.Lock()
    sends: List[Any] = []

    def transport_for(label: str) -> Any:
        def send(payload: Any) -> lc.SendResponse:
            with lock:
                sends.append(label)
            return accepted(f"wamid.{label}")
        return send

    reports: Dict[str, Any] = {}
    barrier = threading.Barrier(2)

    def worker(label: str) -> None:
        barrier.wait(10)
        reports[label] = entry.run_commerce_runtime_turn(
            engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
            conversation_id=pilot.conversation_id,
            conversation_ref=f"wa:{PHONE}:{pilot.conversation_id}",
            connection_ref=f"wa:{pilot.connection_id}", connection_id=str(pilot.connection_id),
            customer_id=pilot.customer_id, normalized_customer_phone=PHONE,
            provider_message_id=pmid, inbound_text=QUESTION, inbound_metadata={},
            transport=transport_for(label), instructions="EXISTING-INSTRUCTIONS",
            budget=ac.LoopBudget(max_steps=2, max_tool_calls=2, tool_timeout_seconds=5.0,
                                 provider_timeout_seconds=10.0, deadline_seconds=40.0),
            anthropic_provider=ScriptedAnthropic([step([reply(f"رد {label}", call_id="r1")])]),
        )

    threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    turn_ids = {r.turn_id for r in reports.values()}
    assert len(turn_ids) == 1                      # one admission, one turn
    assert len(sends) == 1                         # one customer message, whoever won
    accepted_reports = [r for r in reports.values() if r.dispatch_status == dd.SENT_ACCEPTED]
    assert len(accepted_reports) == 1
    turn_id = accepted_reports[0].turn_id
    assert len(pilot.attempts(turn_id)) == 1


# ── Never a success the send does not support ────────────────────────────────


def test_a_rejected_send_is_recorded_as_a_failed_turn_that_reached_nobody(pilot):
    transport = Transport([rejected()])
    report = pilot.run(answers=[step([reply("مرفوضة", call_id="r1")])], transport=transport)
    assert report.dispatch_status == dd.SENT_REJECTED
    assert report.replied is False
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert report.transport_outcome == c.TransportOutcome.REJECTED_DEFINITIVE.value
    assert report.customer_reach == c.CustomerReach.NOT_REACHED.value
    kinds = [r.kind for r in pilot.receipts(report.turn_id)]
    assert kinds == [lc.ReceiptKind.REJECTED.value]


def test_an_unknown_send_is_never_success_and_is_never_blindly_retried(pilot):
    transport = Transport([timed_out()])
    report = pilot.run(answers=[step([reply("غير مؤكدة", call_id="r1")])], transport=transport)
    assert report.dispatch_status == dd.SENT_UNKNOWN and report.provider_message_id is None
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert report.transport_outcome == c.TransportOutcome.UNKNOWN.value
    assert report.customer_reach == c.CustomerReach.UNKNOWN.value

    # The finished turn is not dispatchable again at all: it is no longer the
    # eligible turn, so the reservation cannot be re-sent even by its owner.
    again = Transport([accepted("wamid.SHOULD-NOT-HAPPEN")])
    with pilot.owned() as token:
        blocked = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.conversation_id, token=token,
            sequence_id=report.delivery_sequence_id, transport=again, recorded_by="retrier")
    assert blocked.status == dd.NOT_ATTEMPTED
    assert blocked.blocked_reason == c.RejectReason.TURN_NOT_ELIGIBLE.value
    assert again.sent == []


def test_an_unknown_send_blocks_a_second_dispatch_of_the_same_live_reservation(pilot):
    """Before the turn is finished, an unknown outcome still refuses a retry."""
    reservation = pilot.reserve_reply("غير مؤكدة حية")
    first = Transport([timed_out()])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        one = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=first, recorded_by="pilot")
    assert one.status == dd.SENT_UNKNOWN and len(first.sent) == 1

    again = Transport([accepted("wamid.SHOULD-NOT-HAPPEN")])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        two = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=again, recorded_by="pilot")
    assert two.status == dd.NOT_ATTEMPTED
    assert two.blocked_reason == lc.DispatchBlock.OUTCOME_UNKNOWN.value
    assert again.sent == []

    # And the turn is finished honestly: failed, unknown, unknown.
    with pilot.owned(turn_id=reservation.turn_id) as token:
        terminal = dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=c.ProcessingOutcome.FAILED.value)
    assert terminal is not None
    assert terminal.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert terminal.transport_outcome == c.TransportOutcome.UNKNOWN.value
    assert terminal.customer_reach == c.CustomerReach.UNKNOWN.value


def test_a_transport_that_raises_is_unknown_rather_than_a_proven_rejection(pilot):
    transport = Transport([RuntimeError("connection reset")])
    report = pilot.run(answers=[step([reply("انقطاع", call_id="r1")])], transport=transport)
    assert report.dispatch_status == dd.SENT_UNKNOWN
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert report.transport_outcome == c.TransportOutcome.UNKNOWN.value
    kinds = [r.kind for r in pilot.receipts(report.turn_id)]
    assert kinds == [lc.ReceiptKind.UNKNOWN.value]


def test_a_provider_failure_sends_nothing_and_is_not_dressed_as_an_answer(pilot):
    transport = Transport([])
    report = pilot.run(
        answers=[{"provider": "anthropic", "model": "m", "status": "overloaded", "stop_reason": None,
                  "blocks": [], "usage": None, "error": None}],
        transport=transport,
    )
    assert report.loop_status == ac.LoopStatus.STOPPED.value
    assert report.stop_reason == ac.StopReason.PROVIDER_FAILURE.value
    assert transport.sent == []
    assert report.delivery_sequence_id is None
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value
    assert report.transport_outcome == c.TransportOutcome.NOT_ATTEMPTED.value
    assert report.customer_reach == c.CustomerReach.NOT_APPLICABLE.value


def test_a_model_refusal_sends_nothing_and_says_why(pilot):
    transport = Transport([])
    report = pilot.run(answers=[step([], stop_reason="refusal")], transport=transport)
    assert report.stop_reason == ac.StopReason.PROVIDER_BLOCKED.value
    assert transport.sent == []
    assert report.processing_outcome == c.ProcessingOutcome.FAILED.value


# ── Scope ────────────────────────────────────────────────────────────────────


def test_a_turn_admitted_for_one_tenant_is_invisible_to_another(pilot):
    transport = Transport([accepted("wamid.ISO")])
    report = pilot.run(answers=[step([reply("رد", call_id="r1")])], transport=transport)
    assert pilot.terminal(report.turn_id) is not None
    assert pilot.terminal(report.turn_id, tenant=pilot.tenant_b) is None
    assert pilot.ledgers.get_delivery_sequence(
        tenant_id=pilot.tenant_b, namespace=entry.NAMESPACE, turn_id=report.turn_id) is None


def test_a_conversation_outside_the_tenant_yields_no_trusted_context_and_no_send(pilot):
    transport = Transport([])
    report = pilot.run(answers=[step([reply("رد", call_id="r1")])], transport=transport,
                       tenant=pilot.tenant_b)
    assert report.reason == entry.CONTEXT_UNAVAILABLE
    assert transport.sent == []
    assert report.delivery_sequence_id is None


def test_the_runtime_refuses_when_its_schema_is_not_present(pg_admin_dsn):
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        entry.reset_schema_probe()
        assert entry.runtime_schema_available(engine) is False
        transport = Transport([])
        report = entry.run_commerce_runtime_turn(
            engine=engine, session_factory=sessionmaker(bind=engine), tenant_id=1,
            conversation_id=1, conversation_ref="wa:x:1", connection_ref="wa:1", connection_id="1",
            customer_id=None, normalized_customer_phone=PHONE,
            provider_message_id="wamid." + uuid.uuid4().hex, inbound_text=QUESTION,
            inbound_metadata=None, transport=transport, instructions="EXISTING-INSTRUCTIONS",
        )
        assert report.reason == entry.SCHEMA_UNAVAILABLE
        assert transport.sent == []
    finally:
        entry.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)
