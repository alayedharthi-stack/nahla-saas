"""Operator procedure: hand the commerce-runtime pilot back, safely.

Switching the pilot off is not a rollback. The flag decides whether *this
process* takes new turns; it says nothing about the turns the runtime already
admitted, nothing about the other replicas, and nothing about a send already in
flight. Flipping it while work is outstanding abandons five things at once:

* an admitted turn with no terminal — a customer owed an answer or an honest
  record, and no path left to give either;
* a reply the loop reserved and nothing dispatched;
* a send with no receipt;
* a send whose recorded outcome is ``unknown`` — which is not evidence it did
  not arrive;
* an inbound the provider was told we accepted and nobody has answered.

So the handover runs against a **shared barrier** in the runtime's own tables
(revision ``0111``), which every replica reads and this job writes, and each
step is a command:

    1. DRAIN    python -m scripts.operators.commerce_runtime_pilot_handover drain
                Closes the barrier for every allowlisted tenant, fleet-wide,
                from that instant. Affected inbounds are *deferred* — recorded
                durably with their own identity and payload, answered by nobody.
                They are not released to the legacy path: the runtime being
                handed over from may still have a send in flight, and a second
                answer is the thing a handover must not produce.

    2. CONVERGE Let every replica read the new barrier. A worker records the
                generation **it observed** the first time it evaluates a route
                after the change, so convergence is *observed* here rather than
                asserted. ``status`` names the workers still behind or stale.
                A worker that has stopped is retired explicitly:

                python -m ... handover retire --worker <id> \\
                    --by "<operator>" --reason "terminated in deploy 1234"

                Silence never retires a worker. A stale one blocks settlement
                until somebody says, on the record, that it is gone.

    3. STATUS   python -m ... commerce_runtime_pilot_handover status
                Shows, per tenant: barrier state and generation, worker
                convergence, outstanding work including unknown outcomes, and
                the deferred inbounds awaiting disposition, by id.

    4. DISPOSE  python -m ... commerce_runtime_pilot_handover dispose \\
                    --entry 41 --entry 42 --disposition replayed \\
                    --evidence '{"replayed_at": "...", "by_hand": true}' \\
                    --by "<operator>"
                Named entries, a disposition from a closed set, and evidence
                that travels with each row. An entry that arrived after the
                operator last looked is not in the list and is not disposed of.

    5. SETTLE   python -m ... commerce_runtime_pilot_handover settle
                Validates and transitions in **one** transaction under the
                tenant's lock: the generation the status showed, the
                convergence, every count and every pending entry, re-checked at
                the moment of the write. Exits 0 only when all of them hold, and
                the evidence it stores is the state it actually settled.

    6. STOP     COMMERCE_RUNTIME_PILOT_ENABLED=false
                Only after step 5 exits 0.

    (later)     python -m ... commerce_runtime_pilot_handover reopen
                Admits new work again on a fresh generation, keeping the audit
                trail. It re-checks under the same lock that the barrier is
                still the settled one it was asked about, so a drain that
                started in the meantime is never reopened over.

What this proves, and what it does not
--------------------------------------
Convergence is evidence from the workers themselves, not a statement that they
were restarted, and not an inference from silence. Counts are the database's
account of recorded work. Neither can see a request already on the wire to the
provider, which is why an ``unknown`` outcome blocks settlement rather than
ageing out of it: elapsed time and a cancelled wait are not proof of
non-delivery, and nothing here treats them as such.

Shared admission locking orders this job against the workers that take it. It
does not prove a fleet was rolled out or shut down; only a worker's own report,
or an operator's recorded retirement, says that.

If any of it cannot be established the job stays blocked and says which part.

Read-only except for the barrier, the fleet and the deferred entries it is asked
to dispose of. It sends no message, answers no customer and changes no runtime
configuration.
"""
from __future__ import annotations

import argparse
import json
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
RESULT_RETIRED = "RETIRED"
RESULT_REOPENED = "REOPENED"
RESULT_RECOVERED = "RECOVERED"
RESULT_RELEASED = "RELEASED"
RESULT_HELD = "HELD"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_FAILED = 3

COMMANDS = ("status", "drain", "dispose", "retire", "settle", "recover", "release",
            "reopen")

# The deployment inventory the fleet is reconciled against. Convergence can
# only see processes that wrote a row; a replica that never reported is
# invisible to it and is exactly the one that would still be admitting. The
# operator states what the deployment is supposed to contain.
ENV_EXPECTED_WORKERS = "COMMERCE_RUNTIME_PILOT_EXPECTED_WORKERS"

