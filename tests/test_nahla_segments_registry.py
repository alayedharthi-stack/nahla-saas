"""
tests/test_nahla_segments_registry.py
─────────────────────────────────────
Coherence and contract tests for the canonical Nahla segment registry
(`services.nahla_segments`).

These tests do NOT exercise SQL — that is covered exhaustively in
`tests/test_campaign_wizard.py`. Here we assert the structural
invariants that make the registry safe to be the single source of
truth for Campaigns + Customers + future Autopilot:

  1. Every segment declares both human and machine metadata
     (label_ar, criteria_ar, icon, etc.) — never an empty string.
  2. Every CRM status / RFM bucket referenced in a segment's
     `crm_statuses` / `rfm_buckets` actually exists in the canonical
     enums in `customer_intelligence.py`. Drift here would mean a
     chip's "what does this mean?" tooltip lies about the data.
  3. The wizard's backward-compat shim (`campaign_wizard.segments`)
     re-exports the SAME registry — so a future contributor who
     `from services.campaign_wizard.segments import SEGMENTS` cannot
     accidentally edit a different list.
  4. The serialized JSON shape matches what the frontend expects.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services import nahla_segments
from services.nahla_segments import (
    HIGH_SPENDER_LTV_THRESHOLD,
    NahlaSegment,
    SEGMENTS,
    all_segment_keys,
    coherence_report,
    get_segment,
    serialize_segment,
)
from services.customer_intelligence import (
    CUSTOMER_STATUS_ORDER,
    RFM_SEGMENT_ORDER,
)


class TestRegistryShape:
    def test_segments_is_a_tuple_of_NahlaSegment(self):
        assert isinstance(SEGMENTS, tuple)
        assert all(isinstance(s, NahlaSegment) for s in SEGMENTS)
        assert len(SEGMENTS) >= 13   # documented minimum

    def test_keys_are_unique_and_lowercase(self):
        keys = [s.key for s in SEGMENTS]
        assert len(set(keys)) == len(keys), f"duplicate segment keys in {keys}"
        for k in keys:
            assert k == k.lower(), f"segment key '{k}' must be lowercase"
            assert " " not in k, f"segment key '{k}' must not contain spaces"

    def test_every_segment_has_human_metadata(self):
        for s in SEGMENTS:
            assert s.label_ar.strip(), f"segment '{s.key}' has empty label_ar"
            assert s.label_en.strip(), f"segment '{s.key}' has empty label_en"
            assert s.description_ar.strip(), \
                f"segment '{s.key}' has empty description_ar"
            assert len(s.criteria_ar.strip()) >= 30, (
                f"segment '{s.key}' criteria_ar is too short ({len(s.criteria_ar)} chars) — "
                "the merchant-facing definition needs to actually explain the cohort."
            )
            assert s.icon.strip(), f"segment '{s.key}' has no icon"

    def test_every_natural_goal_is_a_string(self):
        for s in SEGMENTS:
            assert isinstance(s.natural_goals, tuple)
            for g in s.natural_goals:
                assert isinstance(g, str) and g, \
                    f"segment '{s.key}' has invalid natural_goal {g!r}"


class TestRegistryCoherence:
    """The whole point of the registry is "one place for definitions".
    These tests fail if a segment claims to read a CRM/RFM value that
    no production code actually writes — which is exactly the silent
    bug we just fixed for `lost`/`vip`/`new` and never want again.
    """

    def test_coherence_report_has_no_errors(self):
        report = coherence_report()
        assert report["errors"] == [], (
            "Nahla segment registry is INCOHERENT with customer_intelligence.py:\n"
            + "\n".join(f"  - {e}" for e in report["errors"])
        )
        assert report["segment_count"] == len(SEGMENTS)

    def test_every_listed_crm_status_is_canonical(self):
        for s in SEGMENTS:
            for status in s.crm_statuses:
                assert status in CUSTOMER_STATUS_ORDER, (
                    f"segment '{s.key}' references CRM status '{status}' "
                    f"which is NOT in the canonical enum "
                    f"CUSTOMER_STATUS_ORDER={CUSTOMER_STATUS_ORDER}"
                )

    def test_every_listed_rfm_bucket_is_canonical(self):
        for s in SEGMENTS:
            for bucket in s.rfm_buckets:
                assert bucket in RFM_SEGMENT_ORDER, (
                    f"segment '{s.key}' references RFM bucket '{bucket}' "
                    f"which is NOT in the canonical enum "
                    f"RFM_SEGMENT_ORDER={RFM_SEGMENT_ORDER}"
                )

    def test_no_segment_uses_legacy_churned_value(self):
        """Regression: the legacy `'churned'` value briefly lived in
        SEGMENTS but was never written by `compute_customer_status`,
        which silently emptied the `lost` cohort. Lock it out."""
        for s in SEGMENTS:
            assert "churned" not in s.crm_statuses, (
                f"segment '{s.key}' still references the legacy "
                "CRM value 'churned' which compute_customer_status never writes"
            )
            assert "vip" not in s.rfm_buckets, (
                f"segment '{s.key}' references rfm_segment 'vip' "
                "which compute_rfm_segment never writes — use 'champions' / "
                "'cant_lose_them' instead"
            )

    def test_high_spender_threshold_is_documented_in_criteria(self):
        seg = get_segment("high_spenders")
        assert seg is not None
        assert f"{HIGH_SPENDER_LTV_THRESHOLD:.2f}" in seg.criteria_ar, (
            "high_spenders criteria_ar must mention the LTV threshold "
            f"({HIGH_SPENDER_LTV_THRESHOLD}) so the merchant knows what "
            "'high spender' actually means."
        )


class TestPublicAPI:
    def test_get_segment_normalises_input(self):
        assert get_segment("VIP").key == "vip"
        assert get_segment("  All  ").key == "all"
        assert get_segment("") is None
        assert get_segment(None) is None  # type: ignore[arg-type]
        assert get_segment("does-not-exist") is None

    def test_all_segment_keys_matches_registry(self):
        assert all_segment_keys() == tuple(s.key for s in SEGMENTS)

    def test_serialize_segment_emits_full_contract(self):
        sample = SEGMENTS[0]
        out = serialize_segment(sample, customer_count=42)
        # Frontend depends on every one of these field names.
        for required in (
            "key", "label_ar", "label_en",
            "description_ar", "criteria_ar",
            "icon", "natural_goals",
            "crm_statuses", "rfm_buckets",
            "customer_count",
        ):
            assert required in out, f"serialize_segment missing '{required}'"
        assert out["customer_count"] == 42
        assert isinstance(out["natural_goals"], list)
        assert isinstance(out["crm_statuses"], list)
        assert isinstance(out["rfm_buckets"], list)


class TestBackwardCompatShim:
    """The campaign wizard imported SEGMENTS via
    `services.campaign_wizard.segments` for months — and lots of tests
    + the wizard router still do. The shim must re-export the SAME
    objects, not a stale copy."""

    def test_shim_re_exports_canonical_registry(self):
        from services.campaign_wizard import segments as wizard_segments
        assert wizard_segments.SEGMENTS is SEGMENTS, (
            "campaign_wizard.segments.SEGMENTS must BE the canonical "
            "tuple, not a copy — otherwise edits in one place won't "
            "show up in the other."
        )

    def test_shim_exposes_legacy_customer_segment_alias(self):
        from services.campaign_wizard.segments import CustomerSegment
        # Used by older tests + any external code that imported the
        # dataclass by its previous name.
        assert CustomerSegment is NahlaSegment

    def test_shim_exposes_helpers(self):
        from services.campaign_wizard import segments as ws
        assert ws.build_segment_query is nahla_segments.build_segment_query
        assert ws.count_segment        is nahla_segments.count_segment
        assert ws.list_segments_with_counts is nahla_segments.list_segments_with_counts
        assert ws.sample_segment       is nahla_segments.sample_segment
        assert ws.get_segment          is nahla_segments.get_segment
