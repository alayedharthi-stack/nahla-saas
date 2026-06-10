"""
tests/test_product_studio.py
────────────────────────────
Unit tests for the Product Studio backend layer (May 2026 #15 —
Phase 1).

Covers:

  1. ``channel_specs.ChannelRegistry`` — every documented channel is
     registered, every constraint has a label, ``strictest_max_length``
     picks the smallest enabled limit.
  2. ``channel_specs.extract_field`` — resolves top-level columns,
     JSONB sidecars, and the synthetic ``retailer_id`` consistently
     across Product / dict / SimpleNamespace shapes.
  3. ``product_readiness.compute_readiness`` — soft warn threshold,
     hard limit error, missing required, allowed-values, regex.
  4. ``product_readiness.compute_badge`` — green/amber/red level
     decision + score aggregation across enabled channels only.
  5. Router-level Pydantic — ``_ReadinessPreviewBody`` accepts a
     completely empty body (first keystroke) AND a full row.

Tests are DB-free — they use SimpleNamespace stand-ins and Pydantic
model construction directly, matching the pattern used by every
other catalog unit test in this repo.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.channel_specs import (  # noqa: E402
    AI_SPEC,
    CAMPAIGNS_SPEC,
    CHANNEL_AI,
    CHANNEL_CAMPAIGNS,
    CHANNEL_GOOGLE_MERCHANT,
    CHANNEL_META_CATALOG,
    CHANNEL_WHATSAPP,
    GOOGLE_MERCHANT_SPEC,
    META_CATALOG_SPEC,
    WHATSAPP_SPEC,
    all_specs,
    extract_field,
    get_spec,
    strictest_max_length,
)
from services.product_readiness import (  # noqa: E402
    STATE_ERROR,
    STATE_MISSING,
    STATE_OK,
    STATE_WARN,
    compute_all,
    compute_badge,
    compute_readiness,
)


def _p(**kw):
    """Standin product with the same attribute surface a Product row
    exposes. ``extra_metadata`` is the JSONB sidecar."""
    defaults = {
        "id": 1, "tenant_id": 1, "title": "", "description": "",
        "price": "", "sku": None, "external_id": None,
        "meta_retailer_id": None, "in_stock": True,
        "stock_quantity": None, "source": None,
        "extra_metadata": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Channel registry — coverage + invariants
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_lists_every_documented_channel():
    names = {s.channel for s in all_specs()}
    expected = {
        CHANNEL_WHATSAPP, CHANNEL_META_CATALOG, CHANNEL_AI,
        CHANNEL_CAMPAIGNS, CHANNEL_GOOGLE_MERCHANT,
    }
    assert expected.issubset(names), (
        f"missing channels: {expected - names}"
    )


def test_google_is_registered_but_disabled_in_phase_1():
    """Phase 1 surfaces Google readiness without enabling publish.
    Flipping ``enabled=True`` here is the only switch needed when
    Phase 4 (Google publish) lands."""
    spec = get_spec(CHANNEL_GOOGLE_MERCHANT)
    assert spec is not None
    assert spec.enabled is False


def test_every_constraint_has_arabic_label():
    """Live counters render ``fc.label_ar`` directly. An English
    field name would leak into the merchant UI; pin this so a
    careless ``label_ar=""`` slips through the test."""
    for spec in all_specs():
        for fc in spec.fields:
            assert fc.label_ar.strip(), (
                f"{spec.channel}::{fc.field} has empty label_ar"
            )


def test_strictest_max_length_ignores_disabled_channels():
    """Google caps title at 150 (strictest globally) but it's
    disabled in Phase 1 → the strictest among ENABLED channels
    should be Meta/WhatsApp's 200, not Google's 150."""
    assert strictest_max_length("title") == 200


def test_strictest_max_length_returns_none_when_unconstrained():
    """``retailer_id`` is bounded on Meta (100) but the function
    should still find the strictest among enabled channels."""
    assert strictest_max_length("retailer_id") == 100


def test_strictest_max_length_picks_smallest_when_multiple():
    """Description: WhatsApp caps at 1024 (enabled), Meta at 9999
    (enabled). Strictest should be 1024."""
    assert strictest_max_length("description") == 1024


# ─────────────────────────────────────────────────────────────────────────────
# 2.  extract_field — resolution priorities
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_field_reads_top_level_column():
    p = _p(title="عسل سدر")
    assert extract_field(p, "title") == "عسل سدر"


def test_extract_field_falls_back_to_jsonb_for_image_url():
    """Phase 1 row: image lives in ``extra_metadata.image_url``.
    After Phase 2 column-promotion the lookup should still work."""
    p = _p(extra_metadata={"image_url": "https://cdn/x.jpg"})
    assert extract_field(p, "image_url") == "https://cdn/x.jpg"


def test_extract_field_thumbnail_alias_for_legacy_salla_rows():
    """Salla sync historically wrote ``thumbnail`` instead of
    ``image_url`` — the alias keeps legacy rows usable."""
    p = _p(extra_metadata={"thumbnail": "https://cdn/y.jpg"})
    assert extract_field(p, "image_url") == "https://cdn/y.jpg"


def test_extract_field_synthesises_retailer_id_from_external_id():
    p = _p(external_id="ext_99")
    assert extract_field(p, "retailer_id") == "ext_99"


def test_extract_field_meta_retailer_id_beats_external_id():
    p = _p(external_id="ext_99", meta_retailer_id="rid_explicit")
    assert extract_field(p, "retailer_id") == "rid_explicit"


def test_extract_field_handles_dict_shape_with_metadata_alias():
    """``extra_metadata`` and ``metadata`` are both accepted on
    plain dicts — the readiness preview endpoint sends the latter
    via ``model_dump`` and the migration scripts use the former."""
    p = {"title": "x", "metadata": {"image_url": "u"}}
    assert extract_field(p, "image_url") == "u"


def test_extract_field_returns_none_on_missing():
    assert extract_field(_p(), "brand") is None
    assert extract_field(None, "title") is None


# ─────────────────────────────────────────────────────────────────────────────
# 3.  compute_readiness — state transitions
# ─────────────────────────────────────────────────────────────────────────────


def _good_product():
    """Product that satisfies every enabled channel — used as the
    baseline for "negative" tests that mutate one field at a time."""
    return _p(
        title="عسل سدر فاخر",
        description="منتج طبيعي 100%",
        price="95",
        external_id="ext_1",
        meta_retailer_id="rid_1",
        in_stock=True,
        source="manual",
        extra_metadata={
            "image_url":    "https://cdn/a.jpg",
            "product_url":  "https://store/a",
            "currency":     "SAR",
            "availability": "in stock",
        },
    )


def test_readiness_good_product_is_ready_on_meta():
    r = compute_readiness(_good_product(), META_CATALOG_SPEC)
    assert r.ready is True
    assert r.blocking_count == 0
    assert r.score_pct >= 80


def test_readiness_missing_required_blocks_meta():
    p = _good_product()
    p.extra_metadata = {**p.extra_metadata, "image_url": ""}
    r = compute_readiness(p, META_CATALOG_SPEC)
    assert r.ready is False
    assert r.blocking_count >= 1
    image_fs = next(f for f in r.fields if f.field == "image_url")
    assert image_fs.state == STATE_MISSING


def test_readiness_over_limit_is_error():
    p = _good_product()
    p.title = "x" * 250  # over Meta's 200
    r = compute_readiness(p, META_CATALOG_SPEC)
    title_fs = next(f for f in r.fields if f.field == "title")
    assert title_fs.state == STATE_ERROR
    assert r.ready is False


def test_readiness_soft_warn_threshold_triggers_warn():
    p = _good_product()
    # 86% of 200 = 172 — past the default 0.85 threshold.
    p.title = "x" * 172
    r = compute_readiness(p, META_CATALOG_SPEC)
    title_fs = next(f for f in r.fields if f.field == "title")
    assert title_fs.state == STATE_WARN
    assert r.ready is True   # warn does NOT block
    assert r.warnings_count >= 1


def test_readiness_currency_regex_validates_iso_format():
    p = _good_product()
    p.extra_metadata = {**p.extra_metadata, "currency": "abc"}   # lowercase
    r = compute_readiness(p, META_CATALOG_SPEC)
    cur = next(f for f in r.fields if f.field == "currency")
    assert cur.state == STATE_ERROR
    p.extra_metadata = {**p.extra_metadata, "currency": "SAR"}
    r = compute_readiness(p, META_CATALOG_SPEC)
    cur = next(f for f in r.fields if f.field == "currency")
    assert cur.state == STATE_OK


def test_readiness_allowed_values_rejects_unknown_enum():
    p = _good_product()
    p.extra_metadata = {**p.extra_metadata, "availability": "unicorn"}
    r = compute_readiness(p, META_CATALOG_SPEC)
    av = next(f for f in r.fields if f.field == "availability")
    assert av.state == STATE_ERROR


def test_readiness_optional_field_missing_is_ok_not_blocking():
    """Brand is optional on Meta. A blank brand should not block."""
    p = _good_product()
    r = compute_readiness(p, META_CATALOG_SPEC)
    brand = next(f for f in r.fields if f.field == "brand")
    assert brand.state == STATE_OK
    assert brand.required is False


def test_readiness_google_unready_when_brand_category_missing():
    """Google requires brand, category, condition — all missing
    in our test product. Should aggregate to ``ready=False`` with
    multiple blocking_count."""
    r = compute_readiness(_good_product(), GOOGLE_MERCHANT_SPEC)
    assert r.ready is False
    assert r.blocking_count >= 3


def test_readiness_ai_spec_is_lenient():
    """AI only needs title — a blank-everywhere-else product should
    still be AI-ready."""
    p = _p(title="عسل سدر")
    r = compute_readiness(p, AI_SPEC)
    assert r.ready is True


def test_readiness_campaigns_spec_accepts_minimal_row():
    p = _p(title="عسل سدر")
    r = compute_readiness(p, CAMPAIGNS_SPEC)
    assert r.ready is True


# ─────────────────────────────────────────────────────────────────────────────
# 4.  compute_badge — grid summary
# ─────────────────────────────────────────────────────────────────────────────


def test_badge_green_when_every_enabled_channel_ready():
    """A fully-furnished product should hit ``level=green`` AND
    ``blocking_count=0``. Google is disabled in Phase 1 so its
    missing fields must NOT pull the badge to amber/red."""
    b = compute_badge(_good_product())
    assert b.level == "green"
    assert b.blocking_count == 0
    assert b.score_pct >= 90


def test_badge_red_when_any_enabled_channel_blocking():
    p = _good_product()
    p.extra_metadata = {**p.extra_metadata, "image_url": ""}
    b = compute_badge(p)
    assert b.level == "red"
    assert b.blocking_count >= 1


def test_badge_amber_when_only_warnings():
    p = _good_product()
    p.title = "x" * 175   # past soft threshold (>= 170) but under 200
    b = compute_badge(p)
    assert b.level == "amber"
    assert b.blocking_count == 0
    assert b.warn_count >= 1


def test_badge_excludes_disabled_channels_from_total():
    """Phase 1: Google is disabled → enabled_total counts only the
    4 enabled channels."""
    b = compute_badge(_good_product())
    assert b.enabled_total == 4   # WA + Meta + AI + Campaigns


# ─────────────────────────────────────────────────────────────────────────────
# 5.  compute_all — preserves registry order
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_all_returns_one_entry_per_channel_in_registry_order():
    results = compute_all(_good_product())
    assert [r.channel for r in results] == [
        CHANNEL_WHATSAPP, CHANNEL_META_CATALOG, CHANNEL_AI,
        CHANNEL_CAMPAIGNS, CHANNEL_GOOGLE_MERCHANT,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Pydantic _ReadinessPreviewBody — first-keystroke + complete
# ─────────────────────────────────────────────────────────────────────────────


def test_readiness_preview_body_accepts_empty_draft():
    """The drawer hits preview from the moment the merchant opens
    it — the body could be entirely None. Must not 422."""
    from routers.catalog import _ReadinessPreviewBody  # noqa: PLC0415

    b = _ReadinessPreviewBody()
    assert b.title is None


def test_readiness_preview_body_accepts_full_draft():
    from routers.catalog import _ReadinessPreviewBody  # noqa: PLC0415

    b = _ReadinessPreviewBody(
        title="عسل", description="x", price="50",
        currency="SAR", image_url="https://x", product_url="https://y",
        availability="in stock", brand="Nahla", in_stock=True,
    )
    assert b.title == "عسل" and b.currency == "SAR"


def test_readiness_preview_body_bounds_image_url_length():
    """Catches accidental megabyte-URL submissions early."""
    from pydantic import ValidationError

    from routers.catalog import _ReadinessPreviewBody  # noqa: PLC0415

    with pytest.raises(ValidationError):
        _ReadinessPreviewBody(image_url="x" * 5000)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Studio filters helper — pure SQL chain, smoke-test the predicate map
# ─────────────────────────────────────────────────────────────────────────────


def test_studio_filters_default_visibility_applies_active_predicates():
    """P1-G1: empty optional filters still default to active-only listing."""
    from routers.catalog import _apply_studio_filters  # noqa: PLC0415

    class _Q:
        def __init__(self):
            self.calls = 0

        def filter(self, *_a, **_k):
            self.calls += 1
            return self

    q = _Q()
    out = _apply_studio_filters(
        q,
        q=None,
        source=None,
        has_image=None,
        has_retailer_id=None,
        in_stock=None,
    )
    assert out is q
    assert q.calls == 2  # catalog_status=active + merchant_hidden_at IS NULL


def test_studio_filters_catalog_visibility_all_skips_active_predicates():
    from routers.catalog import _apply_studio_filters  # noqa: PLC0415

    class _Q:
        def __init__(self):
            self.calls = 0

        def filter(self, *_a, **_k):
            self.calls += 1
            return self

    q = _Q()
    out = _apply_studio_filters(
        q,
        q=None,
        source=None,
        has_image=None,
        has_retailer_id=None,
        in_stock=None,
        catalog_visibility="all",
    )
    assert out is q
    assert q.calls == 0


@pytest.mark.parametrize(
    "visibility,expected_calls",
    [
        ("hidden", 1),
        ("removed", 1),
        ("archived", 1),
    ],
)
def test_studio_filters_visibility_modes_apply_single_predicate(
    visibility, expected_calls,
):
    from routers.catalog import _apply_studio_filters  # noqa: PLC0415

    class _Q:
        def __init__(self):
            self.calls = 0

        def filter(self, *_a, **_k):
            self.calls += 1
            return self

    q = _Q()
    _apply_studio_filters(
        q,
        q=None,
        source=None,
        has_image=None,
        has_retailer_id=None,
        in_stock=None,
        catalog_visibility=visibility,
    )
    assert q.calls == expected_calls
