"""Dormant agent loop core, proven on real PostgreSQL.

Every test runs against a disposable database created for this module and
migrated with the repository's own Alembic chain to revision ``0109``. The
reasoning provider is always a deterministic script and the tools always read
an in-memory fixture catalogue: no model, commerce, payment or messaging
provider is called, and nothing is ever sent. Concurrency, crash and lock-wait
cases use independent connections and independent spawned processes. Requires
``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and that skip is reported, never counted.

These tests prove *orchestration and its durable guarantees*: attempt
accounting, enforced waits, scope and eligibility, complete boundary
validation, isolation of authoritative data, and closed outcomes. They
establish no live model quality, no provider compatibility, no WhatsApp
delivery and no customer readiness.
"""
from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, text

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_scripted as sp
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.agent_loop import AgentLoop
from core.commerce_runtime.ledgers import LedgerRepository
from tests.commerce_reliability import commerce_runtime_agent_workers as w
from tests.commerce_reliability.agent_fixture_catalog import build_registry
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    CHANNEL,
    LIVE,
    _alembic,
    _create_database,
    _drop_database,
    _pmid,
    _ref,
    _run_crash_worker,
    _run_workers,
    _seed_tenant,
)

REVISION = "0109"
OWNER_A, OWNER_B = "agent-a", "agent-b"
# The representative compound question: one product question and one delivery
# question in a single customer turn. Both branches below answer it from the
# same admitted input with the same tool query.
COMPOUND_QUESTION = "عندكم حذاء رياضي؟ ومتى يوصل؟"
COMPOUND_QUERY = "حذاء"


# ── Harness ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Harness:
    dsn: str
    engine: Any
    ledgers: LedgerRepository
    registry: at.ToolRegistry
    tenant_a: int
    tenant_b: int
    tenant_c: int

    def loop(self, *, budget: Optional[ac.LoopBudget] = None, clock=None,
             registry: Optional[at.ToolRegistry] = None) -> AgentLoop:
        return AgentLoop(self.ledgers, registry or self.registry, budget=budget, clock=clock)

    def admit(self, *, tenant: Optional[int] = None, body: str = COMPOUND_QUESTION,
              conversation_ref: Optional[str] = None) -> c.AdmittedTurn:
        return self.ledgers.foundation.admit_turn(
            tenant_id=tenant or self.tenant_a, namespace=LIVE, conversation_ref=conversation_ref or _ref(),
            channel_connection_ref=CHANNEL, provider_message_id=_pmid(), payload={"kind": "text", "text": body})

    def start(self, *, tenant: Optional[int] = None, body: str = COMPOUND_QUESTION, owner: str = OWNER_A,
              seconds: int = 60) -> Tuple[c.AdmittedTurn, c.Lease]:
        turn = self.admit(tenant=tenant, body=body)
        lease = self.ledgers.foundation.claim(tenant_id=tenant or self.tenant_a, namespace=LIVE,
                                              conversation_id=turn.conversation_id, owner_id=owner,
                                              lease_seconds=seconds)
        return turn, lease

    def run(self, turn: c.AdmittedTurn, lease: c.Lease, provider: Any, *, loop: Optional[AgentLoop] = None,
            tenant: Optional[int] = None, conversation_id: Optional[int] = None,
            cancelled=None) -> ac.LoopOutcome:
        return (loop or self.loop()).run_turn(
            tenant_id=tenant or self.tenant_a, namespace=LIVE,
            conversation_id=conversation_id or turn.conversation_id, turn_id=turn.turn_id,
            token=lease.token, provider=provider, cancelled=cancelled)

    def snapshot(self, turn: c.AdmittedTurn, *, tenant: Optional[int] = None) -> c.ConversationSnapshot:
        return self.ledgers.foundation.get_conversation(tenant_id=tenant or self.tenant_a, namespace=LIVE,
                                                        conversation_id=turn.conversation_id)

    def sequence(self, turn: c.AdmittedTurn, *, tenant: Optional[int] = None):
        return self.ledgers.get_delivery_sequence(tenant_id=tenant or self.tenant_a, namespace=LIVE,
                                                  turn_id=turn.turn_id)

    def sequences(self, turn: c.AdmittedTurn) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM commerce_runtime_delivery_sequences "
                                         "WHERE turn_id = :t"), {"t": turn.turn_id}).scalar())

    def summary(self, turn: c.AdmittedTurn, *, tenant: Optional[int] = None) -> lc.TurnLedgerSummary:
        return self.ledgers.turn_ledger_summary(tenant_id=tenant or self.tenant_a, namespace=LIVE,
                                                turn_id=turn.turn_id)

    def terminal(self, turn: c.AdmittedTurn, *, tenant: Optional[int] = None) -> Optional[c.TerminalRecord]:
        return self.ledgers.foundation.get_terminal(tenant_id=tenant or self.tenant_a, namespace=LIVE,
                                                    turn_id=turn.turn_id)

    def progress(self, turn: c.AdmittedTurn, *, tenant: Optional[int] = None) -> Optional[ac.LoopProgress]:
        payload = self.snapshot(turn, tenant=tenant).state_payload
        return ac.LoopProgress.from_payload(payload.get(ac.AGENT_LOOP_STATE_KEY), turn_id=turn.turn_id)

    def open_transactions(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state IN ('idle in transaction', 'idle in transaction (aborted)')")).scalar())

    def worker_args(self, turn: c.AdmittedTurn, lease: c.Lease, script: str, **extra) -> Dict[str, Any]:
        args = {"tenant_id": self.tenant_a, "namespace": LIVE, "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id, "token": dataclasses.asdict(lease.token), "script": script,
                "tenants": {"a": self.tenant_a, "b": self.tenant_b, "c": self.tenant_c}}
        args.update(extra)
        return args


@pytest.fixture(scope="module")
def agent(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        tenants = [_seed_tenant(engine, label) for label in ("A", "B", "C")]
        yield Harness(dsn=dsn, engine=engine, ledgers=LedgerRepository(engine),
                      registry=build_registry(*tenants), tenant_a=tenants[0], tenant_b=tenants[1],
                      tenant_c=tenants[2])
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _kinds(outcome: ac.LoopOutcome) -> List[str]:
    return [e.kind for e in outcome.events]


def _without_debits(outcome: ac.LoopOutcome) -> List[str]:
    return [k for k in _kinds(outcome) if k != "attempt_debited"]


class _FakeClock:
    """A monotonic clock a test advances explicitly; no wall-clock assumptions."""

    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class _RowLockHolder:
    """Holds the conversation row lock on an independent connection for a while."""

    def __init__(self, dsn: str, conversation_id: int, hold_seconds: float) -> None:
        self._dsn, self._conversation_id, self._hold = dsn, conversation_id, hold_seconds
        self.locked = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        engine = create_engine(self._dsn)
        try:
            with engine.begin() as conn:
                conn.execute(text("SELECT id FROM commerce_runtime_conversations WHERE id = :i FOR UPDATE"),
                             {"i": self._conversation_id})
                self.locked.set()
                time.sleep(self._hold)
        finally:
            engine.dispose()

    def start(self) -> None:
        self._thread.start()
        assert self.locked.wait(timeout=30), "the lock holder never took the row lock"

    def join(self) -> None:
        self._thread.join(timeout=60)


# ── The complete local path ──────────────────────────────────────────────────


def test_turn_reasoning_tool_observation_reasoning_reply_and_one_delivery_intent(agent: Harness) -> None:
    """turn → reasoning → read-only tool → observation → further reasoning → verified reply → one intent."""
    turn, lease = agent.start()
    seen: List[Tuple[int, List[str]]] = []

    def second_step(request: ac.ProviderRequest) -> ac.ProviderResult:
        seen.append((request.step_no, [o.call_id for o in request.observations]))
        observation = sp.observed(request, "c1")
        assert observation is not None and observation.ok
        product = observation.result["products"][0]
        entry = sp.observed(request, "c2").result["entries"][0]
        return sp.reply(f"{product['name']} متوفر بسعر {product['price']} {product['currency']}. {entry['text']}",
                        refs=[product["ref"], entry["ref"]], commerce=True)

    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص"),
                 sp.tool_call("c2", "merchant_knowledge_lookup", topic="shipping")),
        second_step])
    outcome = agent.run(turn, lease, provider)

    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value and outcome.stop_reason is None
    assert (outcome.steps_used, outcome.tool_calls_used, outcome.reused_delivery) == (2, 2, False)
    assert _without_debits(outcome) == ["provider_step", "tool_observation", "tool_observation", "provider_step",
                                        "reply_accepted", "delivery_intent_reserved"]
    assert _kinds(outcome).count("attempt_debited") == 3, "one debit per provider step plus one per tool bundle"
    assert seen == [(2, ["c1", "c2"])], "the second step sees the first step's observations"
    sequence = agent.sequence(turn)
    assert sequence is not None and agent.sequences(turn) == 1
    assert (sequence.attempt_count, sequence.outcome, sequence.turn_id) == (0, "pending", turn.turn_id)
    assert outcome.delivery_sequence_id == sequence.sequence_id
    summary = agent.summary(turn)
    assert (summary.transport_outcome, summary.customer_reach) == ("not_attempted", "not_reached")
    assert agent.terminal(turn) is None, "generating a reply is not completing the turn"
    snapshot = agent.snapshot(turn)
    assert snapshot.state_payload["reply"]["evidence_refs"] == ["product:blue_cotton_shirt", "kb:shipping"]
    progress = agent.progress(turn)
    assert progress is not None
    assert (progress.phase, progress.steps_used, progress.tool_calls_used) == ("reply_pending_delivery", 2, 2)
    assert agent.open_transactions() == 0


