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
import json
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import contracts as c
from core.commerce_runtime import conversation_link as cl
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

REVISION = "0111"
PHONE = "+966500000001"
QUESTION = "عندكم حذاء رياضي أبيض؟"
PRODUCT_TITLE = "حذاء رياضي أبيض"
# The pilot's model is an owner configuration decision. These cases pass an
# explicit placeholder so what is proved is the threading, never a choice.
MODEL = "model-configured-for-this-pilot"
# A number no customer row carries, so it names conversations only through
# ``external_id`` and establishes no association at all.
UNLINKED_PHONE = "+966500007777"


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
            question: str = QUESTION, model: str = MODEL,
            budget: Optional[ac.LoopBudget] = None) -> entry.TurnReport:
        return entry.run_commerce_runtime_turn(
            engine=self.engine,
            session_factory=self.session_factory,
            tenant_id=tenant or self.tenant_a,
            conversation_id=self.conversation_id,
            connection_ref=f"wa:{self.connection_id}",
            connection_id=str(self.connection_id),
            customer_id=self.customer_id,
            normalized_customer_phone=PHONE,
            provider_message_id=provider_message_id or ("wamid." + uuid.uuid4().hex),
            inbound_text=question,
            inbound_metadata={"source": "test"},
            transport=transport,
            instructions="EXISTING-INSTRUCTIONS",
            model=model,
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

    @property
    def runtime_conversation_id(self) -> int:
        """The commerce runtime's own conversation id — never the application's."""
        return self.ledgers.foundation.get_conversation(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_ref=cl.conversation_ref_for(channel=entry.CHANNEL,
                                                     app_conversation_id=self.conversation_id),
        ).conversation_id

    def terminal(self, turn_id: int, *, tenant: Optional[int] = None) -> Any:
        return self.ledgers.foundation.get_terminal(
            tenant_id=tenant or self.tenant_a, namespace=entry.NAMESPACE, turn_id=turn_id)

    @contextlib.contextmanager
    def owned(self, *, turn_id: Optional[int] = None, owner: str = "pilot-test") -> Any:
        """Hold a real lease for the block, and give it back afterwards."""
        lease = self.ledgers.foundation.claim(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=self.runtime_conversation_id, owner_id=owner, lease_seconds=60,
            turn_id=turn_id)
        token = c.OwnershipToken(owner_id=owner, fence=lease.fence, epoch=lease.epoch,
                                 tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
                                 conversation_id=self.runtime_conversation_id)
        try:
            yield token
        finally:
            try:
                self.ledgers.foundation.release(
                    tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
                    conversation_id=self.runtime_conversation_id, token=token)
            except c.CommerceRuntimeError:
                pass

    def reserve_reply(self, body: str) -> Any:
        """Admit a turn and reserve one delivery intent, leaving the turn open."""
        from core.commerce_runtime import agent_scripted as sp
        from tests.commerce_reliability.agent_fixture_catalog import build_registry

        admitted = self.ledgers.foundation.admit_turn(
            tenant_id=self.tenant_a, namespace=entry.NAMESPACE,
            conversation_ref=cl.conversation_ref_for(
                channel=entry.CHANNEL, app_conversation_id=self.conversation_id),
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
            # Push the application conversation id away from 1 so it cannot
            # coincide with the runtime id by accident.
            for _filler in range(3):
                conn.execute(
                    text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                         "VALUES (:t, NULL, :e, 'active')"),
                    {"t": tenant_b, "e": "+96650000999" + str(_filler)},
                )
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
        ledgers = LedgerRepository(engine)
        # And push the runtime conversation sequence past it, from the other
        # direction, by admitting unrelated conversations first. Every case below
        # therefore runs with two different identifiers, which is the production
        # shape and the one a same-value fixture silently hides.
        for filler in range(9001, 9008):
            ledgers.foundation.admit_turn(
                tenant_id=tenant_a, namespace=entry.NAMESPACE,
                conversation_ref=cl.conversation_ref_for(channel=entry.CHANNEL,
                                                         app_conversation_id=filler),
                channel_connection_ref=f"wa:{connection_id}",
                provider_message_id="wamid.filler." + uuid.uuid4().hex, payload={"text": "x"})
        yield Pilot(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                    ledgers=ledgers, tenant_a=tenant_a, tenant_b=tenant_b,
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


# ── Trusted conversation identity (F1) ───────────────────────────────────────


def _runtime_conversation_id(pilot, app_conversation_id: int) -> int:
    return pilot.ledgers.foundation.get_conversation(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        conversation_ref=cl.conversation_ref_for(channel=entry.CHANNEL,
                                                 app_conversation_id=app_conversation_id),
    ).conversation_id


def test_the_two_conversation_identifiers_really_are_different_here(pilot):
    """Guards the guard: a fixture where they coincide proves nothing."""
    transport = Transport([accepted("wamid.IDS")])
    report = pilot.run(answers=[step([reply("رد", call_id="r1")])], transport=transport)
    assert report.dispatch_status == dd.SENT_ACCEPTED
    runtime_id = _runtime_conversation_id(pilot, pilot.conversation_id)
    assert runtime_id != pilot.conversation_id, (runtime_id, pilot.conversation_id)


def test_the_real_tools_run_when_the_identifiers_differ(pilot):
    """The defect this replaces refused every tool call whenever they differed."""
    assert _runtime_conversation_id(pilot, pilot.conversation_id) != pilot.conversation_id
    ref = f"catalog:product:{pilot.product_id}"
    transport = Transport([accepted("wamid.TOOLS")])
    report = pilot.run(
        answers=[step([tool_use("t1", "search_products", query="حذاء")]),
                 step([reply("متوفر", refs=(ref,), commerce=True, call_id="r1")])],
        transport=transport,
    )
    assert report.tools_called == ("search_products",)
    assert ref in report.evidence_refs          # the tool ran; it was not scope-refused
    assert report.dispatch_status == dd.SENT_ACCEPTED


def test_the_link_is_established_by_reading_the_runtime_row(pilot):
    link = cl.verify_conversation_link(
        pilot.ledgers.foundation, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        channel=entry.CHANNEL, app_conversation_id=pilot.conversation_id,
        runtime_conversation_id=_runtime_conversation_id(pilot, pilot.conversation_id))
    assert link.app_conversation_id == pilot.conversation_id
    assert link.runtime_conversation_id != link.app_conversation_id
    assert link.conversation_ref.endswith(f":conv:{pilot.conversation_id}")


def test_a_runtime_conversation_belonging_to_another_application_conversation_is_refused(pilot):
    """The negative control: a real runtime row, the wrong application owner."""
    other = _runtime_conversation_id(pilot, 9001)          # a filler conversation
    with pytest.raises(cl.ConversationLinkUnverified) as raised:
        cl.verify_conversation_link(
            pilot.ledgers.foundation, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            channel=entry.CHANNEL, app_conversation_id=pilot.conversation_id,
            runtime_conversation_id=other)
    assert raised.value.reason == "conversation_ref_mismatch"


def test_a_runtime_conversation_in_another_tenant_is_refused(pilot):
    mine = _runtime_conversation_id(pilot, pilot.conversation_id)
    with pytest.raises(cl.ConversationLinkUnverified) as raised:
        cl.verify_conversation_link(
            pilot.ledgers.foundation, tenant_id=pilot.tenant_b, namespace=entry.NAMESPACE,
            channel=entry.CHANNEL, app_conversation_id=pilot.conversation_id,
            runtime_conversation_id=mine)
    assert raised.value.reason == "runtime_conversation_not_found"


def test_a_conversation_reference_this_runtime_did_not_mint_is_refused(pilot):
    admitted = pilot.ledgers.foundation.admit_turn(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        conversation_ref="legacy-shaped-ref-7",
        channel_connection_ref=f"wa:{pilot.connection_id}",
        provider_message_id="wamid." + uuid.uuid4().hex, payload={"text": "x"})
    with pytest.raises(cl.ConversationLinkUnverified) as raised:
        cl.verify_conversation_link(
            pilot.ledgers.foundation, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            channel=entry.CHANNEL, app_conversation_id=7,
            runtime_conversation_id=admitted.conversation_id)
    assert raised.value.reason == "conversation_ref_unrecognised"


def test_the_binding_refuses_a_scope_carrying_the_application_identifier(pilot):
    """Passing the application id where the runtime id belongs is not acceptable."""
    from core.commerce_runtime import agent_live_tools as alt
    from core.commerce_runtime import agent_tools as at

    link = cl.verify_conversation_link(
        pilot.ledgers.foundation, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        channel=entry.CHANNEL, app_conversation_id=pilot.conversation_id,
        runtime_conversation_id=_runtime_conversation_id(pilot, pilot.conversation_id))
    binding = alt.LiveToolBinding(context=object(), link=link)
    binding.check(at.ToolScope(tenant_id=link.tenant_id, namespace=link.namespace,
                               conversation_id=link.runtime_conversation_id, turn_id=1))
    for wrong in (link.app_conversation_id, link.runtime_conversation_id + 1):
        with pytest.raises(ac.ToolError):
            binding.check(at.ToolScope(tenant_id=link.tenant_id, namespace=link.namespace,
                                       conversation_id=wrong, turn_id=1))
    with pytest.raises(ac.ToolError):
        binding.check(at.ToolScope(tenant_id=link.tenant_id + 1, namespace=link.namespace,
                                   conversation_id=link.runtime_conversation_id, turn_id=1))
    with pytest.raises(ac.ToolError):
        binding.check(at.ToolScope(tenant_id=link.tenant_id, namespace="shadow",
                                   conversation_id=link.runtime_conversation_id, turn_id=1))


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
            step([tool_use("t1", "search_products", query="حذاء")]),
            step([reply("متوفر", refs=(ref,), commerce=True, call_id="r1")]),
        ],
        transport=transport,
    )
    assert report.tools_called == ("search_products",)
    assert ref in report.evidence_refs
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert report.input_tokens == 200 and report.output_tokens == 40


