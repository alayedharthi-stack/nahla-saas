"""
services.crm_atoms
──────────────────
The **bridge layer** between Nahla's per-customer CRM atoms and the
marketing cohorts defined in :mod:`services.nahla_segments`.

Two concrete jobs
─────────────────

1. **Stable named constants** for every CRM atom string. So instead of
   sprinkling ``"vip"`` / ``"at_risk"`` / ``"inactive"`` literals across
   the codebase, callers write ``CrmStatus.VIP`` / ``CrmStatus.AT_RISK``
   / ``CrmStatus.INACTIVE``. Typos become ``AttributeError`` at import
   time, not silent ``filter()`` no-ops at runtime.

2. **Atom → cohort mapping**, computed automatically from the
   :data:`services.nahla_segments.SEGMENTS` registry. Given a single
   customer's ``customer_status`` and ``rfm_segment`` columns, callers
   ask "which official Nahla marketing cohorts does this customer
   belong to right now?" and get an answer that is *guaranteed* to
   match what the segment registry says — because both are computed
   from the same source.

Why a separate module
─────────────────────

* :mod:`services.customer_intelligence` *writes* the atom columns
  (``compute_customer_status``, ``compute_rfm_segment``).
* :mod:`services.nahla_segments` defines the *cohorts* and how they
  consume the atoms (via SQL builders + declared
  ``crm_statuses`` / ``rfm_buckets`` tuples).
* This module is the single place that knows how to go *from atom to
  cohort* without re-reading SQL. Consumers (coupon generator, template
  library, autopilot rules, analytics) never need to import the
  registry just to ask "is this customer a VIP cohort member?"

Contract guarantees
───────────────────

* Every value in ``CrmStatus`` is in
  :data:`services.customer_intelligence.CUSTOMER_STATUS_ORDER`.
* Every value in ``RfmSegment`` is in
  :data:`services.customer_intelligence.RFM_SEGMENT_ORDER`.
* For every segment ``s`` in :data:`services.nahla_segments.SEGMENTS`
  and every status ``a`` in ``s.crm_statuses``,
  ``s.key in cohorts_for_status(a)``. (Round-trip — verified by
  ``tests/test_crm_atoms.py``.)
* ``canonical_status("churned")`` returns ``"inactive"``. The legacy
  ``churned`` value used to live in coupon_generator's
  ``SEGMENT_ALIASES`` — its single source of truth is now this file.

What stays atom-level vs cohort-level
─────────────────────────────────────

**Atom-level (per-customer, low-level)** — keeps using these constants
directly:
  * Coupon generator defaults / aliases (one customer's status drives
    one coupon decision).
  * Automation engine condition matching (one event, one customer's
    atoms compared against an allow-list).
  * Customer status badge rendering on the Customers page.

**Cohort-level (audience, high-level)** — should use cohort keys via
``cohorts_for_*`` helpers or directly via
:data:`services.nahla_segments.SEGMENTS`:
  * Campaign segment selection.
  * Template library "this template is for X audience" declarations.
  * Recommendation scoring.
  * Future: autopilot rules that target cohorts, analytics dashboards.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

from services.customer_intelligence import (
    CUSTOMER_STATUS_ORDER,
    RFM_SEGMENT_ORDER,
)
from services.nahla_segments import SEGMENTS


# ── Named constants for CRM atoms ───────────────────────────────────────────
#
# Plain string-valued classes (not Enum) so existing code that compares
# `profile.customer_status == "vip"` keeps working *and* code that uses
# `CrmStatus.VIP` produces the exact same string. No DB or API change.

class CrmStatus:
    """Named accessors for ``customer_profiles.customer_status`` values.

    Identical strings as the canonical
    :data:`CUSTOMER_STATUS_ORDER` enum — guaranteed by the
    ``test_crm_atoms.py`` contract test. Use these instead of bare
    string literals so typos surface as ``AttributeError`` at import
    time and refactors are findable with grep.
    """

    LEAD = "lead"
    NEW = "new"
    ACTIVE = "active"
    VIP = "vip"
    AT_RISK = "at_risk"
    INACTIVE = "inactive"


class RfmSegment:
    """Named accessors for ``customer_profiles.rfm_segment`` values."""

    LEAD = "lead"
    CHAMPIONS = "champions"
    LOYAL_CUSTOMERS = "loyal_customers"
    POTENTIAL_LOYALISTS = "potential_loyalists"
    NEW_CUSTOMERS = "new_customers"
    PROMISING = "promising"
    NEEDS_ATTENTION = "needs_attention"
    ABOUT_TO_SLEEP = "about_to_sleep"
    AT_RISK = "at_risk"
    CANT_LOSE_THEM = "cant_lose_them"
    HIBERNATING = "hibernating"
    LOST_CUSTOMERS = "lost_customers"
    REGULARS = "regulars"


# ── Aliases & normalization ─────────────────────────────────────────────────
#
# Legacy/historical strings that some callers (or DB rows from older
# schema versions) may still use. Single source of truth — moved here
# from coupon_generator so every caller normalizes the same way.

STATUS_ALIASES: Dict[str, str] = {
    # Old name from the pre-2025 status taxonomy. We never deleted the
    # rows so any code path that reads them must collapse `churned` to
    # the modern `inactive`.
    "churned": CrmStatus.INACTIVE,
}


def canonical_status(value: object, *, default: str = CrmStatus.ACTIVE) -> str:
    """Normalize a (possibly None / mixed-case / aliased) status string
    into the modern canonical value.

    Returns ``default`` (``"active"``) when the input is empty/None.
    Always returns a string that appears in
    :data:`CUSTOMER_STATUS_ORDER` *or* the literal input if it's an
    unrecognised but non-empty value (callers that want strict
    validation should use :func:`assert_status`).
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return STATUS_ALIASES.get(raw, raw)


