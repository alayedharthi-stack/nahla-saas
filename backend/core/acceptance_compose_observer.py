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
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

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


_STAGE: ContextVar[Optional[_StageMarker]] = ContextVar(
    "nahla_acceptance_compose_stage",
    default=None,
)
_CALLS: ContextVar[Tuple[ModelBoundCall, ...]] = ContextVar(
    "nahla_acceptance_model_bound_calls",
    default=(),
)
_OBSERVING: ContextVar[bool] = ContextVar(
    "nahla_acceptance_model_bound_observing",
    default=False,
)


@contextmanager
def model_bound_observation() -> Iterator[None]:
    """Scope one turn's observations. Context-scoped, so turns are isolated."""
    calls_token = _CALLS.set(())
    observing_token = _OBSERVING.set(True)
    try:
        yield
    finally:
        _OBSERVING.reset(observing_token)
        _CALLS.reset(calls_token)


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
    return _CALLS.get()


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
        if not _OBSERVING.get() or current_acceptance_context() is None:
            return -1
        marker = _STAGE.get()
        calls = _CALLS.get()
        record = ModelBoundCall(
            call_index=len(calls),
            stage=marker.stage if marker is not None else STAGE_UNSPECIFIED,
            collection_field=marker.collection_field if marker is not None else "",
            turn_ref=marker.turn_ref if marker is not None else "",
            observed_at=BOUNDARY_ORCHESTRATOR_ADAPTER,
            stage_declared=marker is not None,
            **_address_facts(context_metadata),
        )
        _CALLS.set((*calls, record))
        return record.call_index
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
        if call_index < 0:
            return
        calls = _CALLS.get()
        if call_index >= len(calls):
            return
        updated = replace(
            calls[call_index],
            outcome_recorded=True,
            candidate_present=bool(candidate_present),
            compose_source=_safe(compose_source),
            fallback_reason=_safe(fallback_reason),
        )
        _CALLS.set(tuple(updated if i == call_index else c for i, c in enumerate(calls)))
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
    "model_bound_observation",
    "observe_model_bound_call",
    "record_model_bound_outcome",
    "recorded_model_bound_calls",
]