def test_a_follow_up_sees_the_conversation_the_platform_already_recorded(pilot):
    """The model is given the prior turns as chat turns, not asked to guess them."""
    transport = Transport([accepted("wamid.FOLLOWUP")])
    scripted = ScriptedAnthropic([step([reply("تسعة وتسعون ريال", call_id="r1")])])
    report = entry.run_commerce_runtime_turn(
        engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
        conversation_id=pilot.conversation_id,
        connection_ref=f"wa:{pilot.connection_id}", connection_id=str(pilot.connection_id),
        customer_id=pilot.customer_id, normalized_customer_phone=PHONE,
        provider_message_id="wamid." + uuid.uuid4().hex, inbound_text="وكم سعره؟",
        inbound_metadata={}, transport=transport, instructions="EXISTING-INSTRUCTIONS",
        model=MODEL,
        history=[{"role": "user", "text": QUESTION},
                 {"role": "assistant", "text": "نعم، متوفر"}],
        anthropic_provider=scripted,
    )
    assert report.dispatch_status == dd.SENT_ACCEPTED
    messages = scripted.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"][0]["text"] == QUESTION
    assert messages[1]["content"][0]["text"] == "نعم، متوفر"
    assert messages[-1]["content"][-1]["text"] == "وكم سعره؟"


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
        conversation_ref=cl.conversation_ref_for(
            channel=entry.CHANNEL, app_conversation_id=pilot.conversation_id),
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
            connection_ref=f"wa:{pilot.connection_id}", connection_id=str(pilot.connection_id),
            customer_id=pilot.customer_id, normalized_customer_phone=PHONE,
            provider_message_id=pmid, inbound_text=QUESTION, inbound_metadata={},
            transport=transport_for(label), instructions="EXISTING-INSTRUCTIONS", model=MODEL,
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
            conversation_id=pilot.runtime_conversation_id, token=token,
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
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=first, recorded_by="pilot")
    assert one.status == dd.SENT_UNKNOWN and len(first.sent) == 1

    again = Transport([accepted("wamid.SHOULD-NOT-HAPPEN")])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        two = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=again, recorded_by="pilot")
    # Nothing is sent a second time, and the outcome reported is the one the
    # first attempt established — unknown — rather than "never attempted".
    assert two.status == dd.SENT_UNKNOWN and two.reused_outcome is True
    assert two.blocked_reason == lc.DispatchBlock.OUTCOME_UNKNOWN.value
    assert two.provider_message_id is None
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