# The counts a tenant must have at zero, in the order an operator reads them.
WORK_FIELDS = ("open_turns", "reserved_undispatched", "unresolved_attempts",
               "unknown_outcomes", "deferred_pending")


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


def expected_workers(environ: Optional[dict] = None) -> List[str]:
    """The worker ids this deployment is supposed to be running, if stated."""
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_EXPECTED_WORKERS, "") or "").strip()
    return sorted({piece.strip() for piece in raw.split(",") if piece.strip()})


def observe(tenants: Sequence[int]) -> tuple:
    from core.commerce_runtime import recovery

    return recovery.handover_state(tenant_ids=list(tenants))


def observe_on(conn: Any, tenants: Sequence[int]) -> tuple:
    """The same counts, on a session the caller is already inside."""
    from core.commerce_runtime import recovery

    return recovery.handover_state_on(conn, tenant_ids=list(tenants))


def inspect(db: Any, tenants: Sequence[int]) -> List[Dict[str, Any]]:
    """Barrier, fleet, convergence, counts and deferred work, per tenant."""
    from core.commerce_runtime import handover

    states = {state.tenant_id: state for state in observe(tenants)}
    report = []
    for tenant_id in tenants:
        barrier = handover.read_barrier(db, tenant_id=tenant_id)
        workers = handover.fleet(db, tenant_id=tenant_id)
        report.append({
            "tenant_id": tenant_id,
            "barrier": barrier,
            "workers": workers,
            "convergence": handover.convergence(barrier, workers),
            "fleet_inventory": handover.expected_fleet(workers, expected_workers()),
            "work": states.get(tenant_id),
            "pending": handover.pending_inbound(db, tenant_id=tenant_id),
            "release": handover.release_state(db, tenant_id=tenant_id),
        })
    return report


def describe(entry: Dict[str, Any]) -> None:
    barrier, work = entry["barrier"], entry["work"]
    convergence = entry["convergence"]
    emit(f"tenant={entry['tenant_id']} barrier={barrier.state} "
         f"generation={barrier.generation} converged={convergence['converged']} "
         f"expected={convergence['expected']} on_generation={convergence['on_generation']} "
         f"behind={convergence['behind']} stale={convergence['stale']} "
         f"retired={convergence['retired']}")
    if work is not None:
        emit(" ".join(f"{key}={value}" for key, value in work.as_log_fields().items()))
    for record in entry["pending"]:
        emit(f"tenant={entry['tenant_id']} deferred entry={record.id} "
             f"reason={record.reason} recipient={record.recipient} "
             f"provider_message_id={record.provider_message_id} "
             f"generation={record.barrier_generation}")
    emit(f"tenant={entry['tenant_id']} pending_deferred={len(entry['pending'])}")
    inventory = entry.get("fleet_inventory") or {}
    if inventory.get("expected"):
        emit(f"tenant={entry['tenant_id']} fleet_expected={inventory['expected']} "
             f"reporting={inventory['reporting']} "
             f"missing={inventory['missing_from_fleet']} "
             f"unexpected={inventory['unexpected_in_fleet']} "
             f"reconciled={inventory['reconciled']}")
    else:
        emit(f"tenant={entry['tenant_id']} fleet_expected=UNSTATED — set "
             f"{ENV_EXPECTED_WORKERS} so a replica that never reported is visible")
    release = entry.get("release")
    if release is not None:
        emit(" ".join(f"{key}={value}" for key, value in release.as_log_fields().items()))


def blockers_for(entry: Dict[str, Any]) -> List[str]:
    """Every concrete reason this tenant is not settled. Never a bare 'no'."""
    from core.commerce_runtime import handover

    barrier, work, convergence = entry["barrier"], entry["work"], entry["convergence"]
    reasons: List[str] = []
    if barrier.state != handover.STATE_DRAINING:
        reasons.append(f"barrier_is_{barrier.state}_not_draining")
    reasons.extend(convergence_blockers(convergence))
    if work is None:
        reasons.append("work_counts_unavailable")
    else:
        for field in WORK_FIELDS:
            count = getattr(work, field, 0)
            if count:
                reasons.append(f"{field}={count}")
    inventory = entry.get("fleet_inventory") or {}
    if inventory.get("missing_from_fleet"):
        reasons.append("workers_expected_but_never_reported:"
                       + ",".join(inventory["missing_from_fleet"]))
    return reasons


