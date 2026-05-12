"""
services/offer_policies.py
──────────────────────────
**Phase 6** — open the door for swappable offer-decision policies.

Why this module exists
──────────────────────
Phases 1–5 built one deterministic decision policy and wired every
caller through it. Phase 6 does **not** introduce a learning policy
(that is its own future project) — it splits the *interface* from the
*implementation* so that a future contextual-bandit / experiment-driven
policy can be slotted in without touching any of the three caller
surfaces (automation engine, chat orchestrator, customer-intelligence
segment change).

Two orthogonal axes are now configurable per tenant:

  • **policy_version** — which deterministic algorithm runs at decide
    time. Today only ``v1.0-deterministic`` is registered; a future
    ``v2.0-bandit`` (or merchant-specific custom policy) registers under
    its own version string and selects in by name.

  • **experiment_arm** — when a tenant enables an A/B experiment, every
    request is assigned to exactly one arm. Assignment is **sticky by
    customer** (hash-mod): the same customer always lands on the same
    arm for the lifetime of the experiment, so attribution numbers stay
    comparable. Anonymous (no customer_id) traffic gets a random arm
    drawn from a deterministic seed so a single request can be replayed
    without changing the arm — useful for offline analysis.

Both axes are written into ``OfferDecisionLedger`` so the analytics API
(`/offers/decisions`) can group / compare without any further plumbing.

Defaults
────────
Out of the box every tenant runs ``v1.0-deterministic`` with no
experiment. The selector returns ``("v1.0-deterministic", None)`` which
matches the historical hard-coded behaviour bit-for-bit — guaranteed by
the regression tests in ``tests/test_offer_policy_registry.py``.

Configuration
─────────────
Per-tenant settings live under
``TenantSettings.ai_settings.offer_policy`` with the shape::

    {
        "version": "v1.0-deterministic",     // active policy version
        "experiment": {                       // OPTIONAL — A/B split
            "name": "discount_value_v2",      // free-form label
            "arms": [
                {"name": "control",   "weight": 50, "policy_version": "v1.0-deterministic"},
                {"name": "treatment", "weight": 50, "policy_version": "v1.0-deterministic"}
            ],
            "sticky_by": "customer_id"        // or "decision_id" for non-sticky
        }
    }

If ``experiment`` is present the arm's ``policy_version`` overrides the
top-level ``version`` field, so an A/B test between two policies is a
single configuration change.

Public API
──────────
    Policy                Protocol  — a callable that returns OfferDecision
    register_policy(...)            — register a new policy implementation
    resolve_policy(...)             — look up the registered callable
    select_policy(db, ctx)          — (policy_version, experiment_arm) tuple
    PolicyResolutionError           — raised by `decide()` on truly broken
                                      tenant config; never bubbles up to
                                      callers because `decide()` wraps it.

Failure semantics
─────────────────
This module is on the hot path for every WhatsApp inbound that has any
chance of issuing an offer. None of the public functions raise on
malformed tenant config — they degrade to ``("v1.0-deterministic", None)``
and emit a single WARNING log line so ops can spot the bad config in
Logs without an outage.
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Protocol, Tuple

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # avoid runtime circular import via offer_decision_service
    from services.offer_decision_service import OfferDecision, OfferDecisionContext


logger = logging.getLogger(__name__)


# ── Canonical version string used by the v1 deterministic policy ────────────
#
# Kept here (instead of importing from offer_decision_service) to keep the
# import graph one-way: offer_decision_service imports offer_policies, not
# the other way around. The constant in offer_decision_service.POLICY_VERSION
# is just an alias of this value for backward compatibility.
DEFAULT_POLICY_VERSION = "v1.0-deterministic"


# ── Policy registry ─────────────────────────────────────────────────────────

class Policy(Protocol):
    """A deterministic (or learning) policy implementation.

    Receives the already-built ``OfferDecisionContext`` and a live DB
    session, and returns the chosen :class:`OfferDecision`. Implementations
    MUST be free of side effects on the DB beyond reading reference rows
    (promotions, rules, settings) — ledger persistence is handled by the
    caller in :func:`offer_decision_service.decide` so policy swaps remain
    invisible to analytics infrastructure.
    """

    def __call__(self, db: Session, ctx: "OfferDecisionContext") -> "OfferDecision": ...


# Module-level singleton registry. We use a plain dict (no thread-locking)
# because policies are registered at import time and the registry becomes
# read-only after process boot. Tests that need to add/remove entries do
# so explicitly via the helpers below.
_REGISTRY: Dict[str, Policy] = {}


def register_policy(version: str, fn: Policy) -> None:
    """Register a policy callable under a version string.

    Idempotent — re-registering the same version overwrites the previous
    callable. Used at import time by the built-in v1 policy and by tests
    that want to swap in a stub.
    """
    if not version or not isinstance(version, str):
        raise ValueError("policy version must be a non-empty string")
    if not callable(fn):
        raise TypeError("policy must be callable (db, ctx) -> OfferDecision")
    _REGISTRY[version] = fn


def unregister_policy(version: str) -> None:
    """Remove a policy from the registry (test helper)."""
    _REGISTRY.pop(version, None)


def registered_versions() -> List[str]:
    """Returns the list of registered policy version strings (sorted)."""
    return sorted(_REGISTRY.keys())


def resolve_policy(version: Optional[str]) -> Tuple[str, Policy]:
    """Look up a policy by version. Falls back to v1 on unknown / None.

    Returns the actually-used ``(version, callable)`` pair so the caller
    can stamp the *effective* version into the ledger row, not whatever
    typo'd value the tenant config had. Mismatches log a single WARNING.
    """
    if not version:
        return DEFAULT_POLICY_VERSION, _REGISTRY[DEFAULT_POLICY_VERSION]
    fn = _REGISTRY.get(version)
    if fn is None:
        logger.warning(
            "[offer_policies] unknown policy version=%r — falling back to %s",
            version, DEFAULT_POLICY_VERSION,
        )
        return DEFAULT_POLICY_VERSION, _REGISTRY[DEFAULT_POLICY_VERSION]
    return version, fn


# ── Experiment / A-B selection ──────────────────────────────────────────────

@dataclass(frozen=True)
class _Arm:
    name:           str
    weight:         int
    policy_version: Optional[str]


def _read_policy_config(db: Session, tenant_id: int) -> Dict:
    """Return the ``ai_settings.offer_policy`` dict for this tenant, or {}.

    Defensive: any DB / typing error degrades to {} so the selector
    returns the (safe) default. Never raises.
    """
    try:
        from models import TenantSettings  # noqa: PLC0415 — defer to dodge cycles

        ts = db.query(TenantSettings).filter_by(tenant_id=int(tenant_id)).first()
        if ts is None or not isinstance(ts.ai_settings, dict):
            return {}
        cfg = ts.ai_settings.get("offer_policy")
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("[offer_policies] policy config read failed: %s", exc)
        return {}


def _parse_arms(raw_arms: object) -> List[_Arm]:
    """Coerce ``experiment.arms`` JSON into typed _Arm instances.

    Drops malformed entries (missing name, non-positive weight) so a bad
    arm doesn't disqualify the whole experiment. Returns ``[]`` if none
    survive — caller treats empty as "no experiment".
    """
    if not isinstance(raw_arms, list):
        return []
    arms: List[_Arm] = []
    for entry in raw_arms:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        try:
            weight = int(entry.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        pv = entry.get("policy_version")
        arms.append(_Arm(
            name=name,
            weight=weight,
            policy_version=str(pv).strip() if pv else None,
        ))
    return arms


def _assign_arm(arms: List[_Arm], sticky_key: str) -> _Arm:
    """Pick one arm using a stable hash of ``sticky_key``.

    Hash-mod-cum-weights: we lay each arm out on a [0, total_weight)
    number line and project the hash onto that line. Same key → same
    bucket → same arm. Empty ``sticky_key`` falls back to a fresh
    random draw seeded by ``random``'s default source so anonymous
    traffic still gets sampled in proportion to the weights.
    """
    total = sum(a.weight for a in arms)
    if total <= 0:
        return arms[0]
    if sticky_key:
        # Stable 64-bit projection: blake2b is fast and standard-library.
        digest = hashlib.blake2b(sticky_key.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest, "big") % total
    else:
        bucket = random.randrange(total)
    cursor = 0
    for arm in arms:
        cursor += arm.weight
        if bucket < cursor:
            return arm
    return arms[-1]  # pragma: no cover — guarded by total > 0 above


def select_policy(
    db: Session,
    ctx: "OfferDecisionContext",
) -> Tuple[str, Optional[str], Policy]:
    """Resolve ``(policy_version, experiment_arm_name, policy_callable)``.

    Resolution order:

      1. If ``ai_settings.offer_policy.experiment.arms`` is present and
         non-empty, pick one arm (sticky by ``customer_id`` by default).
         If the arm declares ``policy_version`` it wins.
      2. Else use ``ai_settings.offer_policy.version`` if set.
      3. Else fall back to ``DEFAULT_POLICY_VERSION``.

    Unknown version strings degrade to default with a WARNING log.
    """
    cfg = _read_policy_config(db, ctx.tenant_id)
    experiment = cfg.get("experiment") if isinstance(cfg, dict) else None

    arm_name: Optional[str] = None
    version: Optional[str] = (
        str(cfg.get("version")).strip()
        if isinstance(cfg, dict) and cfg.get("version") else None
    )

    if isinstance(experiment, dict):
        arms = _parse_arms(experiment.get("arms"))
        if arms:
            sticky_by = str(experiment.get("sticky_by") or "customer_id")
            if sticky_by == "customer_id" and ctx.customer_id is not None:
                sticky_key = f"{ctx.tenant_id}:{ctx.customer_id}"
            elif sticky_by == "decision_id":
                sticky_key = ""  # forces per-call random sampling
            else:
                sticky_key = str(experiment.get("name") or "") + f":{ctx.tenant_id}"
            chosen_arm = _assign_arm(arms, sticky_key)
            arm_name = chosen_arm.name
            if chosen_arm.policy_version:
                version = chosen_arm.policy_version

    resolved_version, fn = resolve_policy(version)
    return resolved_version, arm_name, fn


# ── Built-in policy registration (deferred to avoid circular imports) ──────

def _register_builtin_v1() -> None:
    """Register the deterministic v1 policy under its canonical version.

    Imported lazily inside this function so the registry module itself
    has no static dependency on ``offer_decision_service`` (which
    imports *this* module). Called once at import time by
    ``offer_decision_service``.
    """
    from services.offer_decision_service import _run_policy  # noqa: PLC0415
    register_policy(DEFAULT_POLICY_VERSION, _run_policy)
