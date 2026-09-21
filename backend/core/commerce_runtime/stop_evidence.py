"""Reading a platform's captured answer about a deployment, as evidence of a stop.

Two places read the same capture and have to agree on it: the operator job
that builds a structured stop record from it (``scripts/operators/
commerce_runtime_worker_stop_record.py``) and the retirement validator that
re-reads the capture retained under the record's ``raw`` before a worker
leaves the fleet (``handover._validated_stop_record``). A summary the builder
writes must not be able to say something the capture does not — so the
capture is read here, once, by both.

What is read
------------
* **the deployment object** the capture holds for a given id, whatever shape
  the capture is — a CLI listing, a GraphQL ``deployments`` answer with
  ``edges``/``node``, a bare list, or a single object;
* **its status word** — which either establishes the fence a retirement rests
  on (the platform will not run this deployment again), is inactive but
  restartable (a crashed container is restarted under the service's restart
  policy with the same deployment and replica identity), or is active;
* **replica evidence** — a *measured* active count (``active_replicas``,
  ``activeReplicas``, ``runningReplicas`` …, or instances listed with an
  active status), distinguished from a *configured* count (``numReplicas``,
  ``replicas`` …), which is what the deployment asked for and says nothing
  about what is running.

Nothing here talks to a platform. It reads what an operator captured.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Railway's status words for a deployment the platform will not run again
# under the same identity, and the state word a stop record carries for each.
FENCED_STATUSES: Dict[str, str] = {
    "REMOVED": "removed",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}
# Inactive at the moment of capture, and not a fence: the platform restarts a
# crashed container under the service's restart policy, with the same
# deployment and replica identity. The procedure for one is to remove it.
RESTARTABLE_STATUSES: Dict[str, str] = {
    "CRASHED": "crashed",
}
ACTIVE_STATUSES = frozenset({
    "SUCCESS", "DEPLOYING", "BUILDING", "INITIALIZING", "QUEUED", "WAITING",
    "NEEDS_APPROVAL", "REMOVING", "SLEEPING",
})

# The same three classes as state words, for records and captures from any
# platform: what the retirement validator accepts, refuses as restartable, and
# refuses as active.
FENCED_STATES = frozenset({"removed", "terminated", "dead", "failed", "skipped", "deleted"})
RESTARTABLE_STATES = frozenset({"crashed", "stopped", "exited", "inactive"})
ACTIVE_STATES = frozenset({
    "running", "success", "deploying", "building", "initializing", "queued", "waiting",
    "needs_approval", "removing", "sleeping", "active", "up", "live", "starting", "restarting",
})

# Keys under which a capture reports how many replicas or instances are
# actually running — a measurement — as opposed to how many were asked for.
MEASURED_ACTIVE_COUNT_KEYS: Tuple[str, ...] = (
    "active_replicas", "activeReplicas", "runningReplicas", "running_replicas",
    "activeInstances", "active_instances", "runningInstances", "running_instances",
)
CONFIGURED_COUNT_KEYS: Tuple[str, ...] = ("numReplicas", "num_replicas", "replicas",
                                          "desiredReplicas", "desired_replicas")
INSTANCE_LIST_KEYS: Tuple[str, ...] = ("instances", "replicas", "containers", "pods")


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


def status_word(found: Any) -> str:
    """The capture's status word for a deployment, upper-cased, or ``""``."""
    if not isinstance(found, dict):
        return ""
    return str(found.get("status") or found.get("state") or "").strip().upper()


def state_for(status: str) -> Tuple[str, str]:
    """Classify a platform status: ``(class, state_word)`` where the class is
    ``fenced``, ``restartable``, ``active`` or ``unrecognised``."""
    word = str(status or "").strip().upper()
    if word in FENCED_STATUSES:
        return "fenced", FENCED_STATUSES[word]
    if word in RESTARTABLE_STATUSES:
        return "restartable", RESTARTABLE_STATUSES[word]
    if word in ACTIVE_STATUSES:
        return "active", word.lower()
    lowered = word.lower()
    if lowered in FENCED_STATES:
        return "fenced", lowered
    if lowered in RESTARTABLE_STATES:
        return "restartable", lowered
    if lowered in ACTIVE_STATES:
        return "active", lowered
    return "unrecognised", lowered


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def replica_evidence(found: Any) -> Tuple[Optional[int], str, Optional[int]]:
    """What the deployment object says about running replicas.

    Returns ``(measured_active, measured_key, configured)``: the largest
    measured active count found anywhere in the object and the key it was read
    under (``None``/``""`` when the capture measures nothing), and the largest
    configured count (``None`` when none). Instances listed with an active
    status word count as measured, under the list's key.
    """
    measured: Optional[int] = None
    measured_key = ""
    configured: Optional[int] = None

    def take_measured(count: int, key: str) -> None:
        nonlocal measured, measured_key
        if measured is None or count > measured:
            measured, measured_key = count, key

    def walk(node: Any) -> None:
        nonlocal configured
        if isinstance(node, dict):
            for key, value in node.items():
                if key in MEASURED_ACTIVE_COUNT_KEYS:
                    count = _as_count(value)
                    if count is not None:
                        take_measured(count, key)
                        continue
                if key in CONFIGURED_COUNT_KEYS:
                    count = _as_count(value)
                    if count is not None:
                        configured = count if configured is None else max(configured, count)
                        continue
                if key in INSTANCE_LIST_KEYS and isinstance(value, list):
                    active = sum(1 for item in value
                                 if isinstance(item, dict)
                                 and state_for(status_word(item))[0] == "active")
                    take_measured(active, key)
                    for item in value:
                        walk(item)
                    continue
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(found)
    return measured, measured_key, configured


__all__ = [
    "ACTIVE_STATES", "ACTIVE_STATUSES", "CONFIGURED_COUNT_KEYS", "FENCED_STATES",
    "FENCED_STATUSES", "INSTANCE_LIST_KEYS", "MEASURED_ACTIVE_COUNT_KEYS",
    "RESTARTABLE_STATES", "RESTARTABLE_STATUSES", "deployments_in", "find_deployment",
    "replica_evidence", "state_for", "status_word",
]