def convergence_blockers(convergence: Dict[str, Any]) -> List[str]:
    if convergence["converged"]:
        return []
    reasons = []
    if convergence["behind"]:
        reasons.append(f"workers_behind:{','.join(convergence['behind'])}")
    if convergence["stale"]:
        reasons.append(f"workers_stale_retire_or_wait:{','.join(convergence['stale'])}")
    if not convergence["on_generation"]:
        reasons.append("no_worker_has_reported_this_generation")
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


def cmd_retire(db: Any, tenants: Sequence[int], args: Any) -> int:
    """Take a named worker out of the expected set, on the record."""
    from core.commerce_runtime import handover

    name = str(getattr(args, "worker", "") or "").strip()
    by = str(getattr(args, "by", "") or "").strip()
    reason = str(getattr(args, "reason", "") or "").strip()
    if not name or not by or not reason:
        result(RESULT_FAILED_PRECONDITION, reason="worker_by_and_reason_required",
               hint="retirement is a statement an operator makes; it needs a name and a why")
        return EXIT_USAGE
    try:
        evidence = json.loads(str(getattr(args, "evidence", "") or "{}"))
        if not isinstance(evidence, dict):
            raise ValueError
    except ValueError:
        result(RESULT_FAILED_PRECONDITION, reason="evidence_must_be_a_json_object")
        return EXIT_USAGE

    retired: List[int] = []
    try:
        for tenant_id in tenants:
            if handover.retire_worker(db, tenant_id=tenant_id, name=name, by=by,
                                      reason=reason, evidence=evidence):
                retired.append(tenant_id)
    except handover.RetirementRefused as refused:
        result(RESULT_FAILED_PRECONDITION, reason="retirement_refused", detail=str(refused),
               required=list(handover.RETIREMENT_EVIDENCE_KEYS),
               hint="name the deployment, how the stop was verified, and when it was "
                    "observed; elapsed silence is not one of them")
        return EXIT_USAGE
    if not retired:
        result(RESULT_FAILED_PRECONDITION, reason="worker_not_found", worker=name)
        return EXIT_USAGE
    result(RESULT_RETIRED, worker=name, tenants=retired, by=by,
           deployment=evidence.get("deployment"))
    return EXIT_OK


def cmd_dispose(db: Any, tenants: Sequence[int], args: Any) -> int:
    from core.commerce_runtime import handover

    entries = [int(entry) for entry in (getattr(args, "entry", None) or [])]
    disposition = str(getattr(args, "disposition", "") or "").strip()
    by = str(getattr(args, "by", "") or "").strip()
    if not entries or disposition not in handover.DISPOSITIONS or not by:
        result(RESULT_FAILED_PRECONDITION, reason="entries_disposition_and_by_required",
               dispositions=list(handover.DISPOSITIONS),
               evidence_keys={kind: list(keys) for kind, keys
                              in handover.DISPOSITION_EVIDENCE_KEYS.items()},
               hint="name the entries 'status' showed; a note is not evidence")
        return EXIT_USAGE
    try:
        evidence = json.loads(str(getattr(args, "evidence", "") or "{}"))
        if not isinstance(evidence, dict):
            raise ValueError
    except ValueError:
        result(RESULT_FAILED_PRECONDITION, reason="evidence_must_be_a_json_object")
        return EXIT_USAGE

    cutoff = None
    raw_cutoff = str(getattr(args, "as_of", "") or "").strip()
    if raw_cutoff:
        cutoff = handover._parse_moment(raw_cutoff)  # noqa: SLF001
        if cutoff is None:
            result(RESULT_FAILED_PRECONDITION, reason="as_of_must_be_iso_8601")
            return EXIT_USAGE

    disposed: List[int] = []
    refused: Dict[int, str] = {}
    for tenant_id in tenants:
        outcome = handover.dispose_inbound(db, tenant_id=tenant_id, entry_ids=entries,
                                           disposition=disposition, evidence=evidence, by=by,
                                           not_after=cutoff)
        disposed.extend(outcome.disposed)
        for entry, why in outcome.refused.items():
            # An entry belongs to exactly one tenant; "not this tenant's" from
            # the others is expected and is not a refusal to report.
            if why != "not_this_tenant_s_entry":
                refused[entry] = why
    for entry in entries:
        if entry not in disposed and entry not in refused:
            refused[entry] = "no_such_entry"
    if refused:
        result(RESULT_BLOCKED, disposed=disposed, refused=refused,
               next_step="re-run 'status'; an entry that is not pending is not disposable")
        return EXIT_BLOCKED
    result(RESULT_DISPOSED, entries=disposed, disposition=disposition, by=by)
    return EXIT_OK


