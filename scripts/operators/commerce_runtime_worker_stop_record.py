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
single deployment object; the job finds the deployment by id. A status the
platform uses for something no longer running (``REMOVED``, ``CRASHED``,
``FAILED``, ``SKIPPED``) yields a record; ``SUCCESS``, ``DEPLOYING``,
``SLEEPING`` and every transitional status are refused — a sleeping service
wakes, and nothing that can wake is fenced.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

PLATFORM = "railway"

# The platform's status words for a deployment that no longer runs, and the
# state word the stop record carries for each (a member of the handover's
# inactive set). Everything else is refused by name.
INACTIVE_STATUSES: Dict[str, str] = {
    "REMOVED": "removed",
    "CRASHED": "crashed",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}
ACTIVE_STATUSES = frozenset({
    "SUCCESS", "DEPLOYING", "BUILDING", "INITIALIZING", "QUEUED", "WAITING",
    "NEEDS_APPROVAL", "REMOVING", "SLEEPING",
})

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


def deployments_in(document: Any) -> List[Dict[str, Any]]:
    """Every deployment object a capture holds, whatever shape the capture is."""
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if not isinstance(document, dict):
        return []
    for key in ("deployments", "edges", "nodes", "data"):
        inner = document.get(key)
        if isinstance(inner, list):
            found: List[Dict[str, Any]] = []
            for item in inner:
                if isinstance(item, dict) and isinstance(item.get("node"), dict):
                    found.append(item["node"])
                elif isinstance(item, dict):
                    found.append(item)
            return found
        if isinstance(inner, dict):
            nested = deployments_in(inner)
            if nested:
                return nested
    if "id" in document and "status" in document:
        return [document]
    return []


def find_deployment(document: Any, deployment: str) -> Optional[Dict[str, Any]]:
    wanted = str(deployment or "").strip()
    for item in deployments_in(document):
        if str(item.get("id") or "").strip() == wanted:
            return item
    return None


def build_record(*, deployment: str, incarnation: str, platform_json: Any, source: str,
                 observed_at: Optional[str] = None) -> Dict[str, Any]:
    """The structured stop record, or a ``ValueError`` naming why there is none."""
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
    status = str(found.get("status") or "").strip().upper()
    if status in ACTIVE_STATUSES:
        raise ValueError(f"deployment_active:{status}")
    if status not in INACTIVE_STATUSES:
        raise ValueError(f"deployment_status_unrecognised:{status or '(none)'}")
    moment = str(observed_at or "").strip() or _dt.datetime.now(_dt.timezone.utc).isoformat()
    raw = json.dumps(found, ensure_ascii=False, sort_keys=True)
    if len(raw.encode("utf-8")) > RAW_MAX_BYTES:
        raw = raw.encode("utf-8")[:RAW_MAX_BYTES].decode("utf-8", errors="ignore")
    return {
        "deployment": wanted,
        "incarnation": who,
        "state": INACTIVE_STATUSES[status],
        "active_replicas": 0,
        "observed_at": moment,
        "source": str(source).strip(),
        "platform": PLATFORM,
        "platform_status": status,
        "raw": raw,
    }


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
                   "active; stop or remove it, capture again, and re-run")
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
          observed_at=record["observed_at"], out=args.out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
