"""
turn/mismatch.py
────────────────
Classify shadow owner mismatches for architectural telemetry.

Read-only — never affects reply routing or state mutations.
"""
from __future__ import annotations

from .contract import (
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_ORDERING,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
)
from .legacy_owner import owners_compatible

MISMATCH_NONE = "none"
MISMATCH_CHECKOUT_VS_SUPPORT = "checkout_vs_support"
MISMATCH_CHECKOUT_VS_DISCOVERY = "checkout_vs_discovery"
MISMATCH_STAFF_VS_PERSONA = "staff_vs_persona"
MISMATCH_CHECKOUT_VS_PERSONA = "checkout_vs_persona"
MISMATCH_SUPPORT_VS_ORDERING = "support_vs_ordering"
MISMATCH_UNKNOWN = "unknown_mismatch"

_CHECKOUT_LIKE = frozenset({OWNER_CHECKOUT, OWNER_ORDERING})
_SUPPORT_LIKE = frozenset({OWNER_SUPPORT, OWNER_POST_PURCHASE})


def classify_owner_mismatch(
    proposed_owner: str,
    legacy_owner: str,
    *,
    owner_mismatch: bool | None = None,
) -> str:
    """
    Return a stable mismatch category for shadow telemetry.

    When owners are compatible (or identical), returns ``none``.
    """
    mismatch = (
        owner_mismatch
        if owner_mismatch is not None
        else not owners_compatible(proposed_owner, legacy_owner)
    )
    if not mismatch:
        return MISMATCH_NONE

    if legacy_owner in _CHECKOUT_LIKE and proposed_owner in _SUPPORT_LIKE:
        return MISMATCH_CHECKOUT_VS_SUPPORT

    if legacy_owner in _CHECKOUT_LIKE and proposed_owner == OWNER_DISCOVERY:
        return MISMATCH_CHECKOUT_VS_DISCOVERY

    if legacy_owner == OWNER_STAFF_ESCALATION and proposed_owner == OWNER_PERSONA_SOCIAL:
        return MISMATCH_STAFF_VS_PERSONA

    if legacy_owner in _CHECKOUT_LIKE and proposed_owner == OWNER_PERSONA_SOCIAL:
        return MISMATCH_CHECKOUT_VS_PERSONA

    if legacy_owner in _SUPPORT_LIKE and proposed_owner in _CHECKOUT_LIKE:
        return MISMATCH_SUPPORT_VS_ORDERING

    if proposed_owner in _SUPPORT_LIKE and legacy_owner in _CHECKOUT_LIKE:
        return MISMATCH_CHECKOUT_VS_SUPPORT

    return MISMATCH_UNKNOWN


__all__ = [
    "MISMATCH_CHECKOUT_VS_DISCOVERY",
    "MISMATCH_CHECKOUT_VS_PERSONA",
    "MISMATCH_CHECKOUT_VS_SUPPORT",
    "MISMATCH_NONE",
    "MISMATCH_STAFF_VS_PERSONA",
    "MISMATCH_SUPPORT_VS_ORDERING",
    "MISMATCH_UNKNOWN",
    "classify_owner_mismatch",
]