def cmd_settle(db: Any, tenants: Sequence[int], _args: Any) -> int:
    """Validate and transition inside one locked transaction, per tenant."""
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

    # Everything above is a *report*. The decision is taken again below, inside
    # the transaction that writes it, against the state at that instant.
    settled: List[int] = []
    for entry in entries:
        tenant_id = entry["tenant_id"]
        outcome = handover.settle(
            db, tenant_id=tenant_id, expected_generation=entry["barrier"].generation,
            validate=_validator(tenant_id))
        if not outcome.settled:
            blocked[tenant_id] = list(outcome.blockers)
            emit(f"tenant={tenant_id} settle_refused={list(outcome.blockers)}")
            continue
        settled.append(tenant_id)
        emit(f"tenant={tenant_id} evidence_recorded=true "
             f"generation={outcome.evidence.get('settled_generation')}")
    if blocked:
        result(RESULT_BLOCKED, blockers=blocked, settled=settled,
               next_step="the state moved while settling; run 'status' and settle again")
        return EXIT_BLOCKED
    result(RESULT_SETTLED, tenants=len(settled),
           next_step="COMMERCE_RUNTIME_PILOT_ENABLED=false")
    return EXIT_OK


def _validator(tenant_id: int):
    """Recount and re-check convergence on the settling transaction's session.

    Everything this returns was read inside the transaction that is about to
    write, so the evidence describes the state actually settled rather than the
    state as it looked when the operator last ran ``status``.
    """
    from core.commerce_runtime import handover

    def validate(session: Any, barrier: Any):
        reasons: List[str] = []
        workers = _fleet_on(session, tenant_id)
        convergence = handover.convergence(barrier, workers)
        reasons.extend(convergence_blockers(convergence))

        work = observe_on(session, [tenant_id])[0]
        for field in WORK_FIELDS:
            count = getattr(work, field, 0)
            if count:
                reasons.append(f"{field}={count}")

        evidence = {
            "convergence": convergence,
            "work": work.as_log_fields(),
            "retired_workers": [
                {"worker_id": w.worker_id, "by": w.retired_by, "reason": w.retired_reason}
                for w in workers if w.retired
            ],
        }
        return reasons, evidence

    return validate


def _fleet_on(session: Any, tenant_id: int):
    """The fleet, read on a session the caller already holds."""
    from core.commerce_runtime import handover
    from core.commerce_runtime import handover_models as hm

    rows = (session.query(hm.HandoverWorker)
            .filter(hm.HandoverWorker.tenant_id == int(tenant_id),
                    hm.HandoverWorker.namespace == handover.NAMESPACE)
            .order_by(hm.HandoverWorker.worker_id)
            .all())
    return tuple(handover._worker_from_row(row) for row in rows)


def cmd_recover(db: Any, tenants: Sequence[int], args: Any) -> int:
    """Hand accepted-but-unfinished inbounds back to the dispatcher.

    A replay, not a resend. Each entry is checked against the barrier and
    against the ledger first: a turn that already reached a terminal is closed
    against it rather than repeated, a send whose outcome nobody established is
    left alone, and a tenant that is not admitting work takes nothing back.
    """
    from services import commerce_runtime_recovery as runner

    dry_run = not bool(getattr(args, "apply", False))
    limit = int(getattr(args, "limit", 50) or 50)
    reports = runner.recover(db, tenant_ids=list(tenants), limit=limit, dry_run=dry_run)
    failures = 0
    for report in reports:
        for item in report.outcomes:
            emit(f"tenant={report.tenant_id} " + " ".join(
                f"{key}={value}" for key, value in item.as_log_fields().items()))
            if item.outcome == runner.FAILED:
                failures += 1
        emit(f"tenant={report.tenant_id} inspected={report.inspected} "
             f"outcomes={report.counted()}")
    result(RESULT_RECOVERED, tenants=len(reports), dry_run=dry_run, failures=failures,
           next_step=("re-run with --apply once the plan above is what you intend"
                      if dry_run else "run 'status' and confirm pending_deferred dropped"))
    return EXIT_BLOCKED if failures else EXIT_OK


