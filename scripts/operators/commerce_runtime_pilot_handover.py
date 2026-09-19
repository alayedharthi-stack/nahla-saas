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

    2. QUIESCE    Confirm every replica is running the draining configuration —
                  redeploy or restart them, and check each one actually
                  restarted. This job cannot see other processes and does not
                  claim to; see below.

    3. VERIFY     NAHLA_COMMERCE_RUNTIME_HANDOVER_INGRESS_QUIESCED=INGRESS_DRAINED_ALL_REPLICAS \
                      python -m scripts.operators.commerce_runtime_pilot_handover
                  Exits 0 only when every allowlisted tenant has nothing in
                  flight **and** the operator has attested step 2. Any other
                  exit means it is not safe to stop; fix and run it again.

    4. STOP       COMMERCE_RUNTIME_PILOT_ENABLED=false
                  Only after step 3 exits 0.

What this job can and cannot prove
----------------------------------
It reads one database. That tells it what work is **recorded** as outstanding,
and it is authoritative about that. It tells it nothing about what another
process is about to do: a replica that has selected a route but not yet written
its admission is invisible here, and the draining flag is per-process, so a
replica that has not picked up the new configuration is still admitting new
turns while this reports zero.

That gap is not something a count can close, so the job does not pretend to
close it. It requires the operator to attest that ingress is quiesced across
every replica, and reports ``UNVERIFIED_INGRESS`` without that attestation even
when every count is zero. The attestation is the operator's statement about the
fleet; the counts are the database's statement about the work.

An emergency stop is still available and is still one flag: setting
``COMMERCE_RUNTIME_PILOT_ENABLED=false`` stops this process from taking new
turns and from recovering. It is not instantaneous across a fleet, and it does
not stop a send already in flight. This job then reports exactly what that left
behind, so the decision is taken against evidence either way.

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
RESULT_UNVERIFIED_INGRESS = "UNVERIFIED_INGRESS"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_SETTLED = 0
EXIT_IN_FLIGHT = 1
EXIT_USAGE = 2
EXIT_FAILED = 3

# The operator's statement that every replica is running the draining
# configuration. This job cannot observe other processes; it will not report
# "safe to stop" on a fleet nobody has said is quiesced.
QUIESCED_ENV = "NAHLA_COMMERCE_RUNTIME_HANDOVER_INGRESS_QUIESCED"
QUIESCED_TOKEN = "INGRESS_DRAINED_ALL_REPLICAS"


def emit(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def result(marker: str, **observations: Any) -> None:
    body = " ".join(f"{name}={value!r}" for name, value in observations.items())
    emit(f"RESULT={marker} {body}".rstrip())


def ingress_quiesced(environ: Optional[dict] = None) -> bool:
    """Whether the operator has attested that no replica is still admitting."""
    env = environ if environ is not None else os.environ
    return str(env.get(QUIESCED_ENV, "") or "").strip() == QUIESCED_TOKEN


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
    from core.commerce_runtime import recovery

    current = mode()
    emit(f"mode={current}")
    if current != "draining":
        # In ``on`` the counts are a moving target, and in ``off`` the pilot has
        # already stopped recovering whatever they show. Neither can answer
        # "is it safe to stop": one is too early, the other is too late.
        result(RESULT_FAILED_PRECONDITION, reason=f"mode_is_{current}_not_draining",
               next_step=("set COMMERCE_RUNTIME_PILOT_DRAINING=true with "
                          "COMMERCE_RUNTIME_PILOT_ENABLED=true, then run this again"))
        return EXIT_USAGE

    tenants = configured_tenants()
    if not tenants:
        result(RESULT_FAILED_PRECONDITION, reason="no_tenant_allowlist",
               hint="COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST names the tenants to check")
        return EXIT_USAGE
    if len(tenants) > recovery.MAX_TENANTS_CONSIDERED:
        # Never inspect a subset and report on the whole: the tenants left out
        # are exactly the ones whose work would be abandoned unseen.
        result(RESULT_FAILED_PRECONDITION, reason="tenant_allowlist_too_large",
               configured=len(tenants), inspected_maximum=recovery.MAX_TENANTS_CONSIDERED)
        return EXIT_USAGE
    emit(f"tenants={','.join(str(t) for t in tenants)}")

    try:
        states = observe(tenants)
    except Exception as exc:  # noqa: BLE001 - an unreadable database is never 'settled'
        result(RESULT_FAILED, error=type(exc).__name__)
        return EXIT_FAILED

    for state in states:
        emit(" ".join(f"{key}={value}" for key, value in state.as_log_fields().items()))

    if not recovery.handover_settled(states):
        result(RESULT_IN_FLIGHT,
               open_turns=sum(s.open_turns for s in states),
               reserved_undispatched=sum(s.reserved_undispatched for s in states),
               unresolved_attempts=sum(s.unresolved_attempts for s in states),
               unknown_outcomes=sum(s.unknown_outcomes for s in states),
               next_step="keep draining and run this again")
        return EXIT_IN_FLIGHT

    if not ingress_quiesced():
        # Every recorded count is zero. That is the database's answer, and it is
        # not the whole question.
        result(RESULT_UNVERIFIED_INGRESS, tenants=len(states),
               reason="no attestation that every replica is draining",
               next_step=f"{QUIESCED_ENV}={QUIESCED_TOKEN} once every replica runs "
                         f"the draining configuration, then run this again")
        return EXIT_IN_FLIGHT

    result(RESULT_SETTLED, tenants=len(states), ingress_quiesced_by="operator_attestation",
           next_step="COMMERCE_RUNTIME_PILOT_ENABLED=false")
    return EXIT_SETTLED


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main(sys.argv[1:]))