# ── The history belongs to this conversation (F7) ────────────────────────────


@contextlib.contextmanager
def _second_conversation(pilot: "Pilot") -> Any:
    """A second conversation for the same tenant, customer and number.

    This is ordinary: a number is re-contacted, a conversation is closed and a
    new one opened, an integration creates one of its own. The platform allows
    it, so the history reader has to be right about it.
    """
    with pilot.engine.begin() as conn:
        other = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, :c, :e, 'active') RETURNING id"),
            {"t": pilot.tenant_a, "c": pilot.customer_id, "e": PHONE}).scalar_one())
    try:
        yield other
    finally:
        with pilot.engine.begin() as conn:
            conn.execute(text("DELETE FROM message_events WHERE conversation_id = :c"),
                         {"c": other})
            conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": other})


@contextlib.contextmanager
def _messages(pilot: "Pilot") -> Any:
    """Write message rows for the block and remove exactly those afterwards."""
    written: List[int] = []

    def say(*, conversation_id: Optional[int], direction: str, body: str,
            phone: Optional[str] = None, wire: Optional[List[Dict[str, Any]]] = None,
            metadata: Optional[Dict[str, Any]] = None) -> int:
        meta: Dict[str, Any] = dict(metadata or {})
        if phone:
            meta["phone"] = phone
        if wire is not None:
            meta["wire_attempts"] = wire
        with pilot.engine.begin() as conn:
            row = int(conn.execute(
                text("INSERT INTO message_events "
                     "(tenant_id, conversation_id, direction, body, metadata) "
                     "VALUES (:t, :c, :d, :b, CAST(:m AS JSONB)) RETURNING id"),
                {"t": pilot.tenant_a, "c": conversation_id, "d": direction, "b": body,
                 "m": json.dumps(meta, ensure_ascii=False)}).scalar_one())
        written.append(row)
        return row

    try:
        yield say
    finally:
        if written:
            with pilot.engine.begin() as conn:
                conn.execute(text("DELETE FROM message_events WHERE id = ANY(:ids)"),
                             {"ids": written})


def _history(pilot: "Pilot", *, conversation_id: Optional[int] = None,
             current_text: str = "") -> List[Dict[str, str]]:
    from services import commerce_runtime_pilot as seam

    session = pilot.session_factory()
    try:
        return seam._prior_turns(
            session, tenant_id=pilot.tenant_a,
            conversation_id=conversation_id or pilot.conversation_id,
            phone=PHONE, current_text=current_text)
    finally:
        session.close()


def test_a_second_conversation_on_the_same_number_does_not_leak_into_the_history(pilot):
    with _second_conversation(pilot) as other, _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="inbound", body="سؤالي أنا")
        say(conversation_id=pilot.conversation_id, direction="outbound", body="جوابي أنا")
        say(conversation_id=other, direction="inbound", body="سؤال المحادثة الأخرى")
        say(conversation_id=other, direction="outbound", body="جواب المحادثة الأخرى")
        turns = _history(pilot)
    assert turns == [{"role": "user", "text": "سؤالي أنا"},
                     {"role": "assistant", "text": "جوابي أنا"}]


def test_the_history_is_this_conversation_s_even_when_the_number_resolves_to_another(pilot):
    """The phone lookup answers with the latest conversation. That is not this one."""
    from core.conversation_engine import StateManager

    with _second_conversation(pilot) as other, _messages(pilot) as say:
        say(conversation_id=other, direction="inbound", body="لا يخص هذه المحادثة")
        session = pilot.session_factory()
        try:
            resolved = StateManager._find_conversation(session, PHONE, pilot.tenant_a)
        finally:
            session.close()
        assert resolved is not None and int(resolved.id) == other   # the divergence is real
        assert _history(pilot) == []                                # and it is not followed