# ── Strict validation (opt-in) ──────────────────────────────────────────────


_VALID_STATUSES: FrozenSet[str] = frozenset(CUSTOMER_STATUS_ORDER)
_VALID_RFM: FrozenSet[str] = frozenset(RFM_SEGMENT_ORDER)


def is_known_status(value: object) -> bool:
    return canonical_status(value, default="") in _VALID_STATUSES


def is_known_rfm(value: object) -> bool:
    raw = str(value or "").strip().lower()
    return raw in _VALID_RFM


def assert_status(value: object) -> str:
    """Strict variant of :func:`canonical_status` — raises ``ValueError``
    on unknown values. Use in seed/setup paths where a typo would
    silently produce an empty cohort."""
    canonical = canonical_status(value, default="")
    if canonical not in _VALID_STATUSES:
        raise ValueError(
            f"Unknown CRM status {value!r}. Expected one of "
            f"{tuple(CUSTOMER_STATUS_ORDER)} (aliases: {tuple(STATUS_ALIASES)})."
        )
    return canonical


def assert_rfm(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw not in _VALID_RFM:
        raise ValueError(
            f"Unknown RFM segment {value!r}. Expected one of "
            f"{tuple(RFM_SEGMENT_ORDER)}."
        )
    return raw


# ── Bridge: atom → cohort ──────────────────────────────────────────────────
#
# Built ONCE at import time by walking the registry. The maps include
# every `crm_statuses` / `rfm_buckets` value declared by every segment.
# A status that no segment consumes (e.g. `active` today) maps to an
# empty tuple — which is the correct answer ("this customer doesn't
# fall into any marketing cohort built on this atom alone").


def _build_status_to_cohorts() -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, list] = {s: [] for s in CUSTOMER_STATUS_ORDER}
    for seg in SEGMENTS:
        for status in seg.crm_statuses:
            # Defensive: registry's coherence_report() already prevents
            # this, but if a future segment adds an unknown status the
            # bridge silently drops it instead of polluting the map.
            if status in out:
                out[status].append(seg.key)
    return {k: tuple(v) for k, v in out.items()}