def test_same_question_and_query_with_different_observations_take_different_paths(agent: Harness) -> None:
    """The representative compound question, asked identically of two merchants.

    Same admitted input, same tool query: only the observation differs. The
    in-stock branch answers the delivery half from the merchant's own shipping
    evidence; the out-of-stock branch says plainly that no delivery date can be
    given while the product is unavailable. Neither invents a duration, and the
    out-of-stock branch needs no shipping lookup at all.
    """
    runs: Dict[str, Tuple[ac.LoopOutcome, List[ac.ProviderRequest], c.AdmittedTurn]] = {}
    for label, tenant in (("in_stock", agent.tenant_c), ("out_of_stock", agent.tenant_a)):
        turn, lease = agent.start(tenant=tenant)
        assert turn.payload["text"] == COMPOUND_QUESTION

        def after_search(request: ac.ProviderRequest) -> ac.ProviderResult:
            observation = sp.observed(request, "c1")
            assert observation is not None and observation.ok
            product = observation.result["products"][0]
            if not product["in_stock"]:
                # The delivery half is answered by acknowledging that it cannot
                # be answered yet — no invented date, no further lookup.
                return sp.reply(f"{product['name']} غير متوفر حاليًا، ولذلك لا يمكنني تحديد موعد وصول له الآن.",
                                refs=[product["ref"]], commerce=True)
            return sp.tools(sp.tool_call("c2", "merchant_knowledge_lookup", topic="shipping"))

        def after_knowledge(request: ac.ProviderRequest) -> ac.ProviderResult:
            product = sp.observed(request, "c1").result["products"][0]
            entry = sp.observed(request, "c2").result["entries"][0]
            return sp.reply(f"{product['name']} متوفر بسعر {product['price']} {product['currency']}. {entry['text']}",
                            refs=[product["ref"], entry["ref"]], commerce=True)

        provider = sp.ScriptedReasoningProvider(
            [sp.tools(sp.tool_call("c1", "catalog_search", query=COMPOUND_QUERY)), after_search, after_knowledge])
        runs[label] = (agent.run(turn, lease, provider, tenant=tenant), provider.requests, turn)

    in_stock, in_requests, in_turn = runs["in_stock"]
    out_stock, out_requests, out_turn = runs["out_of_stock"]
    # Identical question and identical first tool request in both branches.
    assert in_turn.payload == out_turn.payload
    assert in_requests[0].context.inbound["text"] == out_requests[0].context.inbound["text"] == COMPOUND_QUESTION
    # The observation differs, and the next decision differs because of it.
    assert in_requests[1].observations[0].result["products"][0]["in_stock"] is True
    assert out_requests[1].observations[0].result["products"][0]["in_stock"] is False
    assert (in_stock.steps_used, in_stock.tool_calls_used) == (3, 2)
    assert (out_stock.steps_used, out_stock.tool_calls_used) == (2, 1)
    # Both address delivery: one from evidence, one by acknowledging its absence.
    assert in_stock.detail["evidence_refs"] == ["product:black_sneaker", "kb:shipping"]
    assert out_stock.detail["evidence_refs"] == ["product:white_sneaker"]
    assert len(out_requests) == 2, "the out-of-stock branch needed no shipping lookup"
    assert agent.sequences(in_turn) == 1 and agent.sequences(out_turn) == 1


def test_a_direct_reply_finishes_without_an_unnecessary_tool_call(agent: Harness) -> None:
    turn, lease = agent.start(body="السلام عليكم")
    provider = sp.ScriptedReasoningProvider([sp.reply("وعليكم السلام! كيف أقدر أساعدك اليوم؟")])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert (outcome.steps_used, outcome.tool_calls_used) == (1, 0)
    assert "tool_observation" not in _kinds(outcome)
    assert agent.sequence(turn) is not None and agent.sequences(turn) == 1


def test_a_product_question_can_be_answered_with_one_tool_call(agent: Harness) -> None:
    """No shipping lookup is required for a question that does not ask about delivery."""
    turn, lease = agent.start(body="كم سعر القميص القطني؟")
    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        lambda request: sp.reply(
            f"سعره {sp.observed(request, 'c1').result['products'][0]['price']} ريال.",
            refs=[sp.observed(request, "c1").result["products"][0]["ref"]], commerce=True)])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert (outcome.steps_used, outcome.tool_calls_used) == (2, 1)


# ── Verification feedback ────────────────────────────────────────────────────


def test_invalid_evidence_feeds_verification_back_and_the_correction_is_accepted(agent: Harness) -> None:
    turn, lease = agent.start()
    feedback_seen: List[Tuple[int, List[str]]] = []

    def corrected(request: ac.ProviderRequest) -> ac.ProviderResult:
        feedback_seen.append((request.step_no, [p.code for f in request.feedback for p in f.problems]))
        ref = sp.observed(request, "c1").result["products"][0]["ref"]
        return sp.reply("قميص قطني أزرق متوفر.", refs=[ref], commerce=True)

    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.reply("سعر المنتج ١٠ ريال.", refs=["product:invented_item"], commerce=True),
        corrected,
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert feedback_seen == [(3, ["unknown_evidence"])]
    assert _kinds(outcome).count("verification_failed") == 1
    assert agent.sequences(turn) == 1, "the rejected draft reserved nothing"
    assert outcome.detail["evidence_refs"] == ["product:blue_cotton_shirt"]


def test_uncorrectable_verification_fails_bounded_and_reserves_no_delivery(agent: Harness) -> None:
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        sp.reply("سعر المنتج ١٠ ريال.", refs=["product:invented_item"], commerce=True),
        sp.reply("سعره ١٠ ريال أكيد.", refs=["product:still_invented"], commerce=True),
        sp.reply("بسعر ممتاز.", commerce=True),
    ], capabilities=ac.ProviderCapabilities(provider_name="scripted"))
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=3, max_tool_calls=2)))
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.VERIFICATION_FAILED.value
    assert outcome.detail["problems"] == ["missing_evidence"]
    assert agent.sequence(turn) is None and agent.sequences(turn) == 0
    assert agent.terminal(turn) is None
    progress = agent.progress(turn)
    assert progress is not None and progress.stop_reason == ac.StopReason.VERIFICATION_FAILED.value
    assert progress.steps_used == 3, "every attempt consumed budget"