def test_unlinked_legacy_rows_are_left_out_while_the_number_names_two_conversations(pilot):
    with _second_conversation(pilot), _messages(pilot) as say:
        say(conversation_id=None, direction="inbound", body="رسالة قديمة بلا ربط", phone=PHONE)
        assert _history(pilot) == []


def test_unlinked_legacy_rows_are_kept_while_the_number_names_only_this_conversation(pilot):
    with _messages(pilot) as say:
        say(conversation_id=None, direction="inbound", body="رسالة قديمة بلا ربط", phone=PHONE)
        assert _history(pilot) == [{"role": "user", "text": "رسالة قديمة بلا ربط"}]


def test_an_outbound_row_contributes_what_the_wire_recorded_not_the_draft(pilot):
    """The model is shown the message the customer received."""
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="outbound", body="المسودة",
            wire=[{"classification": "ok", "wamid": "wamid.1",
                   "text_fields": {"text.body": "ما وصل فعلًا"}}])
        assert _history(pilot) == [{"role": "assistant", "text": "ما وصل فعلًا"}]


def test_an_outbound_row_whose_wire_recorded_nothing_delivered_contributes_nothing(pilot):
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="outbound", body="المسودة",
            wire=[{"classification": "provider_error_field", "text_fields": {"text.body": "x"}}])
        assert _history(pilot) == []


def test_the_message_being_answered_now_is_not_shown_to_the_model_twice(pilot):
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="inbound", body="سابق")
        say(conversation_id=pilot.conversation_id, direction="inbound", body=QUESTION)
        assert _history(pilot, current_text=QUESTION) == [{"role": "user", "text": "سابق"}]


def test_a_number_that_names_no_conversation_at_all_does_not_admit_unlinked_rows(pilot):
    """Zero associations is not proof of a unique one. A conversation whose
    number lives only in ``external_id`` produces no association row."""
    with pilot.engine.begin() as conn:
        orphan = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, NULL, :e, 'active') RETURNING id"),
            {"t": pilot.tenant_a, "e": UNLINKED_PHONE}).scalar_one())
        conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, NULL, :e, 'active')"),
            {"t": pilot.tenant_a, "e": UNLINKED_PHONE})
    try:
        session = pilot.session_factory()
        try:
            from services import commerce_runtime_pilot as seam

            assert seam._phone_is_unambiguous(
                session, tenant_id=pilot.tenant_a, conversation_id=orphan,
                phones=seam._phone_variants(UNLINKED_PHONE)) is False
        finally:
            session.close()
    finally:
        with pilot.engine.begin() as conn:
            conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t AND external_id = :e"),
                         {"t": pilot.tenant_a, "e": UNLINKED_PHONE})


def test_the_established_association_must_be_exactly_this_conversation(pilot):
    """The linked conversation is unambiguous; the same query for any other id is not."""
    from services import commerce_runtime_pilot as seam

    session = pilot.session_factory()
    try:
        phones = seam._phone_variants(PHONE)
        assert seam._phone_is_unambiguous(
            session, tenant_id=pilot.tenant_a, conversation_id=pilot.conversation_id,
            phones=phones) is True
        assert seam._phone_is_unambiguous(
            session, tenant_id=pilot.tenant_a, conversation_id=pilot.conversation_id + 9_000,
            phones=phones) is False
    finally:
        session.close()


def test_an_unobserved_outbound_row_is_kept_for_the_operator_and_withheld_from_the_model(pilot):
    """A recovered send's body is the reserved intent, which the send path may
    have rewritten. It stays in the store; it is not shown as what was said."""
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="inbound", body="سؤال")
        row = say(conversation_id=pilot.conversation_id, direction="outbound",
                  body="النية المحجوزة", metadata={
                      "chosen_path": "commerce_runtime_pilot",
                      "commerce_runtime_wire_observed": False,
                      "final_text_transformed": True,
                      "final_transform_reasons": ["wire_text_unobserved"]})
        with pilot.engine.connect() as conn:
            stored = conn.execute(text("SELECT body FROM message_events WHERE id = :i"),
                                  {"i": row}).scalar_one()
        assert stored == "النية المحجوزة"            # the operator still has it
        assert _history(pilot) == [{"role": "user", "text": "سؤال"}]


def test_an_observed_outbound_row_is_still_shown_to_the_model(pilot):
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="outbound", body="ما أرسلناه",
            metadata={"chosen_path": "commerce_runtime_pilot",
                      "commerce_runtime_wire_observed": True,
                      "final_text_transformed": False, "final_transform_reasons": []})
        assert _history(pilot) == [{"role": "assistant", "text": "ما أرسلناه"}]


def test_another_path_s_row_is_never_judged_by_this_runtime_s_marker(pilot):
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="outbound", body="رد المسار القديم",
            metadata={"chosen_path": "merchant_brain",
                      "commerce_runtime_wire_observed": False})
        assert _history(pilot) == [{"role": "assistant", "text": "رد المسار القديم"}]