def cmd_release(db: Any, tenants: Sequence[int], _args: Any) -> int:
    """Whether the pilot may be switched off for these tenants, right now.

    The settlement is evidence about the moment it was taken. This is the same
    question asked again at the moment it matters, so an inbound that arrived
    in the settled window blocks the release rather than being abandoned by a
    verdict that could not have seen it.
    """
    from core.commerce_runtime import handover

    held: Dict[int, List[str]] = {}
    for tenant_id in tenants:
        state = handover.release_state(db, tenant_id=tenant_id)
        emit(" ".join(f"{key}={value}" for key, value in state.as_log_fields().items()))
        if not state.released:
            held[tenant_id] = list(state.blockers)
    if held:
        result(RESULT_HELD, blockers=held,
               next_step="run 'recover' or 'dispose' for the entries above, settle again, "
                         "then re-run 'release'")
        return EXIT_BLOCKED
    result(RESULT_RELEASED, tenants=len(list(tenants)),
           next_step="COMMERCE_RUNTIME_PILOT_ENABLED=false")
    return EXIT_OK


def cmd_reopen(db: Any, tenants: Sequence[int], _args: Any) -> int:
    from core.commerce_runtime import handover

    barriers = {tenant_id: handover.read_barrier(db, tenant_id=tenant_id)
                for tenant_id in tenants}
    for tenant_id, barrier in barriers.items():
        if barrier.state != handover.STATE_SETTLED:
            result(RESULT_FAILED_PRECONDITION, tenant_id=tenant_id,
                   reason=f"barrier_is_{barrier.state}_not_settled",
                   hint="reopening before settlement would discard the evidence")
            return EXIT_USAGE
    reopened, refused = [], {}
    for tenant_id, barrier in barriers.items():
        # Checked and applied together: a drain that opened since the reading
        # above would otherwise be reopened on the strength of a stale one.
        fresh = handover.reopen(db, tenant_id=tenant_id,
                                expected_generation=barrier.generation,
                                validate=_reopen_validator(tenant_id))
        if fresh is None:
            refused[tenant_id] = "state_moved_since_the_precheck"
            continue
        reopened.append(tenant_id)
        emit(f"tenant={tenant_id} barrier=open generation={fresh.generation}")
    if refused:
        result(RESULT_BLOCKED, reopened=reopened, refused=refused,
               next_step="run 'status'; the barrier is no longer the one you inspected")
        return EXIT_BLOCKED
    result(RESULT_REOPENED, tenants=len(reopened))
    return EXIT_OK


def _reopen_validator(tenant_id: int):
    """Refuse to reopen over work nobody has met.

    Read inside the transaction that would write, so an entry recorded during
    the settled window is seen by the decision that reopens rather than buried
    under the traffic the reopen lets in.
    """
    from core.commerce_runtime import handover

    def validate(session: Any, _barrier: Any):
        pending = handover.pending_count_on(session, tenant_id=int(tenant_id))
        return ([f"deferred_pending={pending}"] if pending else []), {"pending": pending}

    return validate


_HANDLERS = {"status": cmd_status, "drain": cmd_drain, "dispose": cmd_dispose,
             "retire": cmd_retire, "settle": cmd_settle, "recover": cmd_recover,
             "release": cmd_release, "reopen": cmd_reopen}


def parse_args(argv: Optional[Sequence[str]] = None) -> Any:
    parser = argparse.ArgumentParser(prog="commerce_runtime_pilot_handover",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="status", choices=COMMANDS)
    parser.add_argument("--entry", action="append", type=int, default=None,
                        help="a deferred entry id from 'status'; repeat for several")
    parser.add_argument("--disposition", default="",
                        help="replayed | answered | superseded | not_required")
    parser.add_argument("--evidence", default="{}",
                        help="a JSON object recording how the entry was handled")
    parser.add_argument("--worker", default="", help="the worker id to retire")
    parser.add_argument("--reason", default="", help="why that worker is gone")
    parser.add_argument("--by", default="", help="who is making this statement")
    parser.add_argument("--as-of", dest="as_of", default="",
                        help="the ISO-8601 moment 'status' was read; entries that arrived "
                             "after it are refused")
    parser.add_argument("--apply", action="store_true",
                        help="'recover' only: actually replay, instead of reporting the plan")
    parser.add_argument("--limit", type=int, default=50,
                        help="'recover' only: how many entries to work per tenant")
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