def test_a_commerce_claim_without_any_evidence_is_refused(agent: Harness) -> None:
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([sp.reply("عندنا قميص بـ ٥٠ ريال.", commerce=True)])
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=1)))
    assert outcome.stop_reason == ac.StopReason.VERIFICATION_FAILED.value
    assert outcome.detail["problems"] == ["missing_evidence"]
    assert agent.sequences(turn) == 0


# ── Tool authorization ───────────────────────────────────────────────────────


def test_unknown_tools_invalid_arguments_and_forged_scope_cannot_execute_work(agent: Harness) -> None:
    turn, lease = agent.start()
    observations: List[ac.ToolObservation] = []

    def collect(request: ac.ProviderRequest) -> ac.ProviderResult:
        observations.extend(request.observations)
        return sp.reply("تم، كيف أساعدك؟")

    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "place_order", product_ref="product:blue_cotton_shirt")),
        sp.tools(sp.tool_call("c2", "catalog_search", query=123)),
        sp.tools(sp.tool_call("c3", "catalog_search", query="قميص", tenant_id=agent.tenant_b)),
        sp.tools(sp.tool_call("c4", "catalog_search", query="قميص", sort_by="price")),
        sp.tools(sp.tool_call("c5", "product_lookup", product_ref="product:leather_belt")),
        collect,
    ])
    outcome = agent.run(turn, lease, provider,
                        loop=agent.loop(budget=ac.LoopBudget(max_steps=6, max_tool_calls=6)))
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert [(o.call_id, o.ok, o.error_code) for o in observations] == [
        ("c1", False, ac.ToolErrorCode.UNKNOWN_TOOL.value),
        ("c2", False, ac.ToolErrorCode.INVALID_ARGUMENTS.value),
        ("c3", False, ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value),
        ("c4", False, ac.ToolErrorCode.INVALID_ARGUMENTS.value),
        # A real product of another tenant is simply absent from this scope's catalogue.
        ("c5", False, ac.ToolErrorCode.TOOL_FAILURE.value),
    ]
    assert all(o.evidence_refs == () for o in observations)
    assert agent.summary(turn).effects_by_status == {s.value: 0 for s in lc.EffectStatus}


def test_a_provider_without_tool_use_or_parallel_capability_stops_before_executing(agent: Harness) -> None:
    turn, lease = agent.start()
    no_tools = ac.ProviderCapabilities(provider_name="scripted", tool_use=False)
    provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "catalog_search", query="قميص"))],
                                            capabilities=no_tools)
    outcome = agent.run(turn, lease, provider)
    assert outcome.stop_reason == ac.StopReason.UNSUPPORTED_CAPABILITY.value
    assert outcome.detail["capability"] == "tool_use" and outcome.tool_calls_used == 0

    turn2, lease2 = agent.start()
    serial = ac.ProviderCapabilities(provider_name="scripted", parallel_tool_use=False)
    provider2 = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "catalog_search", query="قميص"),
                                                       sp.tool_call("c2", "catalog_search", query="حذاء"))],
                                             capabilities=serial)
    outcome2 = agent.run(turn2, lease2, provider2)
    assert outcome2.stop_reason == ac.StopReason.UNSUPPORTED_CAPABILITY.value
    assert (outcome2.detail["capability"], outcome2.detail["requested"], outcome2.detail["allowed"]) == (
        "parallel_tool_use", 2, 1)
    assert outcome2.tool_calls_used == 0 and agent.sequences(turn2) == 0

    turn3, lease3 = agent.start()

    class Undeclared:
        capabilities = {"provider_name": "dict-not-dataclass"}

        def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:      # pragma: no cover - never reached
            raise AssertionError("the loop must not ask an undeclared provider for a step")

    outcome3 = agent.run(turn3, lease3, Undeclared())
    assert outcome3.stop_reason == ac.StopReason.UNSUPPORTED_CAPABILITY.value
    assert outcome3.detail["capability"] == "declaration" and outcome3.steps_used == 0


def test_malformed_and_blocked_provider_results_are_explicit_outcomes(agent: Harness) -> None:
    for result, expected in ((ac.ProviderInvalid("truncated"), ac.StopReason.PROVIDER_INVALID.value),
                             (ac.ProviderBlocked("policy"), ac.StopReason.PROVIDER_BLOCKED.value),
                             (ac.ProviderFailure("upstream_5xx"), ac.StopReason.PROVIDER_FAILURE.value)):
        turn, lease = agent.start()
        outcome = agent.run(turn, lease, sp.ScriptedReasoningProvider([result]))
        assert outcome.status == ac.LoopStatus.STOPPED.value and outcome.stop_reason == expected
        assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
        assert agent.progress(turn).steps_used == 1, "the attempt was debited before the provider answered"
    turn, lease = agent.start()

    class Exploding:
        capabilities = ac.ProviderCapabilities(provider_name="exploding")

        def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:
            raise RuntimeError("boom")

    outcome = agent.run(turn, lease, Exploding())
    assert outcome.stop_reason == ac.StopReason.PROVIDER_FAILURE.value
    assert outcome.detail == {"error": "RuntimeError"}
    assert agent.sequences(turn) == 0


# ── F4: complete boundary validation ─────────────────────────────────────────


def test_malformed_result_collections_execute_no_tool_and_leak_no_type_error(agent: Harness) -> None:
    """Every shape the review reproduced becomes a declared outcome, not an exception."""

    class Unserializable:
        def __repr__(self) -> str:                                 # pragma: no cover - only for a failure message
            return "<unserializable>"

    cases = {
        "none_requests": ac.ProviderToolRequests(requests=None),            # type: ignore[arg-type]
        "empty_requests": ac.ProviderToolRequests(requests=()),
        "mapping_requests": ac.ProviderToolRequests(requests={"a": 1}),     # type: ignore[arg-type]
        "none_evidence": ac.ProviderReply(ac.ReplyDraft(text="مرحبا", evidence_refs=None)),  # type: ignore[arg-type]
        "bad_payload": ac.ProviderReply(ac.ReplyDraft(text="مرحبا", payload={"x": Unserializable()})),
        "empty_reason": ac.ProviderFailure(""),
        "not_a_result": "a plain string",
    }
    for label, result in cases.items():
        turn, lease = agent.start()
        provider = sp.ScriptedReasoningProvider([result])
        outcome = agent.run(turn, lease, provider)
        assert outcome.status == ac.LoopStatus.STOPPED.value, label
        assert outcome.stop_reason == ac.StopReason.PROVIDER_INVALID.value, (label, outcome.stop_reason)
        assert outcome.tool_calls_used == 0 and agent.sequences(turn) == 0, label
        assert agent.progress(turn).steps_used == 1, (label, "the debit is preserved when output is rejected")


def test_a_bundle_mixing_a_valid_and_a_malformed_request_executes_no_tool(agent: Harness) -> None:
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        ac.ProviderToolRequests(requests=(
            ac.ToolRequest("c1", "catalog_search", {"query": "قميص"}),      # valid
            ac.ToolRequest("c2", "catalog_search", {"query": {1, 2}}),      # not serializable
        )),
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.stop_reason == ac.StopReason.PROVIDER_INVALID.value
    assert "not serializable" in outcome.detail["error"]
    assert outcome.tool_calls_used == 0
    assert "tool_observation" not in _kinds(outcome), "no tool of the bundle ran"
    assert agent.progress(turn).tool_calls_used == 0

    turn2, lease2 = agent.start()
    duplicated = sp.ScriptedReasoningProvider([ac.ProviderToolRequests(requests=(
        ac.ToolRequest("c1", "catalog_search", {"query": "قميص"}),
        ac.ToolRequest("c1", "catalog_search", {"query": "حذاء"}),
    ))])
    outcome2 = agent.run(turn2, lease2, duplicated)
    assert outcome2.stop_reason == ac.StopReason.PROVIDER_INVALID.value
    assert "distinct call ids" in outcome2.detail["error"] and outcome2.tool_calls_used == 0