def _build_rfm_to_cohorts() -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, list] = {r: [] for r in RFM_SEGMENT_ORDER}
    for seg in SEGMENTS:
        for rfm in seg.rfm_buckets:
            if rfm in out:
                out[rfm].append(seg.key)
    return {k: tuple(v) for k, v in out.items()}


STATUS_TO_COHORTS: Dict[str, Tuple[str, ...]] = _build_status_to_cohorts()
"""Static map ``customer_status → tuple of Nahla cohort keys``.

Computed from the registry at import. Read-only at runtime — mutating
this dict is unsupported. Use :func:`cohorts_for_status` for the safe
accessor that handles aliases and unknown values.
"""

RFM_TO_COHORTS: Dict[str, Tuple[str, ...]] = _build_rfm_to_cohorts()
"""Static map ``rfm_segment → tuple of Nahla cohort keys``."""


def cohorts_for_status(status: object) -> Tuple[str, ...]:
    """Cohorts a customer with this ``customer_status`` belongs to.

    Handles aliases (``"churned"`` → ``"inactive"``), unknown values
    (returns empty tuple), and ``None`` (returns empty tuple). Never
    raises.
    """
    canonical = canonical_status(status, default="")
    return STATUS_TO_COHORTS.get(canonical, ())


def cohorts_for_rfm(rfm: object) -> Tuple[str, ...]:
    """Cohorts a customer with this ``rfm_segment`` belongs to."""
    raw = str(rfm or "").strip().lower()
    return RFM_TO_COHORTS.get(raw, ())


def cohorts_for_customer(
    status: object = None,
    rfm: object = None,
) -> Tuple[str, ...]:
    """Union of cohorts a customer belongs to given their two atoms.

    Order is preserved (status-derived cohorts first, then RFM-derived
    that aren't already there) so callers can use the first element as
    "the most representative" cohort. The ``"all"`` cohort is *not*
    included automatically — every customer is in ``"all"`` by
    definition; callers that want it should add it explicitly.
    """
    seen: set = set()
    result: list = []
    for key in cohorts_for_status(status):
        if key not in seen:
            seen.add(key)
            result.append(key)
    for key in cohorts_for_rfm(rfm):
        if key not in seen:
            seen.add(key)
            result.append(key)
    return tuple(result)


# ── Convenience: derive cohort_keys for a template library entry ───────────


def derive_cohort_keys_for_template(meta: Dict[str, object]) -> Tuple[str, ...]:
    """Compute ``cohort_keys`` for a template-library entry from its
    declared ``customer_statuses`` / ``rfm_segments``.

    Used by ``routers/templates.py DEFAULT_TEMPLATE_LIBRARY`` so each
    template carries explicit cohort intent without the author having to
    duplicate the cohort list manually. Drift impossible — cohorts are
    derived from the registry, not hand-typed.

    If the entry already declares an explicit ``cohort_keys`` tuple/list,
    that wins (lets a template author override the auto-derivation if
    the heuristic is wrong for their case).
    """
    explicit = meta.get("cohort_keys")
    if explicit:
        return tuple(str(k).strip().lower() for k in explicit if k)

    seen: set = set()
    out: list = []
    for status in meta.get("customer_statuses") or ():
        for cohort_key in cohorts_for_status(status):
            if cohort_key not in seen and cohort_key != "all":
                seen.add(cohort_key)
                out.append(cohort_key)
    for rfm in meta.get("rfm_segments") or ():
        for cohort_key in cohorts_for_rfm(rfm):
            if cohort_key not in seen and cohort_key != "all":
                seen.add(cohort_key)
                out.append(cohort_key)
    return tuple(out)


__all__ = [
    "CrmStatus",
    "RfmSegment",
    "STATUS_ALIASES",
    "STATUS_TO_COHORTS",
    "RFM_TO_COHORTS",
    "canonical_status",
    "is_known_status",
    "is_known_rfm",
    "assert_status",
    "assert_rfm",
    "cohorts_for_status",
    "cohorts_for_rfm",
    "cohorts_for_customer",
    "derive_cohort_keys_for_template",
]
