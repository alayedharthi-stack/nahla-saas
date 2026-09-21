"""Scoped, recorded failure injection for internal-E2E validation.

A scenario that says it injected a provider error has to have injected
one. Carrying the label into the evidence without installing anything
produced identical PASS records for ``none``, ``provider_error``,
``provider_timeout`` and ``guard_boundary`` — four different claims, one
undifferentiated result, and no mechanism actually confirmed.

So the label arms a real fault at a real seam:

* ``provider_error`` and ``provider_timeout`` fire at the orchestrator
  adapter, immediately before the provider call the composer makes —
  the same boundary the observation is taken at, so the call is recorded
  as attempted and then as failed.
* ``guard_boundary`` fires at the OrderFlowV2 outbound guard call, which
  is the failure the recovery path exists for.

Both are inert unless an internal-E2E acceptance context is installed
AND a scenario armed them, and both RECORD that they fired, so evidence
can distinguish "injected and the mechanism behaved" from "injected and
nothing happened" — which is a failed injection, not a passing turn.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Dict, Iterator, Optional

from core.acceptance_execution_context import current_acceptance_context

INJECTION_NONE = "none"
INJECTION_PROVIDER_ERROR = "provider_error"
INJECTION_PROVIDER_TIMEOUT = "provider_timeout"
INJECTION_GUARD_BOUNDARY = "guard_boundary"

SITE_ADAPTER = "orchestrator_adapter"
SITE_GUARD = "order_flow_v2_outbound_guard"

_SITE_FOR_KIND: Dict[str, str] = {
    INJECTION_PROVIDER_ERROR: SITE_ADAPTER,
    INJECTION_PROVIDER_TIMEOUT: SITE_ADAPTER,
    INJECTION_GUARD_BOUNDARY: SITE_GUARD,
}


class InjectedProviderFailure(RuntimeError):
    """A provider failure this run asked for, at the provider boundary."""


class InjectedGuardFailure(RuntimeError):
    """A guard-boundary failure this run asked for."""


@dataclass
class _Armed:
    kind: str
    site: str
    fired: int = 0


_ARMED: ContextVar[Optional[_Armed]] = ContextVar(
    "nahla_acceptance_failure_injection",
    default=None,
)


@contextmanager
def arm_failure_injection(kind: str) -> Iterator[None]:
    """Arm one fault for the duration of a turn. ``none`` arms nothing."""
    site = _SITE_FOR_KIND.get(str(kind or INJECTION_NONE))
    if site is None or current_acceptance_context() is None:
        yield
        return
    token = _ARMED.set(_Armed(kind=str(kind), site=site))
    try:
        yield
    finally:
        _ARMED.reset(token)


def injection_state() -> Dict[str, object]:
    """What was armed and whether it actually fired."""
    armed = _ARMED.get()
    if armed is None:
        return {"kind": INJECTION_NONE, "site": "", "fired": 0, "armed": False}
    return {
        "kind": armed.kind,
        "site": armed.site,
        "fired": int(armed.fired),
        "armed": True,
    }


def _fire(expected_site: str) -> Optional[_Armed]:
    armed = _ARMED.get()
    if armed is None or armed.site != expected_site:
        return None
    if current_acceptance_context() is None:
        return None
    armed.fired += 1
    return armed


def maybe_inject_provider_failure() -> None:
    """Raise the armed provider fault at the adapter boundary, if any."""
    armed = _fire(SITE_ADAPTER)
    if armed is None:
        return
    if armed.kind == INJECTION_PROVIDER_TIMEOUT:
        raise asyncio.TimeoutError("internal_e2e_injected_provider_timeout")
    raise InjectedProviderFailure("internal_e2e_injected_provider_error")


def maybe_inject_guard_failure() -> None:
    """Raise the armed guard fault at the OrderFlowV2 guard boundary, if any."""
    if _fire(SITE_GUARD) is None:
        return
    raise InjectedGuardFailure("internal_e2e_injected_guard_boundary_failure")


__all__ = [
    "INJECTION_GUARD_BOUNDARY",
    "INJECTION_NONE",
    "INJECTION_PROVIDER_ERROR",
    "INJECTION_PROVIDER_TIMEOUT",
    "InjectedGuardFailure",
    "InjectedProviderFailure",
    "SITE_ADAPTER",
    "SITE_GUARD",
    "arm_failure_injection",
    "injection_state",
    "maybe_inject_guard_failure",
    "maybe_inject_provider_failure",
]