# ── F5: isolation of authoritative data ──────────────────────────────────────


def test_provider_visible_schemas_and_context_cannot_change_what_is_enforced(agent: Harness) -> None:
    turn, lease = agent.start()
    tampered: Dict[str, Any] = {}

    def tamper_then_call(request: ac.ProviderRequest) -> ac.ProviderResult:
        schema = next(d for d in request.tools if d.name == "catalog_search").input_schema
        schema["properties"]["query"]["maxLength"] = 100_000          # widen what the provider was shown
        schema["properties"]["query"]["type"] = "object"
        schema["required"] = []
        request.context.inbound["text"] = "محاولة تغيير المدخل الموثوق"
        request.context.state_payload["injected"] = True
        tampered["schema"] = schema
        return sp.tools(sp.tool_call("c1", "catalog_search", query="x" * 500))

    provider = sp.ScriptedReasoningProvider([tamper_then_call, lambda request: sp.reply("تمام.")])
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=3, max_tool_calls=2)))
    observation = next(o for o in provider.requests[1].observations if o.call_id == "c1")
    assert (observation.ok, observation.error_code) == (False, ac.ToolErrorCode.INVALID_ARGUMENTS.value)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    # The authoritative schema and the stored context are untouched.
    fresh = next(d for d in agent.registry.definitions if d.name == "catalog_search")
    assert fresh.input_schema["properties"]["query"]["maxLength"] == 200
    assert fresh.input_schema["properties"]["query"]["type"] == "string"
    assert agent.snapshot(turn).state_payload.get("injected") is None
    assert agent.ledgers.foundation.list_turns(
        tenant_id=agent.tenant_a, namespace=LIVE,
        conversation_id=turn.conversation_id)[0].payload["text"] == COMPOUND_QUESTION


def test_a_provider_cannot_corrupt_the_observations_verification_reads(agent: Harness) -> None:
    turn, lease = agent.start()

    def corrupt(request: ac.ProviderRequest) -> ac.ProviderResult:
        observation = sp.observed(request, "c1")
        observation.result["products"][0]["ref"] = "product:forged"
        return sp.reply("منتج موثق.", refs=["product:forged"], commerce=True)

    provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")), corrupt],
                                            capabilities=ac.ProviderCapabilities(provider_name="scripted"))
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=2, max_tool_calls=2)))
    assert outcome.stop_reason == ac.StopReason.VERIFICATION_FAILED.value
    assert outcome.detail["problems"] == ["unknown_evidence"]
    assert agent.sequences(turn) == 0


# ── F2: enforced waits and the reservation deadline ──────────────────────────


def test_a_hanging_provider_is_abandoned_at_its_enforced_wait(agent: Harness) -> None:
    turn, lease = agent.start()
    entered = threading.Event()

    class Hanging:
        capabilities = ac.ProviderCapabilities(provider_name="hanging")

        def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:
            entered.set()
            time.sleep(30)                                            # never answers inside the wait
            raise AssertionError("unreachable")                       # pragma: no cover

    started = time.monotonic()
    outcome = agent.run(turn, lease, Hanging(),
                        loop=agent.loop(budget=ac.LoopBudget(provider_timeout_seconds=0.3, deadline_seconds=30)))
    elapsed = time.monotonic() - started
    assert entered.is_set() and elapsed < 10, "the loop enforced the wait itself"
    assert outcome.stop_reason == ac.StopReason.PROVIDER_TIMEOUT.value
    assert outcome.detail["waited_seconds"] <= 0.3
    assert agent.sequences(turn) == 0
    assert agent.progress(turn).steps_used == 1, "the abandoned attempt still consumed budget"


def test_a_tool_wait_is_capped_by_the_remaining_overall_deadline(agent: Harness) -> None:
    def slow(scope: at.ToolScope, arguments: Any) -> at.ToolResult:
        time.sleep(20)                                                # far beyond the remaining deadline
        return at.ToolResult(result={"never": "returned"}, evidence_refs=("product:slow",))

    registry = at.ToolRegistry([at.RegisteredTool(
        ac.ToolDefinition(name="slow_lookup", description="A tool that does not answer in time. Read-only.",
                          input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                          result_kind="knowledge_entry"), slow)])
    # The per-call tool limit is generous; the turn deadline is what binds.
    loop = agent.loop(registry=registry,
                      budget=ac.LoopBudget(max_steps=2, max_tool_calls=2, tool_timeout_seconds=60.0,
                                           provider_timeout_seconds=10.0, deadline_seconds=1.5))
    turn, lease = agent.start()
    captured: List[ac.ToolObservation] = []

    def after(request: ac.ProviderRequest) -> ac.ProviderResult:
        captured.extend(request.observations)
        return sp.reply("تعذّر جلب التفاصيل الآن.")

    provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "slow_lookup")), after])
    started = time.monotonic()
    outcome = loop.run_turn(tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                            turn_id=turn.turn_id, token=lease.token, provider=provider)
    elapsed = time.monotonic() - started
    assert elapsed < 10, "the wait was capped by the remaining deadline, not by the 60 s tool limit"
    assert [(o.ok, o.error_code) for o in captured] == [(False, ac.ToolErrorCode.TIMEOUT.value)] or \
        outcome.stop_reason == ac.StopReason.DEADLINE_EXCEEDED.value
    assert agent.sequences(turn) == 0


def test_the_deadline_is_rechecked_inside_the_reservation_transaction(agent: Harness) -> None:
    """A reply accepted before expiry must not reserve delivery after expiry.

    The conversation row lock is held by an independent connection across the
    deadline, so the reservation transaction waits, and the recheck happens on
    the database clock after that wait and before any write.
    """
    turn, lease = agent.start()
    holder = _RowLockHolder(agent.dsn, turn.conversation_id, hold_seconds=3.0)

    def take_lock(request: ac.ProviderRequest) -> None:
        holder.start()

    provider = sp.ScriptedReasoningProvider([sp.reply("جاهز.")], before_step=take_lock)
    loop = agent.loop(budget=ac.LoopBudget(max_steps=2, provider_timeout_seconds=10.0, deadline_seconds=1.0))
    outcome = loop.run_turn(tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                            turn_id=turn.turn_id, token=lease.token, provider=provider)
    holder.join()
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.DEADLINE_EXCEEDED.value
    assert outcome.detail["at"] == "reservation_boundary"
    assert agent.sequences(turn) == 0, "nothing was reserved after the deadline"
    assert agent.snapshot(turn).state_payload.get("reply") is None
    assert agent.open_transactions() == 0


# ── Bounded stopping ─────────────────────────────────────────────────────────


def test_repeated_tool_requests_terminate_explicitly(agent: Harness) -> None:
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.tools(sp.tool_call("c2", "catalog_search", query="قميص")),
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.stop_reason == ac.StopReason.REPEATED_TOOL_REQUEST.value
    assert outcome.detail["tool"] == "catalog_search" and outcome.detail["repeat"] == "repeat_in_invocation"
    assert outcome.tool_calls_used == 1 and agent.sequences(turn) == 0


