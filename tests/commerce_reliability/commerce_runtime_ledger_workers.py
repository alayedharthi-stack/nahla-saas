"""Spawned-process workers for the commerce runtime ledger tests.

Each worker opens its own PostgreSQL connection and calls the real
:class:`core.commerce_runtime.ledgers.LedgerRepository`; nothing here
re-implements ledger logic. The barrier only lines the processes up so their
calls overlap. Results are reported as plain dictionaries.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict

from tests.commerce_reliability.commerce_runtime_workers import _jsonable, _token
from tests.commerce_reliability.pg_workers import _bootstrap


def _repository(dsn: str):
    from sqlalchemy import create_engine  # noqa: PLC0415

    from core.commerce_runtime.ledgers import LedgerRepository  # noqa: PLC0415

    engine = create_engine(dsn, pool_pre_ping=True)
    return engine, LedgerRepository(engine)


def _effect_intent(raw: Dict[str, Any]):
    from core.commerce_runtime.ledger_contracts import EffectIntent  # noqa: PLC0415

    return EffectIntent(action_type=raw["action_type"], idempotency_key=raw["idempotency_key"],
                        payload=raw.get("payload") or {})


def _delivery_intent(raw: Dict[str, Any]):
    from core.commerce_runtime.ledger_contracts import DeliveryIntent  # noqa: PLC0415

    return DeliveryIntent(kind=raw["kind"], payload=raw.get("payload") or {})


def _run(out, label: str, fn: Callable[[], Any]) -> None:
    from core.commerce_runtime import contracts as c  # noqa: PLC0415
    from core.commerce_runtime import ledger_contracts as lc  # noqa: PLC0415

    try:
        out.put({"label": label, "status": "ok", "result": _jsonable(fn())})
    except c.OwnershipRejected as exc:
        out.put({"label": label, "status": "rejected", "reason": exc.reason.value})
    except lc.DispatchBlocked as exc:
        out.put({"label": label, "status": f"dispatch_blocked:{exc.reason.value}",
                 "effect": _jsonable(exc.effect), "open_attempt": _jsonable(exc.open_attempt)})
    except lc.DeliveryDispatchBlocked as exc:
        out.put({"label": label, "status": f"delivery_dispatch_blocked:{exc.reason.value}"})
    except lc.EffectConflict as exc:
        out.put({"label": label, "status": f"effect_conflict:{exc.reason}"})
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent, never hidden
        out.put({"label": label, "status": f"error:{type(exc).__name__}:{exc}"})


def reserve_effect_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        kwargs = dict(args)
        kwargs["token"] = _token(kwargs["token"])
        kwargs["intent"] = _effect_intent(kwargs["intent"])
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.reserve_effect(**kwargs))
    finally:
        engine.dispose()


def reserve_effect_dispatch_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        kwargs = dict(args)
        kwargs["token"] = _token(kwargs["token"])
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.reserve_effect_dispatch(**kwargs))
    finally:
        engine.dispose()


def reserve_delivery_dispatch_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        kwargs = dict(args)
        kwargs["token"] = _token(kwargs["token"])
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.reserve_delivery_dispatch(**kwargs))
    finally:
        engine.dispose()


def _die_factory(out, label: str) -> Callable[[], None]:
    def _die() -> None:
        out.put({"label": label, "status": "dying_before_commit"})
        out.close()
        out.join_thread()
        os._exit(9)
    return _die


def crash_decision_worker(dsn: str, label: str, args: Dict[str, Any], out) -> None:
    """Run the real atomic decision commit, then die before COMMIT."""
    _bootstrap()
    from core.commerce_runtime.contracts import StateTransition  # noqa: PLC0415

    engine, repo = _repository(dsn)
    kwargs = dict(args)
    kwargs["token"] = _token(kwargs["token"])
    transition = kwargs.pop("state_transition", None)
    if transition is not None:
        kwargs["state_transition"] = StateTransition(
            expected_revision=int(transition["expected_revision"]), payload=transition["payload"],
        )
    kwargs["effect_intents"] = [_effect_intent(i) for i in kwargs.get("effect_intents") or []]
    if kwargs.get("delivery_intent") is not None:
        kwargs["delivery_intent"] = _delivery_intent(kwargs["delivery_intent"])
    _run(out, label, lambda: repo.commit_turn_decision(**kwargs, _fault_before_commit=_die_factory(out, label)))
    engine.dispose()


def crash_finalize_worker(dsn: str, label: str, args: Dict[str, Any], out) -> None:
    """Run the real ledger-derived terminal recording, then die before COMMIT."""
    _bootstrap()
    engine, repo = _repository(dsn)
    kwargs = dict(args)
    kwargs["token"] = _token(kwargs["token"])
    _run(out, label, lambda: repo.finalize_turn(**kwargs, _fault_before_commit=_die_factory(out, label)))
    engine.dispose()