def test_another_tenant_s_rows_are_never_part_of_this_conversation_s_history(pilot):
    with _messages(pilot) as say:
        say(conversation_id=pilot.conversation_id, direction="inbound", body="لنا")
        with pilot.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO message_events "
                     "(tenant_id, conversation_id, direction, body, metadata) "
                     "VALUES (:t, :c, 'inbound', 'لغيرنا', CAST(:m AS JSONB))"),
                {"t": pilot.tenant_b, "c": pilot.conversation_id,
                 "m": json.dumps({"phone": PHONE})})
        try:
            assert _history(pilot) == [{"role": "user", "text": "لنا"}]
        finally:
            with pilot.engine.begin() as conn:
                conn.execute(text("DELETE FROM message_events WHERE tenant_id = :t"),
                             {"t": pilot.tenant_b})


# ── The model is the platform's explicit choice (F13) ────────────────────────


def test_the_configured_model_is_what_the_provider_call_is_made_with(pilot):
    """Not the legacy resolution, and not the repository fallback."""
    scripted = ScriptedAnthropic([step([reply("تمام", call_id="r1")])])
    report = entry.run_commerce_runtime_turn(
        engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
        conversation_id=pilot.conversation_id,
        connection_ref=f"wa:{pilot.connection_id}", connection_id=str(pilot.connection_id),
        customer_id=pilot.customer_id, normalized_customer_phone=PHONE,
        provider_message_id="wamid." + uuid.uuid4().hex, inbound_text=QUESTION,
        inbound_metadata={}, transport=Transport([accepted()]),
        instructions="EXISTING-INSTRUCTIONS", model="a-specific-model",
        anthropic_provider=scripted,
    )
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert scripted.calls[0]["audit_context"]["model"] == "a-specific-model"
    assert report.requested_model == "a-specific-model"


def test_what_was_asked_for_and_what_answered_are_recorded_separately(pilot):
    report = pilot.run(answers=[step([reply("تمام", call_id="r1")])],
                       transport=Transport([accepted()]))
    assert report.requested_model == MODEL          # the platform's choice
    assert report.model == "scripted-model"         # what the provider reported


@pytest.mark.parametrize("unset", ["", "   "])
def test_a_turn_with_no_configured_model_admits_nothing_and_sends_nothing(pilot, unset):
    """An unconfigured pilot leaves no turn behind to be recovered or replayed."""
    transport = Transport([])
    scripted = ScriptedAnthropic([step([reply("تمام", call_id="r1")])])
    before = _turn_count(pilot)
    report = entry.run_commerce_runtime_turn(
        engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
        conversation_id=pilot.conversation_id,
        connection_ref=f"wa:{pilot.connection_id}", connection_id=str(pilot.connection_id),
        customer_id=pilot.customer_id, normalized_customer_phone=PHONE,
        provider_message_id="wamid." + uuid.uuid4().hex, inbound_text=QUESTION,
        inbound_metadata={}, transport=transport, instructions="EXISTING-INSTRUCTIONS",
        model=unset, anthropic_provider=scripted,
    )
    assert report.reason == entry.MODEL_UNCONFIGURED
    assert report.turn_id is None
    assert transport.sent == [] and scripted.calls == []
    assert _turn_count(pilot) == before


def _turn_count(pilot: "Pilot") -> int:
    with pilot.engine.connect() as conn:
        return int(conn.execute(
            text("SELECT count(*) FROM commerce_runtime_turns WHERE tenant_id = :t"),
            {"t": pilot.tenant_a}).scalar() or 0)


# ── Unfinished work is recoverable; finished work is not (F3) ────────────────


def _unfinished(pilot: "Pilot", provider_message_id: str, *, tenant: Optional[int] = None) -> Any:
    from core.commerce_runtime import recovery

    return recovery.unfinished_turn_for(
        tenant_id=tenant or pilot.tenant_a, phone_number_id=pilot.connection_id,
        provider_message_id=provider_message_id, engine=pilot.engine)


@contextlib.contextmanager
def _open_turn(pilot: "Pilot") -> Any:
    """Admit one turn and leave it unfinished for the block, then remove it.

    An open turn is the only turn its conversation may work on, so it is
    deleted afterwards rather than finished: finishing it would be a different
    fixture, and leaving it would make every later case in this module queue
    behind it.
    """
    pmid = "wamid." + uuid.uuid4().hex
    admitted = pilot.ledgers.foundation.admit_turn(
        tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
        conversation_ref=cl.conversation_ref_for(channel=entry.CHANNEL,
                                                 app_conversation_id=pilot.conversation_id),
        channel_connection_ref=f"wa:{pilot.connection_id}", provider_message_id=pmid,
        payload={"text": QUESTION})
    try:
        yield pmid, admitted
    finally:
        # A turn the block finished keeps its terminal — terminals are
        # immutable and a finished turn blocks nothing. Only one still open is
        # removed.
        with pilot.engine.begin() as conn:
            still_open = conn.execute(
                text("SELECT 1 FROM commerce_runtime_turns t "
                     "LEFT JOIN commerce_runtime_turn_terminals x ON x.turn_id = t.id "
                     "WHERE t.id = :i AND x.turn_id IS NULL"),
                {"i": admitted.turn_id}).first()
            if still_open is not None:
                conn.execute(text("DELETE FROM commerce_runtime_turns WHERE id = :i"),
                             {"i": admitted.turn_id})