def test_budget_exhaustion_and_cancellation_stop_without_false_success(agent: Harness) -> None:
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.tools(sp.tool_call("c2", "catalog_search", query="حذاء")),
        sp.tools(sp.tool_call("c3", "catalog_search", query="عطر")),
    ])
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=2, max_tool_calls=9)))
    assert outcome.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value
    assert outcome.detail["limit"] == "max_steps" and outcome.steps_used == 2
    assert agent.sequences(turn) == 0

    turn2, lease2 = agent.start()
    provider2 = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.tools(sp.tool_call("c2", "catalog_search", query="حذاء")),
    ])
    outcome2 = agent.run(turn2, lease2, provider2,
                         loop=agent.loop(budget=ac.LoopBudget(max_steps=4, max_tool_calls=1)))
    assert outcome2.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value
    assert outcome2.detail["limit"] == "max_tool_calls" and outcome2.tool_calls_used == 1

    turn4, lease4 = agent.start()
    cancel = {"now": False}

    def cancel_after_tool(request: ac.ProviderRequest) -> ac.ProviderResult:
        cancel["now"] = True
        return sp.reply("مرحبًا")

    provider4 = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
                                              cancel_after_tool])
    outcome4 = agent.run(turn4, lease4, provider4, cancelled=lambda: cancel["now"])
    assert outcome4.stop_reason == ac.StopReason.CANCELLED.value
    assert agent.sequences(turn4) == 0 and agent.terminal(turn4) is None


# ── F3: scope, eligibility and ownership boundaries ──────────────────────────


def test_a_turn_of_another_conversation_is_refused_before_any_work_is_read(agent: Harness) -> None:
    owned, owned_lease = agent.start()
    agent.run(owned, owned_lease, sp.ScriptedReasoningProvider([sp.reply("رد محفوظ.")]))
    assert agent.sequence(owned) is not None, "the other conversation's turn really has existing work"

    other, other_lease = agent.start()
    provider = sp.ScriptedReasoningProvider([sp.reply("لا ينبغي أن يُستدعى")])
    outcome = agent.loop().run_turn(
        tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=other.conversation_id,
        turn_id=owned.turn_id, token=other_lease.token, provider=provider)
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.TURN_NOT_IN_SCOPE.value
    assert outcome.delivery_sequence_id is None and outcome.reused_delivery is False
    assert provider.requests == [], "no reasoning happened for a turn outside the scope"
    assert agent.sequences(other) == 0
    # The mismatched call changed nothing about either conversation.
    assert agent.snapshot(other).state_payload.get(ac.AGENT_LOOP_STATE_KEY) is None


def test_a_turn_that_is_not_the_oldest_unresolved_one_never_reaches_the_provider(agent: Harness) -> None:
    first, lease = agent.start()
    second = agent.ledgers.foundation.admit_turn(
        tenant_id=agent.tenant_a, namespace=LIVE,
        conversation_ref=agent.snapshot(first).conversation_ref, channel_connection_ref=CHANNEL,
        provider_message_id=_pmid(), payload={"kind": "text", "text": "سؤال ثانٍ"})
    assert second.turn_id != first.turn_id
    provider = sp.ScriptedReasoningProvider([sp.reply("لا ينبغي أن يُستدعى")])
    outcome = agent.run(second, lease, provider)
    assert outcome.stop_reason == ac.StopReason.TURN_NOT_ELIGIBLE.value
    assert provider.requests == [] and outcome.steps_used == 0
    assert agent.sequences(second) == 0
    assert agent.progress(second) is None, "an ineligible turn writes no progress"


def test_ownership_lost_during_a_tool_call_permits_no_further_reasoning_step(agent: Harness) -> None:
    """Ownership lapses while a tool is running: the observation arrives, and the
    next reasoning step is refused before it is debited or requested."""
    turn, lease = agent.start()

    def steals_ownership_while_running(scope: at.ToolScope, arguments: Any) -> at.ToolResult:
        thief = create_engine(agent.dsn, pool_pre_ping=True)
        try:
            LedgerRepository(thief).foundation.invalidate_ownership(
                tenant_id=scope.tenant_id, namespace=scope.namespace, conversation_id=scope.conversation_id)
        finally:
            thief.dispose()
        return at.ToolResult(result={"entries": []}, evidence_refs=())

    registry = at.ToolRegistry([at.RegisteredTool(
        ac.ToolDefinition(name="slow_probe", description="A read-only probe whose call outlives the lease.",
                          input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                          result_kind="knowledge_entry"), steals_ownership_while_running)])
    steps: List[int] = []
    provider = sp.ScriptedReasoningProvider(
        [sp.tools(sp.tool_call("c1", "slow_probe")), sp.reply("لا ينبغي أن يُستدعى")],
        before_step=lambda request: steps.append(request.step_no))
    before = agent.snapshot(turn)
    outcome = agent.loop(registry=registry).run_turn(
        tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
        token=lease.token, provider=provider)
    assert steps == [1], "no second reasoning step after the lease was lost during the tool call"
    assert outcome.stop_reason == ac.StopReason.OWNERSHIP_LOST.value
    assert "tool_observation" in _kinds(outcome), "the tool did run before ownership lapsed"
    after = agent.snapshot(turn)
    # Exactly the two debits taken before the lease lapsed: the step and the tool call.
    assert after.state_revision == before.state_revision + 2
    progress = agent.progress(turn)
    assert progress is not None and (progress.steps_used, progress.tool_calls_used) == (1, 1)
    assert after.state_payload.get("reply") is None
    assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    assert agent.open_transactions() == 0


def test_ownership_lost_while_awaiting_a_result_keeps_the_debit_and_writes_nothing_more(agent: Harness) -> None:
    """The pre-attempt debit is durable; the stale result reserves and writes nothing."""
    turn, lease = agent.start(seconds=60)
    taken = threading.Event()

    def steal(request: ac.ProviderRequest) -> None:
        if request.step_no != 1 or taken.is_set():
            return
        thief = create_engine(agent.dsn, pool_pre_ping=True)
        try:
            LedgerRepository(thief).foundation.invalidate_ownership(
                tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id)
        finally:
            thief.dispose()
        taken.set()

    provider = sp.ScriptedReasoningProvider([sp.reply("مرحبًا، كيف أساعدك؟")], before_step=steal)
    debited = agent.snapshot(turn)
    outcome = agent.run(turn, lease, provider)
    assert taken.is_set()
    assert outcome.stop_reason == ac.StopReason.OWNERSHIP_LOST.value
    assert outcome.detail["rejection"] == c.RejectReason.OBSOLETE_EPOCH.value
    after = agent.snapshot(turn)
    # Exactly one write happened: the durable debit taken before the provider ran.
    assert after.state_revision == debited.state_revision + 1
    progress = agent.progress(turn)
    assert progress is not None and (progress.steps_used, progress.phase) == (1, "reasoning")
    assert after.state_payload.get("reply") is None
    assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    # Positive control: a new owner can take the turn forward.
    new_lease = agent.ledgers.foundation.claim(tenant_id=agent.tenant_a, namespace=LIVE,
                                               conversation_id=turn.conversation_id, owner_id=OWNER_B,
                                               lease_seconds=60)
    recovered = agent.run(turn, new_lease, sp.ScriptedReasoningProvider([sp.reply("أهلاً بك.")]))
    assert recovered.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert recovered.steps_used == 2, "the new owner continued the persisted budget"
    assert agent.sequences(turn) == 1


def test_ownership_lost_while_a_failure_is_being_recorded_returns_the_ownership_outcome(agent: Harness) -> None:
    for result in (ac.ProviderFailure("upstream"), ac.ProviderBlocked("policy"), ac.ProviderInvalid("truncated")):
        turn, lease = agent.start()

        def steal(request: ac.ProviderRequest) -> None:
            thief = create_engine(agent.dsn, pool_pre_ping=True)
            try:
                LedgerRepository(thief).foundation.invalidate_ownership(
                    tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id)
            finally:
                thief.dispose()

        provider = sp.ScriptedReasoningProvider([result], before_step=steal)
        outcome = agent.run(turn, lease, provider)
        assert outcome.status == ac.LoopStatus.STOPPED.value
        assert outcome.stop_reason == ac.StopReason.OWNERSHIP_LOST.value, type(result).__name__
        assert outcome.detail["original_reason"] in {ac.StopReason.PROVIDER_FAILURE.value,
                                                     ac.StopReason.PROVIDER_BLOCKED.value,
                                                     ac.StopReason.PROVIDER_INVALID.value}
        assert "stop_not_persisted" in _kinds(outcome)
        progress = agent.progress(turn)
        assert progress is not None and progress.stop_reason is None, "the stop was not written"
        assert agent.sequences(turn) == 0


