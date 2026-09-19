"""Spawned-process workers for the dormant agent loop tests.

Each worker opens its own PostgreSQL connection and runs the real
:class:`core.commerce_runtime.agent_loop.AgentLoop` with the real ledger
repository and a scripted provider; nothing here re-implements loop logic.
The barrier only lines processes up so their calls overlap. Results are
reported as plain dictionaries.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict

from tests.commerce_reliability.commerce_runtime_workers import _jsonable, _token
from tests.commerce_reliability.pg_workers import _bootstrap


def _loop(dsn: str, budget: Dict[str, Any] | None = None, tenants: Dict[str, int] | None = None):
    from sqlalchemy import create_engine  # noqa: PLC0415

    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415
    from core.commerce_runtime.agent_loop import AgentLoop  # noqa: PLC0415
    from core.commerce_runtime.ledgers import LedgerRepository  # noqa: PLC0415
    from tests.commerce_reliability.agent_fixture_catalog import build_registry  # noqa: PLC0415

    engine = create_engine(dsn, pool_pre_ping=True)
    limits = ac.LoopBudget(**budget) if budget else ac.LoopBudget()
    scoped = tenants or {}
    registry = build_registry(int(scoped.get("a", 1)), int(scoped.get("b", 2)), int(scoped.get("c", 3)))
    return engine, AgentLoop(LedgerRepository(engine), registry, budget=limits)


def _scripted(script: str):
    """Build one of the fixed scripts by name; scripts are code, never model output."""
    from core.commerce_runtime import agent_scripted as sp  # noqa: PLC0415

    if script == "search_then_reply":
        return sp.ScriptedReasoningProvider([
            sp.tools(sp.tool_call("c1", "catalog_search", query="shirt")),
            lambda request: sp.reply(
                "قميص قطني أزرق متوفر.",
                refs=[r for o in request.observations for r in o.evidence_refs][:1], commerce=True),
        ])
    if script == "direct_reply":
        return sp.ScriptedReasoningProvider([sp.reply("أهلاً! كيف أقدر أساعدك؟")])
    if script == "search_then_reply_slow":
        # One tool call, then a reply: used by the crash worker so a durable
        # tool debit exists before the process dies at the accept boundary.
        return sp.ScriptedReasoningProvider([
            sp.tools(sp.tool_call("c1", "catalog_search", query="قميص")),
            lambda request: sp.reply("تمام.", refs=[r for o in request.observations for r in o.evidence_refs][:1],
                                     commerce=True),
        ])
    raise AssertionError(f"unknown script {script}")


def _outcome(outcome) -> Dict[str, Any]:
    return {
        "status": outcome.status, "stop_reason": outcome.stop_reason, "turn_id": outcome.turn_id,
        "delivery_sequence_id": outcome.delivery_sequence_id, "reused_delivery": outcome.reused_delivery,
        "state_revision": outcome.state_revision, "steps_used": outcome.steps_used,
        "tool_calls_used": outcome.tool_calls_used,
        "events": [{"kind": e.kind, "detail": _jsonable(e.detail)} for e in outcome.events],
        "detail": _jsonable(outcome.detail),
    }


def _run(out, label: str, fn: Callable[[], Any]) -> None:
    from core.commerce_runtime import contracts as c  # noqa: PLC0415

    try:
        result = fn()
    except c.OwnershipRejected as exc:
        out.put({"label": label, "status": "rejected", "reason": exc.reason.value})
    except c.CommerceRuntimeError as exc:
        out.put({"label": label, "status": "error", "error": type(exc).__name__, "message": str(exc)})
    else:
        out.put({"label": label, "status": "ok", "result": result})


def run_turn_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    """Run one complete loop turn in its own process."""
    _bootstrap()
    engine, loop = _loop(dsn, args.get("budget"), args.get("tenants"))
    provider = _scripted(args["script"])
    kwargs = {k: v for k, v in args.items() if k in {"tenant_id", "namespace", "conversation_id", "turn_id"}}
    kwargs["token"] = _token(args["token"])
    if barrier is not None:
        barrier.wait(timeout=60)
    try:
        _run(out, label, lambda: _outcome(loop.run_turn(**kwargs, provider=provider)))
    finally:
        engine.dispose()


def crash_accept_worker(dsn: str, label: str, args: Dict[str, Any], out) -> None:
    """Run the loop up to the atomic accept, then die before COMMIT."""
    _bootstrap()
    engine, loop = _loop(dsn, args.get("budget"), args.get("tenants"))
    provider = _scripted(args["script"])
    kwargs = {k: v for k, v in args.items() if k in {"tenant_id", "namespace", "conversation_id", "turn_id"}}
    kwargs["token"] = _token(args["token"])

    def _die() -> None:
        out.put({"label": label, "status": "dying_before_commit"})
        out.close()
        out.join_thread()
        os._exit(9)

    _run(out, label, lambda: _outcome(loop.run_turn(**kwargs, provider=provider, _fault_before_commit=_die)))
    engine.dispose()


__all__ = ["crash_accept_worker", "run_turn_worker"]
