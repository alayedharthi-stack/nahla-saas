"""What each model-bound call was actually given, observed where it happens.

This exists because "the right facts reached the model" cannot be
established from the reply, from the final conversation state, or from a
list of key names. It can only be established at the boundary where the
call is made — so that is where this records it.

Two things are recorded, from two different authorities:

* The **stage** (ordinary compose vs the recovery that follows a refused
  candidate) is declared by the branch that actually executes, through
  ``compose_stage``. It is execution evidence, not an inference from what
  the turn ended up looking like.
* The **payload facts** are read at the orchestrator adapter, from the
  very ``context_metadata`` the call carries. ``response_goal``,
  ``missing_field`` and ``delivery_address_status`` are recorded as
  given.

The accepted address's map reference is recorded as a boolean only. Its
presence is the operational fact under validation; its value is customer
data and never enters an evidence artifact.

Everything here is inert unless an internal-E2E acceptance context is
installed, and every entry point is fail-open: a measurement failure must
never change what the customer receives. Completeness is enforced later,
by the evidence layer, which refuses an address turn whose observation is
missing rather than accepting a silent gap.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from core.acceptance_execution_context import current_acceptance_context

STAGE_ORDINARY = "ordinary"
STAGE_RECOVERY = "recovery"
STAGE_UNSPECIFIED = "unspecified"
STAGES: Tuple[str, ...] = (STAGE_ORDINARY, STAGE_RECOVERY, STAGE_UNSPECIFIED)

BOUNDARY_ORCHESTRATOR_ADAPTER = "orchestrator_adapter"

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{0,96}$")


def _safe(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_VALUE.fullmatch(text) else "unsafe_value_omitted"


@dataclass(frozen=True)
class ModelBoundCall:
    """One call to the model, as the boundary saw it."""

    call_index: int
    stage: str
    collection_field: str
    turn_ref: str
    observed_at: str
    response_goal: str
    missing_field: str
    delivery_address_status: str
    has_accepted_maps_reference: bool
    stage_declared: bool
    outcome_recorded: bool = False
    candidate_present: bool = False
    compose_source: str = ""
    fallback_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class _StageMarker:
    stage: str
    collection_field: str
    turn_ref: str


class _Recorder:
    """One turn's records, held by reference rather than by value.

    The composer reaches the provider through ``asyncio.to_thread`` inside
    ``asyncio.wait_for``, and the recovery leg adds another task boundary.
    Python copies the context INTO those children, so a ``ContextVar.set``
    performed there updates the child's copy and is invisible to the
    parent that reads the evidence — which is how every real model-bound
    call went unrecorded while the synchronous unit tests passed.

    Holding a mutable recorder in the ContextVar fixes that: the child
    inherits the same object, and appending to it is visible everywhere.
    A lock keeps concurrent workers honest, and sealing at the end of the
    turn means a call that completes after its ``wait_for`` gave up is
    counted as late instead of landing in the next turn's evidence.
    """

    __slots__ = ("_calls", "_lock", "_sealed", "_late")

    def __init__(self) -> None:
        self._calls: List[ModelBoundCall] = []
        self._lock = threading.Lock()
        self._sealed = False
        self._late = 0

    def append(self, record: ModelBoundCall) -> int:
        with self._lock:
            if self._sealed:
                self._late += 1
                return -1
            index = len(self._calls)
            self._calls.append(replace(record, call_index=index))
            return index

    def update(self, index: int, **changes: Any) -> None:
        with self._lock:
            if index < 0 or index >= len(self._calls):
                self._late += 1
                return
            self._calls[index] = replace(self._calls[index], **changes)

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def snapshot(self) -> Tuple[ModelBoundCall, ...]:
        with self._lock:
            return tuple(self._calls)

    @property
    def late_arrivals(self) -> int:
        with self._lock:
            return self._late


_STAGE: ContextVar[Optional[_StageMarker]] = ContextVar(
    "nahla_acceptance_compose_stage",
    default=None,
)
_RECORDER: ContextVar[Optional[_Recorder]] = ContextVar(
    "nahla_acceptance_model_bound_recorder",
    default=None,
)


@contextmanager
def model_bound_observation() -> Iterator[None]:
    """Scope one turn's observations.

    Each turn gets its own recorder, so two turns — and two concurrent
    tasks — never share records, while everything inside one turn writes
    to the same object however many threads it crosses.
    """
    recorder = _Recorder()
    token = _RECORDER.set(recorder)
    try:
        yield
    finally:
        recorder.seal()
        _RECORDER.reset(token)


@contextmanager
def compose_stage(
    stage: str,
    *,
    collection_field: str = "",
    turn_ref: str = "",
) -> Iterator[None]:
    """Declare which stage the enclosed compose belongs to.

    Wrapped around the call site that runs, so the stage is what executed
    rather than what the final state suggests. Inert without an
    acceptance context.
    """
    if current_acceptance_context() is None:
        yield
        return
    marker = _StageMarker(
        stage=stage if stage in STAGES else STAGE_UNSPECIFIED,
        collection_field=_safe(collection_field),
        turn_ref=_safe(turn_ref),
    )
    token = _STAGE.set(marker)
    try:
        yield
    finally:
        _STAGE.reset(token)


def recorded_model_bound_calls() -> Tuple[ModelBoundCall, ...]:
    recorder = _RECORDER.get()
    return recorder.snapshot() if recorder is not None else ()


def late_model_bound_arrivals() -> int:
    """Calls that completed after their turn was sealed."""
    recorder = _RECORDER.get()
    return recorder.late_arrivals if recorder is not None else 0


def _address_facts(context_metadata: Any) -> Dict[str, Any]:
    meta = dict(context_metadata or {}) if isinstance(context_metadata, Mapping) else {}
    brain_state = meta.get("brain_state")
    state = dict(brain_state) if isinstance(brain_state, Mapping) else {}
    known = state.get("known_facts")
    facts = dict(known) if isinstance(known, Mapping) else {}
    return {
        "response_goal": _safe(state.get("response_goal")),
        "missing_field": _safe(facts.get("missing_field")),
        "delivery_address_status": _safe(facts.get("delivery_address_status")),
        # Presence only. The URL itself is customer data.
        "has_accepted_maps_reference": bool(
            str(facts.get("google_maps_url") or "").strip()
        ),
    }


def observe_model_bound_call(*, context_metadata: Any) -> int:
    """Record one model-bound call. Returns its index, or -1 when inert."""
    try:
        recorder = _RECORDER.get()
        if recorder is None or current_acceptance_context() is None:
            return -1
        marker = _STAGE.get()
        record = ModelBoundCall(
            call_index=0,
            stage=marker.stage if marker is not None else STAGE_UNSPECIFIED,
            collection_field=marker.collection_field if marker is not None else "",
            turn_ref=marker.turn_ref if marker is not None else "",
            observed_at=BOUNDARY_ORCHESTRATOR_ADAPTER,
            stage_declared=marker is not None,
            **_address_facts(context_metadata),
        )
        return recorder.append(record)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — measurement must never change the reply
        return -1


def record_model_bound_outcome(
    call_index: int,
    *,
    candidate_present: bool,
    compose_source: str = "",
    fallback_reason: str = "",
) -> None:
    """Attach what the boundary returned to the call that was recorded."""
    try:
        recorder = _RECORDER.get()
        if recorder is None or call_index < 0:
            return
        recorder.update(
            call_index,
            outcome_recorded=True,
            candidate_present=bool(candidate_present),
            compose_source=_safe(compose_source),
            fallback_reason=_safe(fallback_reason),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — measurement must never change the reply
        return


__all__ = [
    "BOUNDARY_ORCHESTRATOR_ADAPTER",
    "STAGES",
    "STAGE_ORDINARY",
    "STAGE_RECOVERY",
    "STAGE_UNSPECIFIED",
    "ModelBoundCall",
    "compose_stage",
    "late_model_bound_arrivals",
    "model_bound_observation",
    "observe_model_bound_call",
    "record_model_bound_outcome",
    "recorded_model_bound_calls",
]
