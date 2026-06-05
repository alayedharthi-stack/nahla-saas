"""
truth_surface/flags.py
──────────────────────
Feature flags for truth surface Phase 1 inventory and Phase 2 UTS v1.
"""
from __future__ import annotations

import os

_PHASE1_SHADOW = "NAHLA_TRUTH_SURFACE_SHADOW_ENABLED"
_UTS_V1_SHADOW = "NAHLA_UTS_V1_SHADOW_ENABLED"
_UTS_V1_ENFORCE = "NAHLA_UTS_V1_ENFORCE_ENABLED"


def _is_enabled(flag: str) -> bool:
    return os.getenv(flag, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_truth_surface_shadow_enabled() -> bool:
    """Phase 1 full-surface inventory shadow (opt-in)."""
    return _is_enabled(_PHASE1_SHADOW)


def is_uts_v1_shadow_enabled() -> bool:
    """Phase 2 UTS v1 manifest + integrity gate shadow (opt-in)."""
    return _is_enabled(_UTS_V1_SHADOW)


def is_uts_v1_enforce_enabled() -> bool:
    """
    Phase 2+ enforce flag — default false.

    In Phase 2 shadow rollout this flag does NOT modify prompts.
    """
    return _is_enabled(_UTS_V1_ENFORCE)


__all__ = [
    "is_truth_surface_shadow_enabled",
    "is_uts_v1_enforce_enabled",
    "is_uts_v1_shadow_enabled",
]
