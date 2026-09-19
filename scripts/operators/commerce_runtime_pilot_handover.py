"""Operator procedure: hand the commerce-runtime pilot back, safely.

Switching the pilot off is not a rollback. The flag decides whether *this
process* takes new turns; it says nothing about the turns the runtime already
admitted, nothing about the other replicas, and nothing about a send already in
flight. Flipping it while work is outstanding abandons four things at once:

* an admitted turn with no terminal — a customer owed an answer or an honest
  record, and no path left to give either;
* a reply the loop reserved and nothing dispatched;
* a send with no receipt;
* a send whose recorded outcome is ``unknown`` — which is not evidence it did
  not arrive.

So the handover runs against a **shared barrier** in the database, which every
replica reads and this job writes, and each step is a command:

    1. DRAIN    python -m scripts.operators.commerce_runtime_pilot_handover drain
                Closes the barrier for every allowlisted tenant, fleet-wide,
                from that instant. Affected inbounds are *buffered* — recorded
                durably, answered by nobody. They are not released to the legacy
                path: the runtime being handed over from may still have a send
                in flight, and a second answer is the thing a handover must not
                produce.

    2. CONVERGE Let every replica read the new barrier. A worker records the
                generation it is running the first time it evaluates a route
                after the change, so convergence is *observed* here rather than
                asserted. ``status`` names the workers still behind or silent.

    3. STATUS   python -m ... commerce_runtime_pilot_handover status
                Shows, per tenant: barrier state and generation, worker
                convergence, outstanding work including unknown outcomes, and
                buffered inbounds awaiting disposition.

    4. DISPOSE  python -m ... commerce_runtime_pilot_handover dispose \\
                    --note "replayed by hand on 2026-09-19" --by "<operator>"
                Buffered work is never acknowledged and dropped. Settlement is
                blocked until each entry carries a disposition.

    5. SETTLE   python -m ... commerce_runtime_pilot_handover settle
                Exits 0 only when the barrier is draining, every live worker is
                on the current generation, every count is zero, and nothing
                buffered is undisposed. It writes the evidence snapshot *before*
                anything reopens, so what the decision rested on survives it.

    6. STOP     COMMERCE_RUNTIME_PILOT_ENABLED=false
                Only after step 5 exits 0.

    (later)     python -m ... commerce_runtime_pilot_handover reopen
                Admits new work again on a fresh generation, keeping the audit
                trail. Run it only when the pilot is being turned back on.

What this proves, and what it does not
--------------------------------------
Convergence is evidence from the workers themselves, not a statement that they
were restarted. Counts are the database's account of recorded work. Neither can
see a request already on the wire to the provider, which is why an ``unknown``
outcome blocks settlement rather than ageing out of it: elapsed time and a
cancelled wait are not proof of non-delivery, and nothing here treats them as
such.

If any of it cannot be established the job stays blocked and says which part.

Read-only except for the barrier itself. It sends no message, answers no
customer and changes no runtime configuration.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend"),
              os.path.join(_REPO_ROOT, "database")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

LOG_PREFIX = "[COMMERCE_RUNTIME_HANDOVER]"

RESULT_SETTLED = "SETTLED"
RESULT_BLOCKED = "BLOCKED"
RESULT_DRAINING = "DRAINING"
RESULT_REPORTED = "REPORTED"
RESULT_DISPOSED = "DISPOSED"
RESULT_REOPENED = "REOPENED"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_FAILED = 3

COMMANDS = ("status", "drain", "dispose", "settle", "reopen")


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
    """``on``, ``draining`` or ``off`` — what this process's flags say.

    The flags still gate this process. The barrier is what gates the fleet, and
    the two are reported separately so a disagreement is visible.
    """
    from core.commerce_runtime import pilot_guard as pg

    env = environ if environ is not None else os.environ

    def flag(name: str) -> bool:
        return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}

    if not flag(pg.ENV_ENABLED):
        return "off"
    return "draining" if flag(pg.ENV_DRAINING) else "on"


def session() -> Any:
    from database.session import SessionLocal  # noqa: PLC0415

    return SessionLocal()


def observe(tenants: Sequence[int]) -> tuple:
    from core.commerce_runtime import recovery

    return recovery.handover_state(tenant_ids=list(tenants))


def inspect(db: Any, tenants: Sequence[int]) -> List[Dict[str, Any]]:
    """Barrier, convergence, counts and buffered work, per tenant."""
    from core.commerce_runtime import handover

    states = {state.tenant_id: state for state in observe(tenants)}
    report = []
    for tenant_id in tenants:
        barrier = handover.read_barrier(db, tenant_id=tenant_id)
        work = states.get(tenant_id)
        report.append({
            "tenant_id": tenant_id,
            "barrier": barrier,
            "convergence": barrier.convergence(),
            "work": work,
            "undisposed": barrier.undisposed_buffered,
        })
    return report


def describe(entry: Dict[str, Any]) -> None:
    barrier, work = entry["barrier"], entry["work"]
    convergence = entry["convergence"]
    emit(f"tenant={entry['tenant_id']} barrier={barrier.state} "
         f"generation={barrier.generation} converged={convergence['converged']} "
         f"on_generation={convergence['on_generation']} behind={convergence['behind']} "
         f"unknown_disposition={convergence['unknown_disposition']}")
    if work is not None:
        emit(" ".join(f"{key}={value}" for key, value in work.as_log_fields().items()))
    emit(f"tenant={entry['tenant_id']} buffered={len(barrier.buffered)} "
         f"undisposed={len(entry['undisposed'])}")


def blockers_for(entry: Dict[str, Any]) -> List[str]:
    """Every concrete reason this tenant is not settled. Never a bare 'no'."""
    from core.commerce_runtime import handover

    barrier, work, convergence = entry["barrier"], entry["work"], entry["convergence"]
    reasons: List[str] = []
    if barrier.state != handover.STATE_DRAINING:
        reasons.append(f"barrier_is_{barrier.state}_not_draining")
    if not convergence["converged"]:
        if convergence["behind"]:
            reasons.append(f"workers_behind:{','.join(convergence['behind'])}")
        if convergence["unknown_disposition"]:
            reasons.append(
                f"workers_not_seen_since_drain:{','.join(convergence['unknown_disposition'])}")
        if not convergence["on_generation"]:
            reasons.append("no_worker_has_reported_this_generation")
    if work is None:
        reasons.append("work_counts_unavailable")
    else:
        for field in ("open_turns", "reserved_undispatched", "unresolved_attempts",
                      "unknown_outcomes"):
            count = getattr(work, field)
            if count:
                reasons.append(f"{field}={count}")
    if entry["undisposed"]:
        reasons.append(f"buffered_awaiting_disposition={len(entry['undisposed'])}")
    return reasons


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_status(db: Any, tenants: Sequence[int], _args: Any) -> int:
    for entry in inspect(db, tenants):
        describe(entry)
        reasons = blockers_for(entry)
        emit(f"tenant={entry['tenant_id']} blockers={reasons or ['none']}")
    result(RESULT_REPORTED, tenants=len(list(tenants)))
    return EXIT_OK


def cmd_drain(db: Any, tenants: Sequence[int], _args: Any) -> int:
    from core.commerce_runtime import handover

    opened = []
    for tenant_id in tenants:
        barrier = handover.open_drain(db, tenant_id=tenant_id)
        opened.append(barrier.generation)
        emit(f"tenant={tenant_id} barrier=draining generation={barrier.generation}")
    result(RESULT_DRAINING, tenants=len(opened), generations=opened,
           next_step="let every replica evaluate one route, then run 'status'")
    return EXIT_OK


def cmd_dispose(db: Any, tenants: Sequence[int], args: Any) -> int:
    from core.commerce_runtime import handover

    if not str(getattr(args, "note", "") or "").strip():
        result(RESULT_FAILED_PRECONDITION, reason="disposition_note_required",
               hint="--note says what was actually done with the buffered inbounds")
        return EXIT_USAGE
    by = str(getattr(args, "by", "") or "").strip() or "unnamed_operator"
    total = 0
    for tenant_id in tenants:
        disposed = handover.dispose_buffered(db, tenant_id=tenant_id,
                                             disposition=str(args.note), by=by)
        total += disposed
        emit(f"tenant={tenant_id} disposed={disposed}")
    result(RESULT_DISPOSED, entries=total, by=by)
    return EXIT_OK


def cmd_settle(db: Any, tenants: Sequence[int], _args: Any) -> int:
    from core.commerce_runtime import handover

    entries = inspect(db, tenants)
    blocked: Dict[int, List[str]] = {}
    for entry in entries:
        describe(entry)
        reasons = blockers_for(entry)
        if reasons:
            blocked[entry["tenant_id"]] = reasons
    if blocked:
        result(RESULT_BLOCKED, blockers=blocked,
               next_step="resolve each reason above and run 'settle' again")
        return EXIT_BLOCKED

    for entry in entries:
        work, barrier = entry["work"], entry["barrier"]
        evidence = {
            "settled_generation": barrier.generation,
            "convergence": entry["convergence"],
            "work": work.as_log_fields() if work is not None else None,
            "buffered_total": len(barrier.buffered),
            "buffered_dispositions": [e.get("disposition") for e in barrier.buffered],
        }
        handover.record_settlement(db, tenant_id=entry["tenant_id"], evidence=evidence)
        emit(f"tenant={entry['tenant_id']} evidence_recorded=true")
    result(RESULT_SETTLED, tenants=len(entries),
           next_step="COMMERCE_RUNTIME_PILOT_ENABLED=false")
    return EXIT_OK


def cmd_reopen(db: Any, tenants: Sequence[int], _args: Any) -> int:
    from core.commerce_runtime import handover

    for tenant_id in tenants:
        barrier = handover.read_barrier(db, tenant_id=tenant_id)
        if barrier.state != handover.STATE_SETTLED:
            result(RESULT_FAILED_PRECONDITION, tenant_id=tenant_id,
                   reason=f"barrier_is_{barrier.state}_not_settled",
                   hint="reopening before settlement would discard the evidence")
            return EXIT_USAGE
    for tenant_id in tenants:
        reopened = handover.reopen(db, tenant_id=tenant_id)
        emit(f"tenant={tenant_id} barrier=open generation={reopened.generation}")
    result(RESULT_REOPENED, tenants=len(list(tenants)))
    return EXIT_OK


_HANDLERS = {"status": cmd_status, "drain": cmd_drain, "dispose": cmd_dispose,
             "settle": cmd_settle, "reopen": cmd_reopen}


def parse_args(argv: Optional[Sequence[str]] = None) -> Any:
    parser = argparse.ArgumentParser(prog="commerce_runtime_pilot_handover",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="status", choices=COMMANDS)
    parser.add_argument("--note", default="", help="what was done with the buffered inbounds")
    parser.add_argument("--by", default="", help="who did it")
    return parser.parse_args(list(argv if argv is not None else []))


def main(argv: Optional[Sequence[str]] = None) -> int:
    from core.commerce_runtime import recovery

    args = parse_args(argv)
    emit(f"command={args.command} process_mode={mode()}")

    tenants = configured_tenants()
    if not tenants:
        result(RESULT_FAILED_PRECONDITION, reason="no_tenant_allowlist",
               hint="COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST names the tenants to hand over")
        return EXIT_USAGE
    if len(tenants) > recovery.MAX_TENANTS_CONSIDERED:
        # Never act on a subset and report on the whole: the tenants left out
        # are exactly the ones whose work would be abandoned unseen.
        result(RESULT_FAILED_PRECONDITION, reason="tenant_allowlist_too_large",
               configured=len(tenants), inspected_maximum=recovery.MAX_TENANTS_CONSIDERED)
        return EXIT_USAGE
    emit(f"tenants={','.join(str(t) for t in tenants)}")

    db = None
    try:
        db = session()
        return _HANDLERS[args.command](db, tenants, args)
    except Exception as exc:  # noqa: BLE001 - an unreadable database is never 'settled'
        result(RESULT_FAILED, command=args.command, error=type(exc).__name__)
        return EXIT_FAILED
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - a session we cannot close is dropped
                emit("session_close_failed=true")


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main(sys.argv[1:]))
