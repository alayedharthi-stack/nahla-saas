"""
tests/test_crm_atoms.py
───────────────────────
Contract tests for the **CRM-atom ↔ marketing-cohort bridge** layer
(``services.crm_atoms``).

These tests pin the guarantees that consumers (coupon generator,
template library, autopilot seed, automation engine) rely on:

  1. Every named constant on ``CrmStatus`` / ``RfmSegment`` is a real
     value of the canonical enums in ``services.customer_intelligence``.
     Adding a new enum value without exposing it as a constant is OK;
     exposing a constant that doesn't exist as an enum value is a bug.

  2. ``canonical_status`` collapses legacy aliases (``"churned"`` →
     ``"inactive"``) and treats empty/None as the documented default.

  3. ``cohorts_for_status`` / ``cohorts_for_rfm`` are *consistent* with
     the registry: every atom listed in a cohort's ``crm_statuses`` /
     ``rfm_buckets`` round-trips back to that cohort's key. This is the
     single most important invariant — break it and the bridge silently
     lies about what cohort a customer belongs to.

  4. The legacy back-compat shim re-exported by ``coupon_generator``
     (``_canonical_segment``, ``SEGMENT_ALIASES``) still resolves
     ``"churned"`` → ``"inactive"`` so ``offer_decision_service`` keeps
     working unchanged.

  5. Every entry in ``DEFAULT_TEMPLATE_LIBRARY`` (after enrichment)
     carries a non-empty ``cohort_keys`` list — otherwise the wizard
     recommender's cohort-level boost would silently fail to fire.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services import crm_atoms  # noqa: E402
from services.crm_atoms import (  # noqa: E402
    CrmStatus,
    RfmSegment,
    RFM_TO_COHORTS,
    STATUS_ALIASES,
    STATUS_TO_COHORTS,
    assert_rfm,
    assert_status,
    canonical_status,
    cohorts_for_customer,
    cohorts_for_rfm,
    cohorts_for_status,
    derive_cohort_keys_for_template,
    is_known_rfm,
    is_known_status,
)
from services.customer_intelligence import (  # noqa: E402
    CUSTOMER_STATUS_ORDER,
    RFM_SEGMENT_ORDER,
)
from services.nahla_segments import SEGMENTS  # noqa: E402


# ── 1. Constants vs canonical enums ──────────────────────────────────────────


def _public_string_attrs(cls) -> dict[str, str]:
    """Return ``{ATTR_NAME: value}`` for every uppercase, str-valued
    attribute on a constants class."""
    out: dict[str, str] = {}
    for name in dir(cls):
        if name.startswith("_") or not name.isupper():
            continue
        v = getattr(cls, name)
        if isinstance(v, str):
            out[name] = v
    return out


def test_every_crm_status_constant_is_in_canonical_enum():
    constants = _public_string_attrs(CrmStatus)
    assert constants, "CrmStatus exposes no constants — refactor regression"
    canonical = set(CUSTOMER_STATUS_ORDER)
    for name, value in constants.items():
        assert value in canonical, (
            f"CrmStatus.{name} = {value!r} is NOT in CUSTOMER_STATUS_ORDER "
            f"({CUSTOMER_STATUS_ORDER}). Either add it to the enum or "
            "remove the constant."
        )


def test_every_rfm_segment_constant_is_in_canonical_enum():
    constants = _public_string_attrs(RfmSegment)
    assert constants, "RfmSegment exposes no constants — refactor regression"
    canonical = set(RFM_SEGMENT_ORDER)
    for name, value in constants.items():
        assert value in canonical, (
            f"RfmSegment.{name} = {value!r} is NOT in RFM_SEGMENT_ORDER. "
            "Either add it to the enum or remove the constant."
        )


def test_crm_status_constants_cover_every_canonical_value():
    """Soft check: every canonical status SHOULD have a constant. If a
    new value is added to the enum without a constant, this test fails
    so the contributor is reminded to add one."""
    exposed = set(_public_string_attrs(CrmStatus).values())
    missing = set(CUSTOMER_STATUS_ORDER) - exposed
    assert not missing, (
        f"CUSTOMER_STATUS_ORDER values without a CrmStatus.* constant: "
        f"{sorted(missing)}. Add named accessors so callers stop typing "
        "string literals."
    )


# ── 2. Aliases & normalization ──────────────────────────────────────────────


def test_canonical_status_collapses_churned_to_inactive():
    assert canonical_status("churned") == CrmStatus.INACTIVE
    assert canonical_status("CHURNED") == CrmStatus.INACTIVE
    assert canonical_status("  churned  ") == CrmStatus.INACTIVE


def test_canonical_status_returns_default_for_empty_or_none():
    assert canonical_status(None) == CrmStatus.ACTIVE
    assert canonical_status("") == CrmStatus.ACTIVE
    assert canonical_status("   ") == CrmStatus.ACTIVE
    # Custom default flows through.
    assert canonical_status(None, default="lead") == "lead"


def test_canonical_status_passes_unknown_values_through():
    """Lenient mode (the default) returns the input as-is for unknown
    values so callers can decide whether to error or just ignore. Use
    ``assert_status`` for strict validation."""
    assert canonical_status("totally_made_up") == "totally_made_up"


def test_status_aliases_only_map_to_canonical_values():
    """Every alias VALUE must be a real canonical status — otherwise the
    alias points at a ghost."""
    canonical = set(CUSTOMER_STATUS_ORDER)
    for alias, target in STATUS_ALIASES.items():
        assert target in canonical, (
            f"STATUS_ALIASES[{alias!r}] points at {target!r} which is "
            f"NOT in CUSTOMER_STATUS_ORDER {CUSTOMER_STATUS_ORDER}."
        )


def test_assert_status_raises_on_unknown_and_returns_canonical():
    assert assert_status("vip") == CrmStatus.VIP
    assert assert_status("CHURNED") == CrmStatus.INACTIVE
    with pytest.raises(ValueError, match="Unknown CRM status"):
        assert_status("nope")


def test_assert_rfm_raises_on_unknown_and_returns_canonical():
    assert assert_rfm("champions") == RfmSegment.CHAMPIONS
    with pytest.raises(ValueError, match="Unknown RFM segment"):
        assert_rfm("nope")


def test_is_known_helpers_are_lenient():
    assert is_known_status("vip") is True
    assert is_known_status("churned") is True  # alias
    assert is_known_status("nope") is False
    assert is_known_rfm("champions") is True
    assert is_known_rfm("nope") is False


# ── 3. Bridge round-trip vs registry ────────────────────────────────────────


def test_status_to_cohorts_round_trips_with_registry():
    """The headline invariant: for every cohort and every CRM status
    that cohort claims to consume, the bridge maps that status back to
    the cohort key."""
    for seg in SEGMENTS:
        for status in seg.crm_statuses:
            cohorts = cohorts_for_status(status)
            assert seg.key in cohorts, (
                f"Cohort '{seg.key}' declares it consumes CRM status "
                f"{status!r} but cohorts_for_status({status!r}) returned "
                f"{cohorts}. The bridge is out of sync with the registry."
            )


def test_rfm_to_cohorts_round_trips_with_registry():
    for seg in SEGMENTS:
        for rfm in seg.rfm_buckets:
            cohorts = cohorts_for_rfm(rfm)
            assert seg.key in cohorts, (
                f"Cohort '{seg.key}' declares it consumes RFM bucket "
                f"{rfm!r} but cohorts_for_rfm({rfm!r}) returned {cohorts}. "
                "The bridge is out of sync with the registry."
            )


def test_status_to_cohorts_only_contains_known_cohort_keys():
    """The bridge must never invent cohort keys."""
    valid_keys = {s.key for s in SEGMENTS}
    for status, cohorts in STATUS_TO_COHORTS.items():
        for key in cohorts:
            assert key in valid_keys, (
                f"STATUS_TO_COHORTS[{status!r}] contains unknown cohort "
                f"{key!r}."
            )


def test_rfm_to_cohorts_only_contains_known_cohort_keys():
    valid_keys = {s.key for s in SEGMENTS}
    for rfm, cohorts in RFM_TO_COHORTS.items():
        for key in cohorts:
            assert key in valid_keys, (
                f"RFM_TO_COHORTS[{rfm!r}] contains unknown cohort {key!r}."
            )


def test_status_to_cohorts_keys_match_canonical_enum():
    assert set(STATUS_TO_COHORTS.keys()) == set(CUSTOMER_STATUS_ORDER)


def test_rfm_to_cohorts_keys_match_canonical_enum():
    assert set(RFM_TO_COHORTS.keys()) == set(RFM_SEGMENT_ORDER)


def test_cohorts_for_status_handles_alias_and_unknown_safely():
    # Alias resolves through canonical_status first.
    assert cohorts_for_status("CHURNED") == cohorts_for_status(CrmStatus.INACTIVE)
    # Unknown values return empty, never raise.
    assert cohorts_for_status("nope") == ()
    assert cohorts_for_status(None) == ()


def test_cohorts_for_rfm_handles_unknown_safely():
    assert cohorts_for_rfm("nope") == ()
    assert cohorts_for_rfm(None) == ()


# ── 4. Concrete expectations callers depend on ──────────────────────────────


def test_vip_status_lands_in_vip_cohort():
    assert "vip" in cohorts_for_status(CrmStatus.VIP)


def test_inactive_status_lands_in_lost_cohort():
    assert "lost" in cohorts_for_status(CrmStatus.INACTIVE)


def test_at_risk_status_lands_in_dormant_cohort():
    assert "dormant" in cohorts_for_status(CrmStatus.AT_RISK)


def test_lead_and_new_statuses_land_in_new_cohort():
    assert "new" in cohorts_for_status(CrmStatus.LEAD)
    assert "new" in cohorts_for_status(CrmStatus.NEW)


def test_cohorts_for_customer_unions_status_and_rfm_without_duplicates():
    cohorts = cohorts_for_customer(
        status=CrmStatus.VIP,
        rfm=RfmSegment.CHAMPIONS,
    )
    # `vip` is reachable through both — should appear exactly once.
    assert cohorts.count("vip") == 1
    assert "vip" in cohorts


def test_cohorts_for_customer_excludes_all_cohort():
    """The 'all' cohort is universal — including it in every customer's
    cohort list would be noise. Verified explicitly."""
    cohorts = cohorts_for_customer(status=CrmStatus.VIP, rfm=RfmSegment.CHAMPIONS)
    assert "all" not in cohorts


def test_active_status_has_no_explicit_cohort_today():
    """`active` is intentionally NOT in any cohort's ``crm_statuses``
    today — it is the default state and not a marketing target on its
    own. If this changes, the test should be updated to reflect the new
    intent (rather than being silently broken)."""
    assert cohorts_for_status(CrmStatus.ACTIVE) == ()


# ── 5. Back-compat shim through coupon_generator ───────────────────────────


def test_coupon_generator_canonical_segment_shim_still_works():
    """``services.coupon_generator._canonical_segment`` is consumed by
    ``services.offer_decision_service`` and several tests via direct
    import. After Phase-3 refactor it is now a re-export from
    ``crm_atoms.canonical_status`` — verify behavior is preserved."""
    from services.coupon_generator import _canonical_segment, SEGMENT_ALIASES, SEGMENT_DEFAULTS

    assert _canonical_segment("churned") == "inactive"
    assert _canonical_segment("  VIP  ") == "vip"
    assert _canonical_segment(None) == "active"
    assert _canonical_segment("") == "active"
    # Aliases dict must still expose the legacy mapping.
    assert SEGMENT_ALIASES.get("churned") == "inactive"
    # Defaults still keyed by atom strings.
    assert SEGMENT_DEFAULTS["vip"]["discount_pct"] == 20
    assert SEGMENT_DEFAULTS["inactive"]["discount_pct"] == 30


def test_coupon_generator_event_driven_segments_unchanged():
    """``EVENT_DRIVEN_SEGMENTS`` membership drives whether a status
    transition triggers an automatic coupon. Behavior must not change
    just because the values are now expressed via ``CrmStatus.*``."""
    from services.coupon_generator import EVENT_DRIVEN_SEGMENTS

    assert EVENT_DRIVEN_SEGMENTS == frozenset({"new", "active", "vip", "at_risk"})


# ── 6. Template library auto-derivation ─────────────────────────────────────


def test_default_template_library_entries_derive_non_empty_cohort_keys():
    """Every template library entry must produce a non-empty
    ``cohort_keys`` list after enrichment — otherwise the recommender's
    cohort-level boost (`+20`) silently fails to fire and we are back
    to the keyword-only heuristic."""
    from routers.templates import DEFAULT_TEMPLATE_LIBRARY, _enrich_library_meta

    for name, meta in DEFAULT_TEMPLATE_LIBRARY.items():
        enriched = _enrich_library_meta(meta)
        assert enriched.get("cohort_keys"), (
            f"DEFAULT_TEMPLATE_LIBRARY[{name!r}] derives empty "
            "cohort_keys. Either add explicit `cohort_keys` to the entry "
            "or update its `customer_statuses`/`rfm_segments` so the "
            "bridge can map them to a real cohort."
        )


def test_explicit_cohort_keys_overrides_atom_derivation():
    """If a template library entry declares its own ``cohort_keys``,
    those win — used by ``abandoned_cart_reminder`` because the
    abandoned-cart cohort is signal-driven (not atom-driven)."""
    derived = derive_cohort_keys_for_template({
        "cohort_keys": ["vip", "high_spenders"],
        # These would normally derive `new`; the explicit override
        # takes precedence.
        "customer_statuses": ["lead", "new"],
    })
    assert derived == ("vip", "high_spenders")


def test_derive_cohort_keys_drops_all_cohort():
    derived = derive_cohort_keys_for_template({
        "customer_statuses": ["vip"],
        "rfm_segments": [],
    })
    assert "all" not in derived


def test_abandoned_cart_template_carries_abandoned_cart_cohort_key():
    from routers.templates import DEFAULT_TEMPLATE_LIBRARY, _enrich_library_meta
    enriched = _enrich_library_meta(DEFAULT_TEMPLATE_LIBRARY["abandoned_cart_reminder"])
    assert "abandoned_cart" in enriched["cohort_keys"]


# ── 7. Module surface ───────────────────────────────────────────────────────


def test_public_api_is_exported():
    expected = {
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
    }
    assert expected.issubset(set(crm_atoms.__all__))
