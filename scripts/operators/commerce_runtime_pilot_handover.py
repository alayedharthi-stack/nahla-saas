"""Operator check: is it safe to switch the commerce-runtime pilot off?

Switching the pilot off is not, by itself, a rollback. The flag decides whether
*new* turns are taken; it says nothing about the turns the runtime already
admitted. Flipping it while work is in flight abandons three things at once:

* an admitted turn with no terminal — a customer owed an answer or an honest
  record, and no path left to give either;
* a reply the loop reserved and nothing dispatched — composed, verified, never
  sent;
* a send with no receipt — an outcome nobody established, which the ledger will
  never resolve on its own.

So the supported rollback is a handover, in three steps:

    1. DRAIN      COMMERCE_RUNTIME_PILOT_DRAINING=true
                  (leave COMMERCE_RUNTIME_PILOT_ENABLED=true)
                  New turns go to the legacy path from that moment. The turns
                  this runtime already admitted stay reachable and are finished
                  as their inbound messages are redelivered.

    2. VERIFY     python -m scripts.operators.commerce_runtime_pilot_handover
                  Exits 0 only when every allowlisted tenant has nothing in
                  flight. Any other exit means work remains; run it again.

    3. STOP       COMMERCE_RUNTIME_PILOT_ENABLED=false
                  Only after step 2 exits 0.

An emergency stop is still available and is still one flag: setting
``COMMERCE_RUNTIME_PILOT_ENABLED=false`` immediately stops everything, including
recovery. This job then reports exactly what that left behind, so the decision
is taken against evidence either way.

Read-only. It sends nothing, writes nothing and changes no configuration.
"""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend"),
              os.path.join(_REPO_ROOT, "database")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

LOG_PREFIX = "[COMMERCE_RUNTIME_HANDOVER]"

RESULT_SETTLED = "SETTLED"
RESULT_IN_FLIGHT = "IN_FLIGHT"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_SETTLED = 0
EXIT_IN_FLIGHT = 1
EXIT_USAGE = 2
EXIT_FAILED = 3


def emit(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def result(marker: str, **observations: Any) -> None:
    body = " ".join(f"{name}={value!r}" for name, value in observations.items())
    emit(f"RESULT={marker} {body}".rstrip())


def configured_tenants(environ: Optional[dict] = None) -> List[int]:
    """The pilot's own allowlist. Nothing outside it is ever inspected."""
    from core.commerce_runtime import pilot_guard as pg

    env = environ if environ is not None else os.environ
    raw = str(env.get(pg.ENV_TENANT_ALLOWLIST, "") or "").strip()
    tenants = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError:
            continue
        if value > 0:
            tenants.append(value)
    return sorted(set(tenants))


def mode(environ: Optional[dict] = None) -> str:
    """``on``, ``draining`` or ``off`` — what the configuration currently says."""
    from core.commerce_runtime import pilot_guard as pg

    env = environ if environ is not None else os.environ

    def flag(name: str) -> bool:
        return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}

    if not flag(pg.ENV_ENABLED):
        return "off"
    return "draining" if flag(pg.ENV_DRAINING) else "on"


def observe(tenants: Sequence[int]) -> tuple:
    from core.commerce_runtime import recovery

    return recovery.handover_state(tenant_ids=list(tenants))


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv
    current = mode()
    emit(f"mode={current}")
    tenants = configured_tenants()
    if not tenants:
        result(RESULT_FAILED_PRECONDITION, reason="no_tenant_allowlist",
               hint="COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST names the tenants to check")
        return EXIT_USAGE
    emit(f"tenants={','.join(str(t) for t in tenants)}")
    if current == "on":
        emit("note=the pilot is still taking new turns; set "
             "COMMERCE_RUNTIME_PILOT_DRAINING=true before relying on this count")

    try:
        states = observe(tenants)
    except Exception as exc:  # noqa: BLE001 - an unreadable database is never 'settled'
        result(RESULT_FAILED, error=type(exc).__name__)
        return EXIT_FAILED

    for state in states:
        emit(" ".join(f"{k}={v}" for k, v in state.as_log_fields().items()))

    from core.commerce_runtime import recovery

    if recovery.handover_settled(states):
        result(RESULT_SETTLED, tenants=len(states),
               next_step="COMMERCE_RUNTIME_PILOT_ENABLED=false")
        return EXIT_SETTLED
    result(RESULT_IN_FLIGHT,
           open_turns=sum(s.open_turns for s in states),
           reserved_undispatched=sum(s.reserved_undispatched for s in states),
           unresolved_attempts=sum(s.unresolved_attempts for s in states),
           next_step="keep draining and run this again")
    return EXIT_IN_FLIGHT


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main(sys.argv[1:]))
