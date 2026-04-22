"""
campaign_wizard.segments
────────────────────────
Backward-compatibility shim.

The canonical Nahla segment registry now lives in
``services.nahla_segments`` and is consumed by both the campaign
wizard *and* the customers page (and, in the future, the autopilot
and analytics surfaces).

This module re-exports the public surface under the old import path
so existing imports (and any external code depending on the wizard
package) keep working without modification. **Do not add new
definitions here** — extend ``services.nahla_segments`` instead.
"""
from __future__ import annotations

from services.nahla_segments import (
    HIGH_SPENDER_LTV_THRESHOLD,
    NahlaSegment as CustomerSegment,  # legacy alias used by old tests
    SEGMENTS,
    all_segment_keys,
    build_segment_query,
    coherence_report,
    count_segment,
    get_segment,
    list_segments_with_counts,
    sample_segment,
    serialize_segment,
)

# Re-expose the masking helpers under their original (private) names so
# the unit tests that import them via the old path keep working.
from services.nahla_segments import _mask_email, _mask_phone  # noqa: F401

__all__ = [
    "CustomerSegment",
    "SEGMENTS",
    "HIGH_SPENDER_LTV_THRESHOLD",
    "all_segment_keys",
    "build_segment_query",
    "coherence_report",
    "count_segment",
    "get_segment",
    "list_segments_with_counts",
    "sample_segment",
    "serialize_segment",
]