def test_an_admitted_turn_with_no_terminal_is_recoverable(pilot):
    """The shape a worker leaves behind when it stops mid-turn."""
    with _open_turn(pilot) as (pmid, admitted):
        found = _unfinished(pilot, pmid)
        assert found is not None
        assert found.turn_id == admitted.turn_id and found.tenant_id == pilot.tenant_a

        # Once it is finished, it is a duplicate again and nothing may reopen it.
        with pilot.owned(turn_id=admitted.turn_id) as token:
            dd.complete_turn(ledgers=pilot.ledgers, tenant_id=pilot.tenant_a,
                             namespace=entry.NAMESPACE, turn_id=admitted.turn_id, token=token,
                             processing_outcome=c.ProcessingOutcome.FAILED.value)
        assert _unfinished(pilot, pmid) is None


def test_a_completed_turn_is_never_recoverable(pilot):
    report = pilot.run(answers=[step([reply("تم", call_id="r1")])],
                       transport=Transport([accepted("wamid.DONE")]))
    assert report.dispatch_status == dd.SENT_ACCEPTED
    with pilot.engine.connect() as conn:
        pmid = conn.execute(
            text("SELECT provider_message_id FROM commerce_runtime_turns WHERE id = :i"),
            {"i": report.turn_id}).scalar_one()
    assert _unfinished(pilot, pmid) is None


def test_an_inbound_message_this_runtime_never_admitted_is_not_recoverable(pilot):
    assert _unfinished(pilot, "wamid." + uuid.uuid4().hex) is None


def test_another_tenant_cannot_recover_this_tenant_s_unfinished_turn(pilot):
    with _open_turn(pilot) as (pmid, _admitted):
        assert _unfinished(pilot, pmid, tenant=pilot.tenant_b) is None
        assert _unfinished(pilot, pmid) is not None


def test_a_turn_admitted_on_another_channel_connection_is_not_recovered_here(pilot):
    from core.commerce_runtime import recovery

    with _open_turn(pilot) as (pmid, _admitted):
        assert recovery.unfinished_turn_for(
            tenant_id=pilot.tenant_a, phone_number_id="some-other-number",
            provider_message_id=pmid, engine=pilot.engine) is None


def test_recovery_is_offered_only_for_a_configured_tenant_and_recipient(pilot, monkeypatch):
    from core.commerce_runtime import pilot_guard as guard
    from core.commerce_runtime import recovery

    with _open_turn(pilot) as (pmid, _admitted):
        def ask() -> Any:
            return recovery.duplicate_carries_unfinished_work(
                phone_number_id=pilot.connection_id, customer_phone=PHONE,
                provider_message_id=pmid, engine=pilot.engine)

        monkeypatch.setenv(guard.ENV_ENABLED, "true")
        monkeypatch.setenv(guard.ENV_TENANT_ALLOWLIST, str(pilot.tenant_a))
        monkeypatch.setenv(guard.ENV_RECIPIENT_ALLOWLIST, PHONE)
        monkeypatch.setenv(guard.ENV_MODEL, MODEL)
        assert ask() is not None

        monkeypatch.delenv(guard.ENV_ENABLED, raising=False)
        assert ask() is None                               # the pilot is off
        monkeypatch.setenv(guard.ENV_ENABLED, "true")

        monkeypatch.setenv(guard.ENV_TENANT_ALLOWLIST, str(pilot.tenant_b))
        assert ask() is None                               # another tenant's list
        monkeypatch.setenv(guard.ENV_TENANT_ALLOWLIST, str(pilot.tenant_a))

        monkeypatch.setenv(guard.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
        assert ask() is None                               # another recipient
        monkeypatch.setenv(guard.ENV_RECIPIENT_ALLOWLIST, PHONE)

        monkeypatch.delenv(guard.ENV_MODEL, raising=False)
        assert ask() is None                               # no model is configured


# ── Handover: nothing is abandoned on the way out (F4) ───────────────────────


def _handover(pilot: "Pilot", *, tenant: Optional[int] = None) -> Any:
    from core.commerce_runtime import recovery

    return recovery.handover_state(tenant_ids=[tenant or pilot.tenant_a], engine=pilot.engine)[0]


def _in_flight(pilot: "Pilot", *, tenant: Optional[int] = None) -> Tuple[int, int, int]:
    state = _handover(pilot, tenant=tenant)
    return state.open_turns, state.reserved_undispatched, state.unresolved_attempts


def test_a_tenant_with_nothing_in_flight_is_settled(pilot):
    """A tenant of its own, so the counts are this case's and nothing else's."""
    fresh = _seed(pilot.engine, label="تسليم")
    state = _handover(pilot, tenant=fresh)
    assert state.open_turns == 0 and state.reserved_undispatched == 0
    assert state.unresolved_attempts == 0 and state.settled is True


def test_a_turn_left_open_is_counted_and_is_not_settled(pilot):
    before = _in_flight(pilot)
    with _open_turn(pilot) as (_pmid, _admitted):
        during = _in_flight(pilot)
        assert during[0] == before[0] + 1
        assert _handover(pilot).settled is False
    assert _in_flight(pilot) == before


def test_a_reply_reserved_and_never_sent_is_counted(pilot):
    """Composed, verified, and nobody dispatched it. Switching off abandons it."""
    before = _in_flight(pilot)
    reservation = pilot.reserve_reply("محجوزة ولم تُرسل")
    try:
        assert _in_flight(pilot)[1] == before[1] + 1
        assert _handover(pilot).settled is False
    finally:
        with pilot.owned(turn_id=reservation.turn_id) as token:
            dd.dispatch_reserved_delivery(
                ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
                conversation_id=pilot.runtime_conversation_id, token=token,
                sequence_id=reservation.sequence_id, transport=Transport([accepted()]),
                recorded_by="pilot")
            dd.complete_turn(ledgers=pilot.ledgers, tenant_id=pilot.tenant_a,
                             namespace=entry.NAMESPACE, turn_id=reservation.turn_id, token=token,
                             processing_outcome=c.ProcessingOutcome.COMPLETED.value)
    assert _in_flight(pilot) == before


def test_a_send_completing_after_the_handover_began_is_recorded_not_resent(pilot):
    """The transport answered late. The turn is finished from that answer alone."""
    before = _in_flight(pilot)
    reservation = pilot.reserve_reply("ردّ بطيء")
    gate = threading.Event()
    sends: List[str] = []

    def slow(payload: Any) -> lc.SendResponse:
        sends.append(str(payload.get("text") or ""))
        gate.wait(10)                       # the provider answers only later
        return accepted("wamid.LATE")

    outcome: Dict[str, Any] = {}

    def dispatch() -> None:
        with pilot.owned(turn_id=reservation.turn_id, owner="slow-sender") as token:
            outcome["first"] = dd.dispatch_reserved_delivery(
                ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
                conversation_id=pilot.runtime_conversation_id, token=token,
                sequence_id=reservation.sequence_id, transport=slow, recorded_by="slow-sender")

    worker = threading.Thread(target=dispatch)
    worker.start()
    try:
        # While it is in flight the turn is unfinished and the handover is not settled.
        for _ in range(100):
            if sends:
                break
            time.sleep(0.02)
        assert sends == ["ردّ بطيء"]
        assert _in_flight(pilot)[2] == before[2] + 1        # one send with no receipt yet
        assert _handover(pilot).settled is False
        assert _unfinished(pilot, _pmid_of(pilot, reservation.turn_id)) is not None
    finally:
        gate.set()
        worker.join(20)
    assert outcome["first"].status == dd.SENT_ACCEPTED

    # The retry that arrives afterwards finishes the turn from the receipt and
    # dispatches nothing of its own.
    again = Transport([])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        second = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=again, recorded_by="recovery")
        terminal = dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=second.processing_outcome)
    assert again.sent == [] and sends == ["ردّ بطيء"]           # sent once, in total
    assert second.status == dd.SENT_ACCEPTED and second.reused_outcome is True
    assert second.provider_message_id == "wamid.LATE"
    assert terminal is not None
    assert terminal.processing_outcome == c.ProcessingOutcome.COMPLETED.value
    assert _in_flight(pilot) == before


