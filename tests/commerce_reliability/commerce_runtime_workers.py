"""Spawned-process workers for the commerce runtime foundation tests.

Each worker opens its own PostgreSQL connection and calls the real
:class:`core.commerce_runtime.repositories.CommerceRuntimeRepository`; nothing
here re-implements foundation logic. The barrier only lines the processes up
so their calls overlap. Results are reported as plain dictionaries.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import os
from typing import Any, Callable, Dict

from tests.commerce_reliability.pg_workers import _bootstrap


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _repository(dsn: str):
    from sqlalchemy import create_engine  # noqa: PLC0415

    from core.commerce_runtime.repositories import CommerceRuntimeRepository  # noqa: PLC0415

    engine = create_engine(dsn, pool_pre_ping=True)
    return engine, CommerceRuntimeRepository(engine)


def _token(raw: Dict[str, Any]):
    from core.commerce_runtime.contracts import OwnershipToken  # noqa: PLC0415

    return OwnershipToken(owner_id=raw["owner_id"], fence=int(raw["fence"]), epoch=int(raw["epoch"]))


def _run(out, label: str, fn: Callable[[], Any]) -> None:
    from core.commerce_runtime import contracts as c  # noqa: PLC0415

    try:
        out.put({"label": label, "status": "ok", "result": _jsonable(fn())})
    except c.OwnershipRejected as exc:
        out.put({"label": label, "status": "rejected", "reason": exc.reason.value,
                 "snapshot": _jsonable(exc.snapshot)})
    except c.TerminalAlreadyRecorded as exc:
        out.put({"label": label, "status": "terminal_exists", "existing": _jsonable(exc.existing)})
    except c.AdmissionConflict as exc:
        out.put({"label": label, "status": "admission_conflict", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent, never hidden
        out.put({"label": label, "status": f"error:{type(exc).__name__}:{exc}"})


def admit_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.admit_turn(**args))
    finally:
        engine.dispose()


def claim_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.claim(**args))
    finally:
        engine.dispose()


def commit_state_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    engine, repo = _repository(dsn)
    try:
        kwargs = dict(args)
        kwargs["token"] = _token(kwargs["token"])
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.commit_state(**kwargs))
    finally:
        engine.dispose()


def terminal_worker(dsn: str, label: str, args: Dict[str, Any], barrier, out) -> None:
    _bootstrap()
    from core.commerce_runtime.contracts import StateTransition  # noqa: PLC0415

    engine, repo = _repository(dsn)
    try:
        kwargs = dict(args)
        kwargs["token"] = _token(kwargs["token"])
        transition = kwargs.pop("state_transition", None)
        if transition is not None:
            kwargs["state_transition"] = StateTransition(
                expected_revision=int(transition["expected_revision"]), payload=transition["payload"],
            )
        barrier.wait(timeout=60)
        _run(out, label, lambda: repo.record_terminal(**kwargs))
    finally:
        engine.dispose()


def crash_before_commit_worker(dsn: str, label: str, args: Dict[str, Any], out) -> None:
    """Run the real atomic terminal + state transition, then die before COMMIT."""
    _bootstrap()
    from core.commerce_runtime.contracts import StateTransition  # noqa: PLC0415

    engine, repo = _repository(dsn)
    kwargs = dict(args)
    kwargs["token"] = _token(kwargs["token"])
    transition = kwargs.pop("state_transition")
    kwargs["state_transition"] = StateTransition(
        expected_revision=int(transition["expected_revision"]), payload=transition["payload"],
    )

    def _die() -> None:
        out.put({"label": label, "status": "dying_before_commit"})
        out.close()
        out.join_thread()
        os._exit(9)

    _run(out, label, lambda: repo.record_terminal(**kwargs, _fault_before_commit=_die))
    engine.dispose()
