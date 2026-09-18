"""Acceptance-only fault injection for knowledge retrieval.

K13 has to prove something a passing run cannot otherwise show: that a
knowledge-base failure does not take the catalog answer down with it, and that
the knowledge half of the reply becomes an explicit "not documented" rather
than an invention.  Run 1 could never prove it — the case expected a timeout
and nothing in the environment could cause one.

The fault is deliberately narrow:

* it affects **knowledge retrieval only**, never the catalog, the order tools
  or anything else;
* it is armed for **one case** and disarmed the moment that case ends, by a
  context manager that releases in a ``finally``;
* it refuses to arm unless the process is an isolated Phase 2.7B acceptance
  database **and** the INTERNAL_E2E synthetic channel is enabled **and** the
  mode was declared by the versioned matrix;
* production never calls the arming function, and if it somehow did, the guard
  rejects it — there is no environment in which the public API can reach this.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from services.commerce_v2_phase_2_7b_environment import ISOLATED_ACCEPTANCE_ENV

INTERNAL_E2E_ENABLED_ENV = "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED"
FAULT_TIMEOUT = "timeout"
FAULT_ERROR = "error"
SUPPORTED_FAULT_MODES = frozenset({FAULT_TIMEOUT, FAULT_ERROR})


class AcceptanceFaultError(RuntimeError):
    """Fail-closed guard for acceptance-only fault injection."""


class InjectedKnowledgeFault(RuntimeError):
    """Raised inside knowledge retrieval while a fault is armed."""

    def __init__(self, mode: str) -> None:
        super().__init__(f"phase_2_7b_injected_knowledge_fault:{mode}")
        self.mode = str(mode)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def fault_injection_permitted(env: Mapping[str, str] | None = None) -> bool:
    """Both guards must hold: isolated acceptance database and internal channel."""
    values = env if env is not None else os.environ
    return _truthy(values.get(ISOLATED_ACCEPTANCE_ENV)) and _truthy(
        values.get(INTERNAL_E2E_ENABLED_ENV)
    )


@contextmanager
def knowledge_fault(
    mode: str, *, env: Mapping[str, str] | None = None
) -> Iterator[str]:
    """Arm one knowledge-retrieval fault for the duration of one case."""
    resolved = str(mode or "").strip().lower()
    if resolved not in SUPPORTED_FAULT_MODES:
        raise AcceptanceFaultError("phase_2_7b_fault_mode_unsupported")
    if not fault_injection_permitted(env):
        raise AcceptanceFaultError("phase_2_7b_fault_injection_not_permitted")

    from modules.ai.commerce_agent_v2 import knowledge_retrieval

    previous = knowledge_retrieval.active_knowledge_fault()
    if previous:
        raise AcceptanceFaultError("phase_2_7b_fault_already_armed")
    knowledge_retrieval.arm_knowledge_fault(resolved)
    try:
        yield resolved
    finally:
        # Disarmed even if the case raises, so a fault can never survive into
        # the next case.
        knowledge_retrieval.arm_knowledge_fault(None)


__all__ = [
    "AcceptanceFaultError",
    "FAULT_ERROR",
    "FAULT_TIMEOUT",
    "InjectedKnowledgeFault",
    "SUPPORTED_FAULT_MODES",
    "fault_injection_permitted",
    "knowledge_fault",
]
