"""
clarification/flags.py
──────────────────────
Feature flags for phased clarification architecture rollout.
"""
from __future__ import annotations

import os

_FLAG = "CONTEXTUAL_CLARIFY_ENABLED"
_SHADOW_FLAG = "CLARIFICATION_SHADOW_ENABLED"


def is_contextual_clarify_enabled() -> bool:
    """Phase 1 — when True, generative contextual clarify replaces legacy template fallback."""
    return os.getenv(_FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_clarification_shadow_enabled() -> bool:
    """
    Phase 0 — shadow logging on by default.

    Set ``CLARIFICATION_SHADOW_ENABLED=false`` to disable telemetry only.
    """
    raw = os.getenv(_SHADOW_FLAG, "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


__all__ = [
    "is_clarification_shadow_enabled",
    "is_contextual_clarify_enabled",
]
