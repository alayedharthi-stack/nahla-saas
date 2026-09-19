"""Dormant agent loop core, proven on real PostgreSQL.

Every test runs against a disposable database created for this module and
migrated with the repository's own Alembic chain to revision ``0109``. The
reasoning provider is always a deterministic script and the tools always read
an in-memory fixture catalogue: no model, commerce, payment or messaging
provider is called, and nothing is ever sent. Concurrency and crash cases use
independent spawned processes with their own connections. Requires
``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and that skip is reported, never counted.

These tests prove *orchestration*: control flow, tool authorization,
verification feedback, bounded stopping, ownership revalidation, atomic
hand-off to the delivery ledger and re-entry. They establish no live model
quality, no provider compatibility, no WhatsApp delivery and no customer
readiness.
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


# ── Harness ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Harness:
    dsn: str
    engine: Any
    ledgers: LedgerRepository
    registry: at.ToolRegistry
    tenant_a: int
    tenant_b: int

    def loop(self, *, budget: Optional[ac.LoopBudget] = None, clock=None) -> AgentLoop:
        return AgentLoop(self.ledgers, self.registry, budget=budget, clock=clock)

    def start(self, *, tenant: Optional[int] = None, text_body: str = "عندكم قمصان؟",
              owner: str = OWNER_A, seconds: int = 60) -> Tuple[c.AdmittedTurn, c.Lease]:
        tenant_id = tenant or self.tenant_a
        turn = self.ledgers.foundation.admit_turn(
            tenant_id=tenant_id, namespace=LIVE, conversation_ref=_ref(), channel_connection_ref=CHANNEL,
            provider_message_id=_pmid(), payload={"kind": "text", "text": text_body})
        lease = self.ledgers.foundation.claim(tenant_id=tenant_id, namespace=LIVE,
                                              conversation_id=turn.conversation_id, owner_id=owner,
                                              lease_seconds=seconds)
        return turn, lease

    def run(self, turn: c.AdmittedTurn, lease: c.Lease, provider: Any, *, loop: Optional[AgentLoop] = None,
            tenant: Optional[int] = None, cancelled=None) -> ac.LoopOutcome:
        return (loop or self.loop()).run_turn(
            tenant_id=tenant or self.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
            turn_id=turn.turn_id, token=lease.token, provider=provider, cancelled=cancelled)

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

    def summary(self, turn: c.AdmittedTurn) -> lc.TurnLedgerSummary:
        return self.ledgers.turn_ledger_summary(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id)

    def terminal(self, turn: c.AdmittedTurn) -> Optional[c.TerminalRecord]:
        return self.ledgers.foundation.get_terminal(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id)

    def progress(self, turn: c.AdmittedTurn) -> Optional[ac.LoopProgress]:
        payload = self.snapshot(turn).state_payload
        return ac.LoopProgress.from_payload(payload.get(ac.AGENT_LOOP_STATE_KEY), turn_id=turn.turn_id)

    def open_transactions(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state IN ('idle in transaction', 'idle in transaction (aborted)')")).scalar())

    def worker_args(self, turn: c.AdmittedTurn, lease: c.Lease, script: str, **extra) -> Dict[str, Any]:
        args = {"tenant_id": self.tenant_a, "namespace": LIVE, "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id, "token": dataclasses.asdict(lease.token), "script": script,
                "tenants": {"a": self.tenant_a, "b": self.tenant_b}}
        args.update(extra)
        return args


@pytest.fixture(scope="module")
def agent(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        tenant_a, tenant_b = _seed_tenant(engine, "A"), _seed_tenant(engine, "B")
        yield Harness(dsn=dsn, engine=engine, ledgers=LedgerRepository(engine),
                      registry=build_registry(tenant_a, tenant_b), tenant_a=tenant_a, tenant_b=tenant_b)
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _kinds(outcome: ac.LoopOutcome) -> List[str]:
    return [e.kind for e in outcome.events]


class _FakeClock:
    """A monotonic clock a test advances explicitly; no wall-clock timing assumptions."""

    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


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
        return sp.reply(f"{product['name']} متوفر بسعر {product['price']} {product['currency']}.",
                        refs=[product["ref"]], commerce=True)

    provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")), second_step])
    outcome = agent.run(turn, lease, provider)

    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value and outcome.stop_reason is None
    assert (outcome.steps_used, outcome.tool_calls_used, outcome.reused_delivery) == (2, 1, False)
    assert _kinds(outcome) == ["provider_step", "tool_observation", "provider_step", "reply_accepted",
                               "delivery_intent_reserved"]
    assert seen == [(2, ["c1"])], "the second step must see the first step's observation"
    # One durable delivery intent, reserved and never dispatched; nothing was sent.
    sequence = agent.sequence(turn)
    assert sequence is not None and agent.sequences(turn) == 1
    assert (sequence.attempt_count, sequence.outcome, sequence.turn_id) == (0, "pending", turn.turn_id)
    assert outcome.delivery_sequence_id == sequence.sequence_id
    summary = agent.summary(turn)
    assert (summary.transport_outcome, summary.customer_reach) == ("not_attempted", "not_reached")
    assert agent.terminal(turn) is None, "generating a reply is not completing the turn"
    # The reply and the loop's progress are in the conversation's versioned state.
    snapshot = agent.snapshot(turn)
    assert snapshot.state_revision == 1
    assert snapshot.state_payload["reply"]["evidence_refs"] == ["product:blue_cotton_shirt"]
    progress = agent.progress(turn)
    assert progress is not None
    assert (progress.phase, progress.steps_used, progress.tool_calls_used) == ("reply_pending_delivery", 2, 1)
    assert agent.open_transactions() == 0


def test_different_observations_lead_to_different_next_decisions(agent: Harness) -> None:
    """The same script branches on what the tool actually returned, and the observation is the real one."""
    outcomes: Dict[str, Tuple[ac.LoopOutcome, List[ac.ProviderRequest]]] = {}
    for label, query, expected_ref in (("in_stock", "قميص", "product:blue_cotton_shirt"),
                                       ("out_of_stock", "حذاء", "product:white_sneaker")):
        turn, lease = agent.start()

        def branch(request: ac.ProviderRequest) -> ac.ProviderResult:
            observation = sp.observed(request, "c1")
            assert observation is not None and observation.ok
            product = observation.result["products"][0]
            if product["in_stock"]:
                return sp.reply(f"{product['name']} متوفر.", refs=[product["ref"]], commerce=True)
            return sp.tools(sp.tool_call("c2", "merchant_knowledge_lookup", topic="shipping"))

        def after_knowledge(request: ac.ProviderRequest) -> ac.ProviderResult:
            observation = sp.observed(request, "c2")
            assert observation is not None and observation.ok
            entry = observation.result["entries"][0]
            product_ref = sp.observed(request, "c1").result["products"][0]["ref"]
            return sp.reply(f"غير متوفر حاليًا. {entry['text']}", refs=[product_ref, entry["ref"]], commerce=True)

        provider = sp.ScriptedReasoningProvider(
            [sp.tools(sp.tool_call("c1", "catalog_search", query=query)), branch, after_knowledge])
        outcomes[label] = (agent.run(turn, lease, provider), provider.requests)
        assert agent.sequence(turn) is not None

    in_stock, in_stock_requests = outcomes["in_stock"]
    out_of_stock, out_requests = outcomes["out_of_stock"]
    assert (in_stock.steps_used, in_stock.tool_calls_used) == (2, 1)
    assert (out_of_stock.steps_used, out_of_stock.tool_calls_used) == (3, 2), "the branch took a second tool call"
    assert in_stock.detail["evidence_refs"] == ["product:blue_cotton_shirt"]
    assert out_of_stock.detail["evidence_refs"] == ["product:white_sneaker", "kb:shipping"]
    # The observations the provider received are the real tool results, not echoes of its request.
    assert in_stock_requests[1].observations[0].result["products"][0]["in_stock"] is True
    assert out_requests[1].observations[0].result["products"][0]["in_stock"] is False
    assert out_requests[2].observations[1].result["entries"][0]["ref"] == "kb:shipping"


def test_a_direct_reply_finishes_without_an_unnecessary_tool_call(agent: Harness) -> None:
    turn, lease = agent.start(text_body="السلام عليكم")
    provider = sp.ScriptedReasoningProvider([sp.reply("وعليكم السلام! كيف أقدر أساعدك اليوم؟")])
    outcome = agent.run(turn, lease, provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert (outcome.steps_used, outcome.tool_calls_used) == (1, 0)
    assert "tool_observation" not in _kinds(outcome)
    assert agent.sequence(turn) is not None and agent.sequences(turn) == 1


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
        # A real product of the *other* tenant is simply absent from this scope's catalogue.
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
    assert outcome2.detail == {"capability": "parallel_tool_use", "requested": 2, "allowed": 1}
    assert outcome2.tool_calls_used == 0 and agent.sequences(turn2) == 0


def test_malformed_and_blocked_provider_results_are_explicit_outcomes(agent: Harness) -> None:
    for result, expected in ((ac.ProviderInvalid("truncated"), ac.StopReason.PROVIDER_INVALID.value),
                             (ac.ProviderBlocked("policy"), ac.StopReason.PROVIDER_BLOCKED.value),
                             (ac.ProviderFailure("upstream_5xx"), ac.StopReason.PROVIDER_FAILURE.value)):
        turn, lease = agent.start()
        outcome = agent.run(turn, lease, sp.ScriptedReasoningProvider([result]))
        assert outcome.status == ac.LoopStatus.STOPPED.value and outcome.stop_reason == expected
        assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    # A provider that raises is a failure outcome, not a crash of the loop.
    turn, lease = agent.start()

    class Exploding:
        capabilities = ac.ProviderCapabilities(provider_name="exploding")

        def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:
            raise RuntimeError("boom")

    outcome = agent.run(turn, lease, Exploding())
    assert outcome.stop_reason == ac.StopReason.PROVIDER_FAILURE.value
    assert outcome.detail == {"error": "RuntimeError"}
    assert agent.sequences(turn) == 0


# ── Bounded stopping ─────────────────────────────────────────────────────────


def test_repeated_tool_requests_terminate_explicitly(agent: Harness) -> None:
    turn, lease = agent.start()
    same = sp.tools(sp.tool_call("c1", "catalog_search", query="قميص"))
    provider = sp.ScriptedReasoningProvider([same, sp.tools(sp.tool_call("c2", "catalog_search", query="قميص"))])
    outcome = agent.run(turn, lease, provider)
    assert outcome.stop_reason == ac.StopReason.REPEATED_TOOL_REQUEST.value
    assert outcome.detail == {"tool": "catalog_search", "repeats": 2}
    assert outcome.tool_calls_used == 1 and agent.sequences(turn) == 0


def test_tool_timeout_is_an_observation_and_the_loop_stays_honest(agent: Harness) -> None:
    def slow(scope: at.ToolScope, arguments: Any) -> at.ToolResult:
        time.sleep(2.0)
        return at.ToolResult(result={"never": "returned"}, evidence_refs=("product:slow",))

    registry = at.ToolRegistry([at.RegisteredTool(
        ac.ToolDefinition(name="slow_lookup", description="A tool that does not answer in time.",
                          input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                          result_kind="knowledge_entry"), slow)])
    loop = AgentLoop(agent.ledgers, registry, budget=ac.LoopBudget(max_steps=2, max_tool_calls=2,
                                                                   tool_timeout_seconds=0.2))
    turn, lease = agent.start()
    captured: List[ac.ToolObservation] = []

    def after(request: ac.ProviderRequest) -> ac.ProviderResult:
        captured.extend(request.observations)
        return sp.reply("تعذّر جلب التفاصيل الآن.")

    provider = sp.ScriptedReasoningProvider([sp.tools(sp.tool_call("c1", "slow_lookup")), after])
    outcome = loop.run_turn(tenant_id=agent.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                            turn_id=turn.turn_id, token=lease.token, provider=provider)
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
    assert [(o.ok, o.error_code) for o in captured] == [(False, ac.ToolErrorCode.TIMEOUT.value)]
    assert captured[0].evidence_refs == (), "a discarded result contributes no evidence"


def test_budget_exhaustion_and_deadline_and_cancellation_stop_without_false_success(agent: Harness) -> None:
    # Step budget: the provider keeps asking for tools it never used before.
    turn, lease = agent.start()
    provider = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.tools(sp.tool_call("c2", "catalog_search", query="حذاء")),
        sp.tools(sp.tool_call("c3", "catalog_search", query="عطر")),
    ])
    outcome = agent.run(turn, lease, provider, loop=agent.loop(budget=ac.LoopBudget(max_steps=2, max_tool_calls=9)))
    assert outcome.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value
    assert outcome.detail == {"limit": "max_steps", "steps_used": 2} and outcome.steps_used == 2
    assert agent.sequences(turn) == 0

    # Tool-call budget.
    turn2, lease2 = agent.start()
    provider2 = sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        sp.tools(sp.tool_call("c2", "catalog_search", query="حذاء")),
    ])
    outcome2 = agent.run(turn2, lease2, provider2,
                         loop=agent.loop(budget=ac.LoopBudget(max_steps=4, max_tool_calls=1)))
    assert outcome2.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value
    assert outcome2.detail == {"limit": "max_tool_calls"} and outcome2.tool_calls_used == 1

    # Deadline, on a controlled clock advanced by the provider itself: it "thinks" past the deadline.
    clock = _FakeClock()
    turn3, lease3 = agent.start()
    slow_loop = agent.loop(budget=ac.LoopBudget(deadline_seconds=60.0), clock=clock)
    thinking_long = sp.ScriptedReasoningProvider([sp.reply("مرحبًا")],
                                                 before_step=lambda request: clock.advance(61.0))
    outcome3 = agent.run(turn3, lease3, thinking_long, loop=slow_loop)
    assert outcome3.stop_reason == ac.StopReason.DEADLINE_EXCEEDED.value
    assert agent.sequences(turn3) == 0

    # Cancellation between steps.
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


# ── Ownership across awaits (independent connections) ────────────────────────


def test_ownership_lost_while_awaiting_a_result_prevents_state_and_delivery_writes(agent: Harness) -> None:
    """A second owner takes over on its own connection while the provider is 'thinking'."""
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
    before = agent.snapshot(turn)
    outcome = agent.run(turn, lease, provider)
    assert taken.is_set()
    assert outcome.status == ac.LoopStatus.STOPPED.value
    assert outcome.stop_reason == ac.StopReason.OWNERSHIP_LOST.value
    assert outcome.detail["rejection"] == c.RejectReason.OBSOLETE_EPOCH.value
    after = agent.snapshot(turn)
    assert (after.state_revision, after.state_payload) == (before.state_revision, before.state_payload)
    assert agent.progress(turn) is None, "stale work writes no progress"
    assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    assert agent.open_transactions() == 0


def test_two_processes_running_the_same_turn_produce_one_delivery_sequence(agent: Harness) -> None:
    turn, lease = agent.start()
    args = agent.worker_args(turn, lease, "direct_reply")
    results = _run_workers([(w.run_turn_worker, (agent.dsn, f"p{i}", args)) for i in range(2)])
    assert [r["status"] for r in results] == ["ok", "ok"], results
    sequence_ids = {r["result"]["delivery_sequence_id"] for r in results}
    assert len(sequence_ids) == 1 and agent.sequences(turn) == 1
    assert sorted(r["result"]["reused_delivery"] for r in results) == [False, True]
    assert all(r["result"]["status"] == ac.LoopStatus.PENDING_DELIVERY.value for r in results)


# ── Crash and re-entry ───────────────────────────────────────────────────────


def test_crash_before_the_atomic_accept_leaves_no_state_and_no_delivery_sequence(agent: Harness) -> None:
    turn, lease = agent.start()
    before = agent.snapshot(turn)
    exit_code, messages = _run_crash_worker(w.crash_accept_worker,
                                            (agent.dsn, "crash", agent.worker_args(turn, lease, "direct_reply")))
    assert exit_code == 9 and any(m.get("status") == "dying_before_commit" for m in messages), messages
    after = agent.snapshot(turn)
    assert (after.state_revision, after.state_payload) == (before.state_revision, before.state_payload)
    assert agent.sequences(turn) == 0 and agent.terminal(turn) is None
    # Re-entry after the crash completes the turn exactly once.
    outcome = agent.run(turn, lease, sp.ScriptedReasoningProvider([sp.reply("أهلاً بك مجددًا.")]))
    assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value and outcome.reused_delivery is False
    assert agent.sequences(turn) == 1


def test_re_entry_reuses_the_existing_delivery_intent_and_the_persisted_budget(agent: Harness) -> None:
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
    assert second.steps_used == 0 and agent.terminal(turn) is None
    # The persisted budget is adopted, not reset: a stopped run continues the same counters.
    other, other_lease = agent.start()
    stopped = agent.run(other, other_lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
        ac.ProviderFailure("upstream"),
    ]), loop=agent.loop(budget=ac.LoopBudget(max_steps=3, max_tool_calls=3)))
    assert stopped.stop_reason == ac.StopReason.PROVIDER_FAILURE.value and stopped.steps_used == 2
    resumed = agent.run(other, other_lease, sp.ScriptedReasoningProvider([
        sp.tools(sp.tool_call("c2", "catalog_search", query="حذاء")),
        sp.tools(sp.tool_call("c3", "catalog_search", query="عطر")),
    ]), loop=agent.loop(budget=ac.LoopBudget(max_steps=3, max_tool_calls=3)))
    assert resumed.stop_reason == ac.StopReason.BUDGET_EXHAUSTED.value
    assert resumed.steps_used == 3, "the resumed run continued the persisted step count"
    assert agent.sequences(other) == 0


def test_existing_pending_and_unknown_ledger_work_neither_resends_nor_completes(agent: Harness) -> None:
    # A delivery sequence that was already dispatched and accepted: no second send, no completion here.
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

    # An effect attempt whose outcome is unknown: the loop stops instead of reasoning or completing.
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
