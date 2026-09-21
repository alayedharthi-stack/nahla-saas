"""Build the structured stop record a worker retirement rests on.

A retirement (``commerce_runtime_pilot_handover retire``) takes a worker out of
the expected fleet on the strength of the platform's **own** record of the
stop. A free-text capture could say ``RUNNING`` about the very deployment it is
offered as proof of stopping and still be accepted on a digest, so the record
is structured, and this job is how an operator produces it from what the
platform answered.

The trusted operator boundary, stated plainly
============================================
This job does not stop anything and does not query the platform. The operator
performs the stop and captures the platform's answer; this job reads that
capture, refuses to write a record for a deployment the capture says is still
active, and writes a record that names the exact deployment and incarnation,
the platform's state word, zero active replicas, the moment and the source. The
retirement command then checks that record again before the worker leaves the
fleet. What remains trusted operator work is exactly the capture: that it is
the platform's genuine answer about that deployment at that moment.

Railway (the deployed topology)
-------------------------------
The workers are replicas of the ``nahla-saas`` service. Each names itself
``<RAILWAY_DEPLOYMENT_ID>/<RAILWAY_REPLICA_ID>@<host>:<pid>`` in the fleet
table, so the deployment a worker belonged to is on its row. Stop and fence one
by removing that deployment (``railway down``, or the console's *Remove*), or
by rolling to a new deployment so the platform removes the old one; then
capture the deployment listing::

    railway deployment list --service nahla-saas --environment production \\
        --json > /tmp/deployments.json

and build the record::

    python -m scripts.operators.commerce_runtime_worker_stop_record \\
        --deployment 477f560f-a47c-4750-966e-8acffc4c9596 \\
        --incarnation "477f560f-a47c-4750-966e-8acffc4c9596/0" \\
        --platform-json /tmp/deployments.json \\
        --source "railway deployment list --service nahla-saas --environment production --json" \\
        --out /tmp/stop-record.json

The capture may be the CLI listing, the GraphQL ``deployments`` answer or a
single deployment object; the job finds the deployment by id. Only a status
that establishes the fence — the platform will not run this deployment again:
``REMOVED``, ``FAILED``, ``SKIPPED`` — yields a record. ``CRASHED`` does not:
the platform restarts a crashed container under the service's restart policy
with the same deployment and replica identity, so the procedure for one is to
remove the deployment and capture again. ``SUCCESS``, ``DEPLOYING``,
``SLEEPING`` and every transitional status are refused — a sleeping service
wakes, and nothing that can wake is fenced.

The capture is read for what it says about replicas, and the record says on
what basis it states zero: ``active_replicas_basis`` is
``measured:<key>`` when the capture carries a running/active count (which then
has to be zero — a capture that says ``REMOVED`` and ``active_replicas: 1``
in the same breath is a contradiction and is refused, never summarised as
zero), and ``inferred_from_status:<STATUS>`` when the capture measures nothing
and zero follows from the fence status alone. A configured replica count
(``numReplicas``) is what the deployment asked for, not what is running; it is
carried as ``configured_replicas`` and contradicts nothing. The retirement
validator re-reads the retained capture against the same rules.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any, Dict, Optional, Sequence

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _path in (os.path.join(_ROOT, "backend"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.commerce_runtime import stop_evidence as se  # noqa: E402

PLATFORM = "railway"

# The platform's status words that establish the fence, and the state word the
# stop record carries for each; the words that are inactive but restartable;
# the words for something running or on its way to running. Read from one
# place so the retirement validator and this job cannot disagree.
FENCED_STATUSES: Dict[str, str] = dict(se.FENCED_STATUSES)
RESTARTABLE_STATUSES: Dict[str, str] = dict(se.RESTARTABLE_STATUSES)
ACTIVE_STATUSES = se.ACTIVE_STATUSES

RAW_MAX_BYTES = 32 * 1024

RESULT_RECORDED = "RECORDED"
RESULT_REFUSED = "REFUSED"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

LOG_PREFIX = "[commerce-runtime-stop-record]"


def _emit(marker: str, **fields: Any) -> None:
    rendered = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}"
                        for key, value in fields.items())
    print(f"{LOG_PREFIX} RESULT={marker} {rendered}".rstrip())


deployments_in = se.deployments_in
find_deployment = se.find_deployment


def build_record(*, deployment: str, incarnation: str, platform_json: Any, source: str,
                 observed_at: Optional[str] = None) -> Dict[str, Any]:
    """The structured stop record, or a ``ValueError`` naming why there is none.

    The record summarises the capture; it never overrules it. A capture that
    reports a replica or instance running is refused whatever its status word
    says, and the basis on which the record states zero replicas — measured
    in the capture, or inferred from a fence status — is written down.
    """
    wanted = str(deployment or "").strip()
    if not wanted:
        raise ValueError("deployment_required")
    who = str(incarnation or "").strip()
    if not who:
        raise ValueError("incarnation_required")
    if not str(source or "").strip():
        raise ValueError("source_required")
    found = find_deployment(platform_json, wanted)
    if found is None:
        raise ValueError("deployment_not_in_capture")
    status = se.status_word(found)
    kind, state = se.state_for(status)
    if kind == "active":
        raise ValueError(f"deployment_active:{status}")
    if kind == "restartable":
        raise ValueError(f"deployment_status_not_a_fence:{status}")
    if kind != "fenced":
        raise ValueError(f"deployment_status_unrecognised:{status or '(none)'}")
    measured, measured_key, configured = se.replica_evidence(found)
    if measured is not None and measured > 0:
        raise ValueError(f"capture_reports_active_replicas:{measured}:{measured_key}")
    moment = str(observed_at or "").strip() or _dt.datetime.now(_dt.timezone.utc).isoformat()
    raw = json.dumps(found, ensure_ascii=False, sort_keys=True)
    if len(raw.encode("utf-8")) > RAW_MAX_BYTES:
        # A capture too large to retain whole cannot be re-read whole either;
        # the retained part has to stay a document the validator can read.
        raise ValueError(f"capture_too_large:{len(raw.encode('utf-8'))}>{RAW_MAX_BYTES}")
    record: Dict[str, Any] = {
        "deployment": wanted,
        "incarnation": who,
        "state": state,
        "active_replicas": 0,
        "active_replicas_basis": (f"measured:{measured_key}" if measured is not None
                                  else f"inferred_from_status:{status}"),
        "observed_at": moment,
        "source": str(source).strip(),
        "platform": PLATFORM,
        "platform_status": status,
        "raw": raw,
    }
    if configured is not None:
        record["configured_replicas"] = configured
    return record


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", required=True,
                        help="the deployment id, exactly as the platform names it")
    parser.add_argument("--incarnation", required=True,
                        help="the replica/process incarnation, e.g. <deployment>/<replica>")
    parser.add_argument("--platform-json", dest="platform_json", required=True,
                        help="file holding the platform's captured answer (JSON)")
    parser.add_argument("--source", required=True,
                        help="the exact command or query that produced the capture")
    parser.add_argument("--observed-at", dest="observed_at", default="",
                        help="ISO-8601 moment of the capture (default: now, UTC)")
    parser.add_argument("--out", required=True, help="where to write the record")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        with open(args.platform_json, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        _emit(RESULT_REFUSED, reason="capture_unreadable", error=type(exc).__name__)
        return EXIT_USAGE
    try:
        record = build_record(deployment=args.deployment, incarnation=args.incarnation,
                              platform_json=document, source=args.source,
                              observed_at=args.observed_at)
    except ValueError as exc:
        _emit(RESULT_REFUSED, reason=str(exc), deployment=args.deployment,
              hint="no record is written for a deployment the platform reports as "
                   "active, restartable (CRASHED) or with a replica running; remove "
                   "the deployment so the platform reports REMOVED, capture again, "
                   "and re-run")
        return EXIT_REFUSED
    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        _emit(RESULT_REFUSED, reason="record_unwritable", error=type(exc).__name__)
        return EXIT_USAGE
    _emit(RESULT_RECORDED, deployment=record["deployment"], incarnation=record["incarnation"],
          state=record["state"], platform_status=record["platform_status"],
          active_replicas_basis=record["active_replicas_basis"],
          observed_at=record["observed_at"], out=args.out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
