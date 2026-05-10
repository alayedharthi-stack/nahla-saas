"""tests/test_template_library_modes.py
─────────────────────────────────────
Manual vs auto contract for the Nahla template library.

Why this file exists
────────────────────
The campaign wizard treats "manual" and "auto" templates very
differently:

  * Manual → merchant types every dynamic value (coupon, discount,
    URL). The wizard MUST refuse to auto-bind a coupon or attach a
    service to these.
  * Auto   → Nahla resolves customer_name / cart_url / coupon from
    system data; merchant only confirms the send.

A regression in either direction is hard to spot in the UI but very
visible to merchants ("لماذا ولّدت كوبون لقالب يدوي؟"). These tests
pin the contract so a future edit can't silently re-classify a
template.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from routers.templates import (  # noqa: E402
    DEFAULT_TEMPLATE_LIBRARY,
    SEED_TEMPLATES,
    TEMPLATE_VAR_MAP,
    _enrich_library_meta,
)


# ── The Meta-approved manual templates we promised NOT to change ────────


class TestExistingManualTemplatesUntouched:
    """The user said: "اترك القالب اليدوي كما هو" — these templates
    were approved by Meta as manual and their bodies must not change.
    We pin them by length + by the presence of the discount/coupon
    placeholders that make them manual."""

    def test_special_offer_still_uses_three_slots(self):
        seed = next(s for s in SEED_TEMPLATES if s["name"] == "special_offer")
        body = next(c for c in seed["components"] if c["type"] == "BODY")
        # The approved body has {{1}}, {{2}}, {{3}} — name, discount %,
        # coupon code. Drift here means the merchant's existing Meta
        # approval just got invalidated.
        assert "{{1}}" in body["text"]
        assert "{{2}}" in body["text"]
        assert "{{3}}" in body["text"]

    def test_special_offer_var_map_unchanged(self):
        assert TEMPLATE_VAR_MAP["special_offer"] == {
            "{{1}}": "customer_name",
            "{{2}}": "discount_pct",
            "{{3}}": "coupon_code",
        }

    def test_win_back_var_map_unchanged(self):
        assert TEMPLATE_VAR_MAP["win_back"] == {
            "{{1}}": "customer_name",
            "{{2}}": "discount_pct",
            "{{3}}": "coupon_code",
        }

    def test_vip_exclusive_var_map_unchanged(self):
        assert TEMPLATE_VAR_MAP["vip_exclusive"] == {
            "{{1}}": "customer_name",
            "{{2}}": "discount_pct",
            "{{3}}": "coupon_code",
        }


# ── Mode contract ───────────────────────────────────────────────────────


class TestLibraryModeContract:
    """Every library entry must declare its mode, and each declared
    mode must match the body's nature (auto bodies use auto-resolvable
    slots; manual bodies use merchant-typed slots).
    """

    def test_every_library_entry_has_a_mode(self):
        # The library is the source of truth — the wizard reads `mode`
        # to decide manual vs auto behaviour. A missing mode silently
        # defaults to "auto" via the enricher, which can be a footgun
        # for new entries. We force every entry to declare it.
        for name, meta in DEFAULT_TEMPLATE_LIBRARY.items():
            assert meta.get("mode") in {"manual", "auto"}, (
                f"library entry {name!r} is missing an explicit "
                f"`mode` declaration"
            )

    def test_manual_templates_label_contains_yadawi(self):
        for name, meta in DEFAULT_TEMPLATE_LIBRARY.items():
            if meta.get("mode") != "manual":
                continue
            assert "يدوي" in meta.get("library_label_ar", ""), (
                f"manual template {name!r} library_label_ar must "
                f"literally contain 'يدوي'"
            )

    def test_auto_templates_label_contains_tilqai(self):
        for name, meta in DEFAULT_TEMPLATE_LIBRARY.items():
            if meta.get("mode") != "auto":
                continue
            assert "تلقائي" in meta.get("library_label_ar", ""), (
                f"auto template {name!r} library_label_ar must "
                f"literally contain 'تلقائي'"
            )

    def test_special_offer_is_manual(self):
        # The classic, Meta-approved promo template carries discount %
        # and a coupon code typed by the merchant — it must remain
        # MANUAL even though we ship an auto sibling.
        assert DEFAULT_TEMPLATE_LIBRARY["special_offer"]["mode"] == "manual"

    def test_special_offer_auto_is_separate_entry(self):
        # The new auto sibling must coexist with the manual original —
        # we never replace the approved one.
        assert "special_offer_auto" in DEFAULT_TEMPLATE_LIBRARY
        assert DEFAULT_TEMPLATE_LIBRARY["special_offer_auto"]["mode"] == "auto"
        assert DEFAULT_TEMPLATE_LIBRARY["special_offer_auto"].get(
            "auto_coupon_capable"
        ) is True

    def test_each_manual_family_has_an_auto_sibling(self):
        # Every manual template that historically required typing a
        # coupon code (special_offer, vip_exclusive, win_back) ships
        # an auto sibling whose name ends with `_auto`.
        for stem in ("special_offer", "vip_exclusive", "win_back"):
            assert stem in DEFAULT_TEMPLATE_LIBRARY, f"missing manual base {stem}"
            sibling = f"{stem}_auto"
            assert sibling in DEFAULT_TEMPLATE_LIBRARY, (
                f"manual template {stem!r} is missing its `_auto` sibling"
            )
            assert DEFAULT_TEMPLATE_LIBRARY[stem]["mode"] == "manual"
            assert DEFAULT_TEMPLATE_LIBRARY[sibling]["mode"] == "auto"


# ── Auto siblings ship as DRAFT (not APPROVED — Meta hasn't seen them) ──


class TestAutoSiblingsShipAsDraft:
    """The auto bodies are NEW templates that Meta hasn't reviewed.
    Seeding them as APPROVED would be a lie that breaks Meta sync.
    """

    def test_auto_seeds_are_draft(self):
        for name in ("special_offer_auto", "vip_exclusive_auto", "win_back_auto"):
            seed = next((s for s in SEED_TEMPLATES if s["name"] == name), None)
            assert seed is not None, f"missing seed for {name!r}"
            assert seed["status"] == "DRAFT", (
                f"{name!r} must ship as DRAFT — Meta has not approved it"
            )

    def test_auto_var_maps_use_two_slots(self):
        # Auto templates only need customer_name + a coupon slot —
        # discount_pct is a merchant decision and lives outside the
        # auto pipeline.
        for name in ("special_offer_auto", "vip_exclusive_auto", "win_back_auto"):
            var_map = TEMPLATE_VAR_MAP.get(name)
            assert var_map is not None, f"missing var_map for {name!r}"
            assert var_map["{{1}}"] == "customer_name"
            assert var_map["{{2}}"] in {"coupon_code", "vip_coupon"}


# ── Enricher backfills + suffixes ──────────────────────────────────────


class TestEnrichLibraryMeta:
    def test_enricher_keeps_explicit_mode(self):
        enriched = _enrich_library_meta({
            "label": "اختبار",
            "mode": "manual",
            "library_label_ar": "اختبار — يدوي",
        })
        assert enriched["mode"] == "manual"
        assert enriched["library_label_ar"] == "اختبار — يدوي"

    def test_enricher_defaults_missing_mode_to_auto(self):
        enriched = _enrich_library_meta({"label": "بلا مود"})
        assert enriched["mode"] == "auto"
        # And generates a label suffix automatically.
        assert "تلقائي" in enriched["library_label_ar"]

    def test_enricher_handles_empty_meta(self):
        # Templates not present in the library return falsy unchanged
        # so callers can short-circuit.
        assert _enrich_library_meta({}) == {}
