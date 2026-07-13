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
_TRUSTED_CONTEXT_SHADOW = "NAHLA_TRUSTED_CONTEXT_SHADOW_ENABLED"
_LAYER2_SHADOW = "NAHLA_LAYER2_SHADOW_ENABLED"


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


def is_trusted_context_shadow_enabled() -> bool:
    """Trusted Context snapshot shadow — on by default. Set false to disable."""
    raw = os.getenv(_TRUSTED_CONTEXT_SHADOW, "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def is_layer2_shadow_enabled() -> bool:
    """Layer 2 intent/decision shadow compare telemetry — off by default."""
    return _is_enabled(_LAYER2_SHADOW)


__all__ = [
    "is_layer2_shadow_enabled",
    "is_trusted_context_shadow_enabled",
    "is_truth_surface_shadow_enabled",
    "is_uts_v1_enforce_enabled",
    "is_uts_v1_shadow_enabled",
]