def _pmid_of(pilot: "Pilot", turn_id: int) -> str:
    with pilot.engine.connect() as conn:
        return str(conn.execute(
            text("SELECT provider_message_id FROM commerce_runtime_turns WHERE id = :i"),
            {"i": turn_id}).scalar_one())


def test_a_send_whose_outcome_is_unknown_is_never_settled(pilot):
    """The reviewer's wrapper-timeout shape: the send wrapper gave up, the
    receipt says unknown, the turn was completed honestly as failed — and the
    request may still be with the provider. "Nothing in flight" is not true."""
    before = _in_flight(pilot)
    reservation = pilot.reserve_reply("مصير غير معروف")
    with pilot.owned(turn_id=reservation.turn_id) as token:
        outcome = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=Transport([timed_out()]),
            recorded_by="pilot")
        terminal = dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=outcome.processing_outcome)
    assert outcome.status == dd.SENT_UNKNOWN
    # The turn itself is finished and honest about what it does not know…
    assert terminal is not None
    assert terminal.transport_outcome == c.TransportOutcome.UNKNOWN.value
    state = _handover(pilot)
    assert state.open_turns == before[0]                  # nothing is left open
    # …and the handover still refuses to call the tenant settled.
    assert state.unknown_outcomes >= 1 and state.settled is False


def test_an_acceptance_after_an_unknown_resolves_it(pilot):
    """An unknown the provider later identified is no longer unknown."""
    reservation = pilot.reserve_reply("مؤكدة بعد الغموض")
    with pilot.owned(turn_id=reservation.turn_id) as token:
        first = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=Transport([timed_out()]),
            recorded_by="pilot")
        assert first.status == dd.SENT_UNKNOWN
        unknown_now = _handover(pilot).unknown_outcomes
        # The operator establishes what the provider did and records it on the
        # same attempt; nothing is re-sent.
        pilot.ledgers.record_delivery_receipt(
            tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, attempt_id=first.attempt_id,
            kind=lc.ReceiptKind.ACCEPTED, provider_message_id="wamid.RESOLVED",
            evidence={"established_by": "operator"}, recorded_by="operator")
        assert _handover(pilot).unknown_outcomes == unknown_now - 1
        dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=c.ProcessingOutcome.COMPLETED.value)


def test_another_tenant_s_work_is_never_part_of_this_tenant_s_handover(pilot):
    before = _in_flight(pilot)
    with _open_turn(pilot) as (_pmid, _admitted):
        assert _in_flight(pilot)[0] == before[0] + 1
        assert _handover(pilot, tenant=pilot.tenant_b).settled is True


