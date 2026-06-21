"""
turn/flags.py
─────────────
Feature flags for Turn Understanding + Turn Arbiter rollout.
"""
from __future__ import annotations

import os
from typing import FrozenSet

_SHADOW_FLAG = "TURN_ARBITER_SHADOW_ENABLED"
_ENFORCE_FLAG = "TURN_ARBITER_ENFORCE_ENABLED"
_ENFORCE_TENANTS_FLAG = "TURN_ARBITER_ENFORCE_TENANTS"
_ENFORCE_MISMATCH_TYPES_FLAG = "TURN_ARBITER_ENFORCE_MISMATCH_TYPES"

_DEFAULT_ENFORCE_MISMATCH_TYPES = frozenset({
    "checkout_vs_support",
    "checkout_vs_discovery",
    "staff_vs_persona",
})


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_turn_arbiter_shadow_enabled() -> bool:
    """
    Phase 1 — shadow logging on by default.

    Set ``TURN_ARBITER_SHADOW_ENABLED=false`` to disable telemetry only.
    """
    raw = os.getenv(_SHADOW_FLAG, "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def is_turn_arbiter_enforce_enabled() -> bool:
    """Phase 2A — off by default. Enable via ``TURN_ARBITER_ENFORCE_ENABLED=true``."""
    return _truthy(os.getenv(_ENFORCE_FLAG, "false"))


def get_enforce_tenant_allowlist() -> FrozenSet[int]:
    """
    Optional tenant allowlist for gradual rollout.

    Empty / unset → enforce applies platform-wide (all tenants).
    Example: ``TURN_ARBITER_ENFORCE_TENANTS=33,44`` → only those tenants.
    """
    raw = os.getenv(_ENFORCE_TENANTS_FLAG, "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


def is_enforce_tenant(tenant_id: int | None) -> bool:
    """True when enforce may apply to this tenant."""
    if tenant_id is None:
        return False
    allowlist = get_enforce_tenant_allowlist()
    if not allowlist:
        return True
    return int(tenant_id) in allowlist


def get_enforce_mismatch_types() -> FrozenSet[str]:
    """
    Mismatch categories eligible for Phase 2A enforce.

    Default: checkout_vs_support, checkout_vs_discovery, staff_vs_persona
    """
    raw = os.getenv(_ENFORCE_MISMATCH_TYPES_FLAG, "").strip()
    if not raw:
        return _DEFAULT_ENFORCE_MISMATCH_TYPES
    return frozenset(
        p.strip()
        for p in raw.split(",")
        if p.strip()
    )


def should_prepare_turn_arbitration() -> bool:
    """True when shadow or enforce needs pre-decide synthesis."""
    return is_turn_arbiter_shadow_enabled() or is_turn_arbiter_enforce_enabled()


__all__ = [
    "get_enforce_mismatch_types",
    "get_enforce_tenant_allowlist",
    "is_enforce_tenant",
    "is_turn_arbiter_enforce_enabled",
    "is_turn_arbiter_shadow_enabled",
    "should_prepare_turn_arbitration",
]