# ── F1: durable accounting, concurrency and re-entry ─────────────────────────


def test_two_processes_on_the_same_turn_cannot_share_one_debit(agent: Harness) -> None:
    turn, lease = agent.start()
    args = agent.worker_args(turn, lease, "direct_reply")
    results = _run_workers([(w.run_turn_worker, (agent.dsn, f"p{i}", args)) for i in range(2)])
    assert [r["status"] for r in results] == ["ok", "ok"], results
    outcomes = [r["result"] for r in results]
    reserved = [o for o in outcomes if o["delivery_sequence_id"] is not None]
    refused = [o for o in outcomes if o["delivery_sequence_id"] is None]
    assert len(reserved) == 1 and len(refused) == 1, outcomes
    assert reserved[0]["status"] == ac.LoopStatus.PENDING_DELIVERY.value
    assert refused[0]["stop_reason"] in {ac.StopReason.CONCURRENT_INVOCATION.value,
                                         ac.StopReason.TURN_NOT_ELIGIBLE.value}, refused[0]
    assert agent.sequences(turn) == 1
    # The winner's progress survived; the loser overwrote nothing.
    progress = agent.progress(turn)
    assert progress is not None and progress.steps_used == 1
    assert progress.phase == "reply_pending_delivery"


def test_a_crash_after_the_tool_ran_keeps_the_debits_and_leaves_no_reservation(agent: Harness) -> None:
    turn, lease = agent.start()
    before = agent.snapshot(turn)
    exit_code, messages = _run_crash_worker(
        w.crash_accept_worker, (agent.dsn, "crash", agent.worker_args(turn, lease, "search_then_reply_slow")))
    assert exit_code == 9 and any(m.get("status") == "dying_before_commit" for m in messages), messages
    after = agent.snapshot(turn)
    progress = agent.progress(turn)
    assert progress is not None, "the durable debits survived the crash"
    assert (progress.steps_used, progress.tool_calls_used) == (2, 1)
    assert progress.phase == "reasoning", "the uncommitted acceptance left no reply phase"
    assert after.state_payload.get("reply") is None
    assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    assert after.state_revision > before.state_revision
    # Re-entry continues from the surviving debits and completes the turn once.
    outcome = agent.run(turn, lease, sp.ScriptedReasoningProvider([sp.reply("أهلاً بك مجددًا.")]))
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value and outcome.reused_delivery is False
    assert outcome.steps_used == 3, "the re-entry paid for its own attempt on top of the surviving debits"
    assert agent.sequences(turn) == 1


def test_re_entry_restores_the_authoritative_limits_and_cannot_enlarge_them(agent: Harness) -> None:
    turn, lease = agent.start()
    small = ac.LoopBudget(max_steps=2, max_tool_calls=2, deadline_seconds=120)
    first = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        ac.ProviderFailure("upstream"),
    ]), loop=agent.loop(budget=small))
    assert first.stop_reason == ac.StopReason.PROVIDER_FAILURE.value and first.steps_used == 2
    persisted = agent.progress(turn)
    assert persisted is not None and persisted.limits == small

    generous = ac.LoopBudget(max_steps=99, max_tool_calls=99, deadline_seconds=9999)
    second_provider = sp.ScriptedReasoningProvider([sp.reply("محاولة أخرى")])
    second = agent.run(turn, lease, second_provider, loop=agent.loop(budget=generous))
    assert second.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value, "the persisted limit still binds"
    assert second.detail["limit"] == "max_steps" and second_provider.requests == []
    resumed = next(e for e in second.events if e.kind == "resumed")
    assert resumed.detail["limits_restored"] is True and resumed.detail["requested_limits_differ"] is True
    assert agent.progress(turn).limits == small, "the caller could not enlarge the stored limits"
    assert agent.sequences(turn) == 0


def test_re_entry_reuses_the_existing_delivery_intent_and_never_creates_a_second(agent: Harness) -> None:
    turn, lease = agent.start()
    first = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="عطر")),
        lambda request: sp.reply("عطر ورد متوفر.",
                                 refs=[sp.observed(request, "c1").result["products"][0]["ref"]], commerce=True),
    ]))
    assert first.status == ac.LoopStatus.PENDING_DELIVERY.value and first.reused_delivery is False
    second_provider = sp.ScriptedReasoningProvider([sp.reply("رد مختلف تمامًا.")])
    second = agent.run(turn, lease, second_provider)
    assert second.status == ac.LoopStatus.PENDING_DELIVERY.value and second.reused_delivery is True
    assert second.delivery_sequence_id == first.delivery_sequence_id and agent.sequences(turn) == 1
    assert second_provider.requests == [], "re-entry inspects existing work before reasoning again"
    assert agent.terminal(turn) is None


def test_checkpointed_observations_and_repeat_history_survive_re_entry(agent: Harness) -> None:
    turn, lease = agent.start()
    budget = ac.LoopBudget(max_steps=4, max_tool_calls=4, deadline_seconds=300)
    first = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        ac.ProviderFailure("upstream"),
    ]), loop=agent.loop(budget=budget))
    assert first.stop_reason == ac.StopReason.PROVIDER_FAILURE.value
    persisted = agent.progress(turn)
    assert persisted is not None
    assert [o.call_id for o in persisted.observations] == ["c1"]
    assert persisted.observations[0].evidence_refs == ("product:blue_cotton_shirt",)
    assert [signature for signature, _ in persisted.executed][0].startswith("catalog_search:")
    assert [attempts for _, attempts in persisted.executed] == [1], "the original attempt is charged once"

    restored: List[ac.ToolObservation] = []

    def reuse_restored_evidence(request: ac.ProviderRequest) -> ac.ProviderResult:
        restored.extend(request.observations)
        return sp.reply("قميص قطني أزرق متوفر.", refs=["product:blue_cotton_shirt"], commerce=True)

    second = agent.run(turn, lease, sp.ScriptedReasoningProvider([reuse_restored_evidence]),
                       loop=agent.loop(budget=budget))
    assert second.status == ac.LoopStatus.PENDING_DELIVERY.value, "restored evidence still verifies"
    assert len(restored) == 1 and restored[0].restored is True
    assert restored[0].evidence_refs == ("product:blue_cotton_shirt",)


def test_duplicate_requests_inside_one_bundle_execute_no_tool(agent: Harness) -> None:
    """Distinct correlation ids do not make identical tool and argument work distinct."""
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([ac.ProviderToolRequests(requests=(
        ac.ToolRequest("c1", "catalog_search", {"query": "قميص"}),
        ac.ToolRequest("c2", "catalog_search", {"query": "قميص"}),      # same work, different call id
    ))])
    outcome = agent.run(turn, lease, provider)
    assert outcome.stop_reason == ac.StopReason.REPEATED_TOOL_REQUEST.value
    assert outcome.detail["repeat"] == "duplicate_in_bundle"
    assert (outcome.detail["call_id"], outcome.detail["first_call_id"]) == ("c2", "c1")
    assert "tool_observation" not in _kinds(outcome), "not even the first request ran"
    assert outcome.tool_calls_used == 0 and agent.sequences(turn) == 0
    progress = agent.progress(turn)
    # The reasoning attempt that produced the bundle stays paid; no tool attempt
    # is charged, and no signature is recorded for work never admitted.
    assert progress is not None and (progress.steps_used, progress.tool_calls_used) == (1, 0)
    assert progress.executed == ()