# ── A refused redispatch reports what is established (F10) ───────────────────


def test_a_redispatch_of_an_accepted_send_reports_that_acceptance_rather_than_failure(pilot):
    """The customer holds the message. Completing the turn as failed is untrue."""
    reservation = pilot.reserve_reply("وصلت فعلًا")
    first = Transport([accepted("wamid.FIRST")])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        one = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=first, recorded_by="pilot")
    assert one.status == dd.SENT_ACCEPTED and one.reused_outcome is False

    again = Transport([accepted("wamid.SHOULD-NOT-HAPPEN")])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        two = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=again, recorded_by="pilot")
    assert again.sent == []                                   # nothing was sent twice
    assert two.status == dd.SENT_ACCEPTED and two.reused_outcome is True
    assert two.provider_message_id == "wamid.FIRST"           # the first send's own id
    assert two.delivered is True
    assert two.processing_outcome == c.ProcessingOutcome.COMPLETED.value

    with pilot.owned(turn_id=reservation.turn_id) as token:
        terminal = dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=two.processing_outcome)
    assert terminal is not None
    assert terminal.processing_outcome == c.ProcessingOutcome.COMPLETED.value
    assert terminal.transport_outcome == c.TransportOutcome.ACCEPTED.value
    # And exactly one receipt exists: reuse records nothing of its own.
    kinds = [r.kind for r in pilot.receipts(reservation.turn_id)]
    assert kinds == [lc.ReceiptKind.ACCEPTED.value]


def test_a_redispatch_after_a_definitive_rejection_reports_that_rejection(pilot):
    reservation = pilot.reserve_reply("مرفوضة")
    with pilot.owned(turn_id=reservation.turn_id) as token:
        one = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=Transport([rejected()]),
            recorded_by="pilot")
    assert one.status == dd.SENT_REJECTED

    again = Transport([accepted("wamid.SHOULD-NOT-HAPPEN")])
    with pilot.owned(turn_id=reservation.turn_id) as token:
        two = dd.dispatch_reserved_delivery(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            conversation_id=pilot.runtime_conversation_id, token=token,
            sequence_id=reservation.sequence_id, transport=again, recorded_by="pilot")
    assert again.sent == []
    assert two.status == dd.SENT_REJECTED and two.reused_outcome is True
    assert two.delivered is False
    assert two.processing_outcome == c.ProcessingOutcome.FAILED.value

    with pilot.owned(turn_id=reservation.turn_id) as token:
        terminal = dd.complete_turn(
            ledgers=pilot.ledgers, tenant_id=pilot.tenant_a, namespace=entry.NAMESPACE,
            turn_id=reservation.turn_id, token=token,
            processing_outcome=two.processing_outcome)
    assert terminal is not None
    assert terminal.transport_outcome == c.TransportOutcome.REJECTED_DEFINITIVE.value


def test_a_re_entry_that_recovers_an_accepted_send_records_the_message_once(pilot):
    """The first caller died before persisting; the second must not write it twice."""
    from services import commerce_runtime_pilot as seam

    class _Trace:
        def __init__(self) -> None:
            self.marked: List[int] = []

        def mark_outbound_sent(self, *, source: str, length: int = 0) -> None:
            self.marked.append(length)

    class _Convo:
        id = pilot.conversation_id

    recovered = entry.TurnReport(
        reason=entry.HANDLED, tenant_id=pilot.tenant_a, conversation_id=pilot.conversation_id,
        turn_id=1, dispatch_status=dd.SENT_ACCEPTED, provider_message_id="wamid.RECOVERED",
        delivery_sequence_id=1, reply_text="الرسالة المستعادة", reused_dispatch=True)

    session = pilot.session_factory()
    try:
        # First recovery: nothing is in the transcript, so the message is written.
        seam._record(db=session, trace=_Trace(), convo=_Convo(), tenant_id=pilot.tenant_a,
                     to=PHONE, report=recovered, wire=seam.WireObservation())
        session.commit()
        # Second recovery of the same accepted send: already there, so nothing.
        trace = _Trace()
        seam._record(db=session, trace=trace, convo=_Convo(), tenant_id=pilot.tenant_a,
                     to=PHONE, report=recovered, wire=seam.WireObservation())
        session.commit()
        assert trace.marked == []
        with pilot.engine.connect() as conn:
            rows = int(conn.execute(
                text("SELECT count(*) FROM message_events WHERE tenant_id = :t "
                     "AND metadata->>'provider_message_id' = 'wamid.RECOVERED'"),
                {"t": pilot.tenant_a}).scalar() or 0)
        assert rows == 1
    finally:
        session.close()
        with pilot.engine.begin() as conn:
            conn.execute(text("DELETE FROM message_events WHERE tenant_id = :t "
                              "AND metadata->>'provider_message_id' = 'wamid.RECOVERED'"),
                         {"t": pilot.tenant_a})


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
            conversation_id=1, connection_ref="wa:1", connection_id="1",
            customer_id=None, normalized_customer_phone=PHONE,
            provider_message_id="wamid." + uuid.uuid4().hex, inbound_text=QUESTION,
            inbound_metadata=None, transport=transport, instructions="EXISTING-INSTRUCTIONS",
            model=MODEL,
        )
        assert report.reason == entry.SCHEMA_UNAVAILABLE
        assert transport.sent == []
    finally:
        entry.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)