def test_a_bundle_of_distinct_requests_still_runs(agent: Harness) -> None:
    """The duplicate guard does not disturb a valid multi-request bundle."""
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص"),
                 sp.tool_call("c2", "catalog_search", query="عطر"),
                 sp.tool_call("c3", "merchant_knowledge_lookup", topic="returns")),
        lambda request: sp.reply("تمام.", refs=[request.observations[0].evidence_refs[0]], commerce=True),
    ])
    outcome = agent.run(turn, lease, provider,
                        loop=agent.loop(budget=ac.LoopBudget(max_steps=3, max_tool_calls=3)))
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert _kinds(outcome).count("tool_observation") == 3 and outcome.tool_calls_used == 3
    progress = agent.progress(turn)
    assert progress is not None and len(progress.executed) == 3
    assert {attempts for _, attempts in progress.executed} == {1}


def test_the_recovery_allowance_is_one_repeat_and_then_refused(agent: Harness) -> None:
    """Original, one later recovery, then refusal — each admitted attempt charged."""
    turn, lease = agent.start()
    budget = ac.LoopBudget(max_steps=6, max_tool_calls=6, deadline_seconds=300)
    perfume = sp.tool_call("c1", "catalog_search", query="عطر")

    agent.run(turn, lease, sp.ScriptedReasoningProvider([sp.tools(perfume), ac.ProviderFailure("upstream")]),
              loop=agent.loop(budget=budget))
    first = agent.progress(turn)
    assert first is not None and first.tool_calls_used == 1
    signature = first.executed[0][0]
    assert first.executed == ((signature, 1),), "the original attempt is charged once"

    second = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c9", "catalog_search", query="عطر")),   # same work, new correlation id
        ac.ProviderFailure("upstream again"),
    ]), loop=agent.loop(budget=budget))
    assert [e for e in second.events if e.detail.get("recovery_repeat")], "marked as a recovery repeat"
    after = agent.progress(turn)
    assert after is not None and after.tool_calls_used == 2
    assert after.executed == ((signature, 2),), "the recovery attempt is charged to the same signature"

    third_provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("cX", "catalog_search", query="عطر"))])
    third = agent.run(turn, lease, third_provider, loop=agent.loop(budget=budget))
    assert third.stop_reason == ac.StopReason.REPEATED_TOOL_REQUEST.value
    assert third.detail["repeat"] == "allowance_exhausted" and third.detail["attempts"] == 2
    exhausted = agent.progress(turn)
    assert exhausted is not None and exhausted.tool_calls_used == 2, "the refused attempt ran and charged nothing"
    assert exhausted.executed == ((signature, 2),)
    assert agent.sequences(turn) == 0


def test_different_signatures_keep_independent_allowances(agent: Harness) -> None:
    turn, lease = agent.start()
    budget = ac.LoopBudget(max_steps=6, max_tool_calls=6, deadline_seconds=300)
    agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="عطر")), ac.ProviderFailure("upstream")],
    ), loop=agent.loop(budget=budget))
    agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c2", "catalog_search", query="عطر")), ac.ProviderFailure("upstream")],
    ), loop=agent.loop(budget=budget))
    exhausted = dict(agent.progress(turn).executed)
    assert list(exhausted.values()) == [2], "the perfume signature is spent"

    # A different argument is a different signature with its own untouched allowance.
    outcome = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c3", "catalog_search", query="قميص")),
        lambda request: sp.reply("قميص قطني أزرق متوفر.",
                                 refs=[sp.observed(request, "c3").result["products"][0]["ref"]], commerce=True),
    ]), loop=agent.loop(budget=budget))
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value, "an independent signature still executes"
    charged = dict(agent.progress(turn).executed)
    assert len(charged) == 2 and sorted(charged.values()) == [1, 2]


def test_a_crash_right_after_the_tool_debit_keeps_the_identity_and_the_charge(agent: Harness) -> None:
    """The allowance is spent by the debit, even when the crash prevents proof the tool ran."""
    turn, lease = agent.start()
    args = agent.worker_args(turn, lease, "search_perfume_then_reply")
    exit_code, messages = _run_crash_worker(w.crash_after_tool_debit_worker, (agent.dsn, "crash-1", args))
    assert exit_code == 9 and any(m.get("status") == "dying_after_tool_debit" for m in messages), messages
    after = agent.progress(turn)
    assert after is not None, "the durable debit survived the crash"
    assert after.tool_calls_used == 1 and len(after.executed) == 1
    signature, attempts = after.executed[0]
    assert signature.startswith("catalog_search:") and attempts == 1
    assert after.observations == (), "the tool never ran, so there is no observation"
    assert agent.sequences(turn) == 0

    # The identity survived, so the next request for it is the one permitted recovery.
    second = agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="عطر")),
        lambda request: sp.reply("عطر ورد متوفر.",
                                 refs=[sp.observed(request, "c1").result["products"][0]["ref"]], commerce=True),
    ]), loop=agent.loop(budget=ac.LoopBudget(max_steps=6, max_tool_calls=6, deadline_seconds=300)))
    assert second.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert dict(agent.progress(turn).executed)[signature] == 2


def test_a_crash_right_after_the_recovery_debit_leaves_the_allowance_exhausted(agent: Harness) -> None:
    turn, lease = agent.start()
    args = agent.worker_args(turn, lease, "search_perfume_then_reply")
    for label in ("original", "recovery"):
        exit_code, messages = _run_crash_worker(w.crash_after_tool_debit_worker, (agent.dsn, label, args))
        assert exit_code == 9, (label, messages)
    after = agent.progress(turn)
    assert after is not None and len(after.executed) == 1
    signature, attempts = after.executed[0]
    assert attempts == ac.MAX_TOOL_ATTEMPTS_PER_SIGNATURE, "both crashes spent the allowance"
    assert after.tool_calls_used == 2

    refused = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("cZ", "catalog_search", query="عطر"))])
    outcome = agent.run(turn, lease, refused,
                        loop=agent.loop(budget=ac.LoopBudget(max_steps=8, max_tool_calls=8, deadline_seconds=300)))
    assert outcome.stop_reason == ac.StopReason.REPEATED_TOOL_REQUEST.value
    assert outcome.detail["repeat"] == "allowance_exhausted"
    assert dict(agent.progress(turn).executed)[signature] == ac.MAX_TOOL_ATTEMPTS_PER_SIGNATURE


def test_concurrent_invocations_cannot_spend_the_same_recovery_allowance_twice(agent: Harness) -> None:
    turn, lease = agent.start()
    budget = ac.LoopBudget(max_steps=8, max_tool_calls=8, deadline_seconds=300)
    agent.run(turn, lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="عطر")), ac.ProviderFailure("upstream")],
    ), loop=agent.loop(budget=budget))
    signature = agent.progress(turn).executed[0][0]
    assert dict(agent.progress(turn).executed)[signature] == 1, "one recovery remains"

    args = agent.worker_args(turn, lease, "search_perfume_then_reply")
    results = _run_workers([(w.run_turn_worker, (agent.dsn, f"r{i}", args)) for i in range(2)])
    assert [r["status"] for r in results] == ["ok", "ok"], results
    outcomes = [r["result"] for r in results]
    refused = [o for o in outcomes if o["status"] == ac.LoopStatus.STOPPED.value]
    assert len(refused) == 1, outcomes
    assert refused[0]["stop_reason"] in {ac.StopReason.CONCURRENT_INVOCATION.value,
                                         ac.StopReason.REPEATED_TOOL_REQUEST.value}, refused[0]
    final = dict(agent.progress(turn).executed)
    assert final[signature] == ac.MAX_TOOL_ATTEMPTS_PER_SIGNATURE, "the allowance was spent exactly once more"


def test_existing_pending_and_unknown_ledger_work_neither_resends_nor_completes(agent: Harness) -> None:
    turn, lease = agent.start()
    first = agent.run(turn, lease, sp.ScriptedReasoningProvider([sp.reply("تم استلام طلبك.")]))
    sequence = agent.sequence(turn)
    attempt = agent.ledgers.reserve_delivery_dispatch(tenant_id=agent.tenant_a, namespace=LIVE,
                                                      conversation_id=turn.conversation_id, token=lease.token,
                                                      sequence_id=sequence.sequence_id)
    agent.ledgers.record_delivery_receipt(tenant_id=agent.tenant_a, namespace=LIVE,
                                          conversation_id=turn.conversation_id, attempt_id=attempt.attempt_id,
                                          kind="accepted", provider_message_id="wamid." + uuid.uuid4().hex,
                                          recorded_by=OWNER_A)
    again = agent.run(turn, lease, sp.ScriptedReasoningProvider([sp.reply("رد ثانٍ")]))
    assert again.status == ac.LoopStatus.PENDING_DELIVERY.value and again.reused_delivery is True
    assert again.delivery_sequence_id == first.delivery_sequence_id
    assert again.detail["delivery_outcome"] == "accepted" and again.detail["delivery_attempt_count"] == 1
    assert agent.sequences(turn) == 1 and agent.terminal(turn) is None

    unknown_turn, unknown_lease = agent.start()
    reservation = agent.ledgers.reserve_effect(
        tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=unknown_turn.conversation_id,
        token=unknown_lease.token, turn_id=unknown_turn.turn_id,
        intent=lc.EffectIntent(action_type="order_cancel",
                               idempotency_key=lc.derive_business_key("order_cancel", "t", uuid.uuid4().hex[:8]),
                               payload={"order": "SO-9"}))
    dispatch = agent.ledgers.reserve_effect_dispatch(tenant_id=agent.tenant_a, namespace=LIVE,
                                                     conversation_id=unknown_turn.conversation_id,
                                                     token=unknown_lease.token,
                                                     effect_id=reservation.effect.effect_id)
    agent.ledgers.record_effect_result(tenant_id=agent.tenant_a, namespace=LIVE,
                                       conversation_id=unknown_turn.conversation_id, attempt_id=dispatch.attempt_id,
                                       outcome="unknown", evidence={"timeout_seconds": 30}, recorded_by=OWNER_A)
    blocked_provider = sp.ScriptedReasoningProvider([sp.reply("رد جديد")])
    outcome = agent.run(unknown_turn, unknown_lease, blocked_provider)
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.TURN_NOT_ELIGIBLE.value
    assert outcome.detail["effects_by_status"]["unknown"] == 1
    assert blocked_provider.requests == [] and agent.sequences(unknown_turn) == 0
    assert agent.terminal(unknown_turn) is None


def test_a_completed_turn_is_never_reasoned_about_again(agent: Harness) -> None:
    turn, lease = agent.start()
    agent.ledgers.finalize_turn(tenant_id=agent.tenant_a, namespace=LIVE, turn_id=turn.turn_id,
                                token=lease.token, processing_outcome="completed")
    provider = sp.ScriptedReasoningProvider([sp.reply("رد بعد الإغلاق")])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.TURN_COMPLETED.value
    assert provider.requests == [] and agent.sequences(turn) == 0


def test_every_loop_run_closes_its_transactions_and_holds_none_across_reasoning(agent: Harness) -> None:
    turn, lease = agent.start()
    observed_open: List[int] = []

    def watch(request: ac.ProviderRequest) -> None:
        observed_open.append(agent.open_transactions())

    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "merchant_knowledge_lookup", topic="returns")),
        lambda request: sp.reply("سياسة الاستبدال ١٤ يومًا.",
                                 refs=[sp.observed(request, "c1").result["entries"][0]["ref"]], commerce=True),
    ], before_step=watch)
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert observed_open == [0, 0], "no transaction is held while the provider reasons"
    assert agent.open_transactions() == 0 and agent.engine.pool.checkedout() == 0


# ── A step cut off at the output limit is recoverable, not the end of a turn ──


def test_a_truncated_step_is_told_back_and_the_finished_answer_is_accepted(agent: Harness) -> None:
    """Tenant 1, 2026-09-22 14:26Z: «أبي أشوف الخيارات» produced exactly 1024
    output tokens, ``stop_reason=max_tokens``, and the customer received
    nothing at all. Being cut off is a fact about that step, not a verdict on
    the turn: with a step left the loop hands the fact back, the model keeps
    its whole context and finishes, and the reply is delivered.
    """
    turn, lease = agent.start()
    feedback_seen: List[Tuple[int, List[str]]] = []

    def finished(request: ac.ProviderRequest) -> ac.ProviderResult:
        feedback_seen.append((request.step_no, [p.code for f in request.feedback for p in f.problems]))
        ref = sp.observed(request, "c1").result["products"][0]["ref"]
        return sp.reply("قميص قطني أزرق متوفر.", refs=[ref], commerce=True)

    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        ac.ProviderInvalid(ac.TRUNCATED_OUTPUT),
        finished,
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert feedback_seen == [(3, ["output_truncated"])]
    assert _kinds(outcome).count("output_truncated") == 1
    assert agent.sequences(turn) == 1, "the truncated step reserved nothing of its own"
    assert outcome.detail["evidence_refs"] == ["product:blue_cotton_shirt"]


def test_a_truncated_step_with_no_step_left_still_stops_rather_than_sending_half(agent: Harness) -> None:
    """The bound is the budget, not optimism. An unfinished answer is never
    sent: with nothing left to spend the turn stops and reserves no delivery.
    """
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        ac.ProviderInvalid(ac.TRUNCATED_OUTPUT),
        ac.ProviderInvalid(ac.TRUNCATED_OUTPUT),
    ], capabilities=ac.ProviderCapabilities(provider_name="scripted"))
    outcome = agent.run(turn, lease, provider,
                        loop=agent.loop(budget=ac.LoopBudget(max_steps=2, max_tool_calls=1)))
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.PROVIDER_INVALID.value
    assert agent.sequence(turn) is None and agent.sequences(turn) == 0


def test_output_that_is_invalid_for_any_other_reason_still_ends_the_turn(agent: Harness) -> None:
    """Only truncation is correctable. Malformed output is not a step that ran
    out of room, so it keeps its existing, stricter outcome."""
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        ac.ProviderInvalid("reply_arguments_not_an_object"),
        sp.reply("لن يُطلب هذا أبدًا.", commerce=False),
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.PROVIDER_INVALID.value
    assert agent.sequences(turn) == 0


# ── The customer's own words reach verification, and only theirs ────────────


def test_a_number_the_customer_wrote_survives_verification_and_is_delivered(agent: Harness) -> None:
    """End to end on PostgreSQL: the number is in the admitted turn's payload,
    the loop hands that payload to verification, and the reply naming it is
    accepted and reserved for delivery. Nothing about the order is claimed —
    the agent says it could not find it and asks. That answer is the customer's
    to receive, and a guard reading it as an invention would take it away.
    """
    turn, lease = agent.start(body="وش حال طلبي RRRD1234؟")
    provider = sp.ScriptedReasoningProvider([
        sp.reply("ما لقيت طلبًا بالرقم RRRD1234 — تتأكد لي منه؟", commerce=False),
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert agent.sequences(turn) == 1
    assert "verification_failed" not in _kinds(outcome)


def test_the_same_sentence_on_a_turn_that_never_mentioned_it_is_refused(agent: Harness) -> None:
    """The contrast that proves the inbound is what changed the outcome, not
    the wording. One identical draft, two turns: the customer who wrote the
    number gets it back, and the customer who asked about a shirt does not get
    an order number the agent produced from nowhere. Refusal is not silence —
    a step remains, the model is told, and it answers without the token.
    """
    turn, lease = agent.start(body="عندكم قميص قطني أزرق؟")
    told: List[List[str]] = []

    def second(request: ac.ProviderRequest) -> ac.ProviderResult:
        told.append([p.code for f in request.feedback for p in f.problems])
        return sp.reply("القميص القطني الأزرق متوفر.", commerce=False)

    provider = sp.ScriptedReasoningProvider([
        sp.reply("ما لقيت طلبًا بالرقم RRRD1234 — تتأكد لي منه؟", commerce=False),
        second,
    ])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert told == [["unobserved_code"]]
    assert agent.sequences(turn) == 1, "the refused draft reserved nothing of its own"
