"""
tests/test_catalog_source_layer.py
──────────────────────────────────
Unit tests for the source-agnostic Product Catalog layer (May 2026).

What this covers
────────────────
The Product Catalog is now a first-class asset that multiple sources
feed (Salla / Zid / manual / future Shopify-CSV) and multiple channels
consume (WhatsApp / Meta / campaigns / AI). The tests below pin the
behaviour of:

  1. ``core.catalog.product_source``           — resolves source from
     column, JSONB metadata, or the external_id heuristic.
  2. ``core.catalog.source_breakdown``         — aggregates the source
     map for the diagnostics endpoint.
  3. ``core.catalog.dominant_source``          — picks the badge that
     the dashboard renders ("salla" / "manual" / "mixed" / "unknown").
  4. Manual-product Pydantic models — title required, lengths bounded.
  5. Manual-product serialiser surfaces the JSONB image_url /
     product_url at the top level.

These tests are DB-free. They use plain SimpleNamespace stand-ins so
they run inside the existing pytest harness without spinning up a
SQLite session — same pattern used by every other catalog unit test.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Resolve backend root so `from core.catalog import ...` works regardless
# of where pytest is invoked from.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    KNOWN_SOURCES,
    SOURCE_MANUAL,
    SOURCE_SALLA,
    SOURCE_UNKNOWN,
    dominant_source,
    product_source,
    source_breakdown,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  product_source — resolution priority
# ─────────────────────────────────────────────────────────────────────────────


def _p(**kw):
    """Build a stand-in Product. ``source``, ``extra_metadata``, and
    ``external_id`` default to None so each test sets only what it
    cares about."""
    defaults = {"source": None, "extra_metadata": None, "external_id": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_product_source_reads_column_first():
    assert product_source(_p(source="manual")) == SOURCE_MANUAL
    assert product_source(_p(source="salla")) == SOURCE_SALLA


def test_product_source_normalises_case_and_whitespace():
    assert product_source(_p(source="  SALLA  ")) == SOURCE_SALLA


def test_product_source_unknown_literal_collapses_to_unknown():
    """A garbage string in the column must not leak through — the
    UI badge palette is a closed set."""
    assert product_source(_p(source="some_random_source")) == SOURCE_UNKNOWN


def test_product_source_falls_back_to_metadata_jsonb():
    """Legacy Salla sync writers stamped ``source`` inside
    ``extra_metadata`` only. ``product_source`` must still find it
    so pre-migration-0062 rows render correctly."""
    p = _p(extra_metadata={"source": "salla", "thumbnail": "x"})
    assert product_source(p) == SOURCE_SALLA


def test_product_source_metadata_dict_on_plain_dict_product():
    """Plain dict products (used by the [PRODUCT:...] resolver path)
    also surface a source via metadata."""
    p = {"source": None, "extra_metadata": {"source": "manual"}}
    assert product_source(p) == SOURCE_MANUAL


def test_product_source_heuristic_external_id_means_salla():
    """No explicit source + a populated external_id → assume Salla
    (the longest-running writer + matches the 0062 backfill rule)."""
    assert product_source(_p(external_id="sku_123")) == SOURCE_SALLA


def test_product_source_no_signal_returns_unknown():
    assert product_source(_p()) == SOURCE_UNKNOWN
    assert product_source(None) == SOURCE_UNKNOWN


def test_product_source_column_takes_priority_over_metadata():
    """If both are set, the top-level column wins — metadata is
    only consulted when the column is empty."""
    p = _p(source="manual", extra_metadata={"source": "salla"})
    assert product_source(p) == SOURCE_MANUAL


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3.  source_breakdown + dominant_source
# ─────────────────────────────────────────────────────────────────────────────


def test_source_breakdown_counts_each_source():
    products = [
        _p(source="salla"),
        _p(source="salla"),
        _p(source="manual"),
        _p(external_id="ext_1"),  # heuristic → salla
    ]
    b = source_breakdown(products)
    assert b == {SOURCE_SALLA: 3, SOURCE_MANUAL: 1}


def test_source_breakdown_empty_iterable():
    assert source_breakdown([]) == {}
    assert source_breakdown(None) == {}  # type: ignore[arg-type]


def test_dominant_source_single_source_short_circuits():
    assert dominant_source({SOURCE_SALLA: 5}) == SOURCE_SALLA
    assert dominant_source({SOURCE_MANUAL: 1}) == SOURCE_MANUAL


def test_dominant_source_strict_majority_wins():
    """If >50% of products come from one source, that's the badge.
    8 Salla vs 2 manual → "salla" (the merchant clearly has a store)."""
    assert dominant_source({SOURCE_SALLA: 8, SOURCE_MANUAL: 2}) == SOURCE_SALLA


def test_dominant_source_no_strict_majority_returns_mixed():
    """5/5 or 4/4/2 — no majority → ``"mixed"`` so the UI shows
    the merchant they have multiple data sources to reconcile."""
    assert dominant_source({SOURCE_SALLA: 5, SOURCE_MANUAL: 5}) == "mixed"
    assert dominant_source(
        {SOURCE_SALLA: 4, SOURCE_MANUAL: 4, SOURCE_UNKNOWN: 2},
    ) == "mixed"


def test_dominant_source_empty_returns_unknown():
    assert dominant_source({}) == SOURCE_UNKNOWN
    assert dominant_source({SOURCE_SALLA: 0}) == SOURCE_UNKNOWN


def test_known_sources_includes_all_documented_strings():
    """Regression: every writer in store_sync / salla sync / manual
    CRUD MUST stamp a value that's inside KNOWN_SOURCES, otherwise
    ``product_source`` will silently coerce it to ``"unknown"`` and
    the dashboard badge will lie."""
    expected = {SOURCE_SALLA, SOURCE_MANUAL, SOURCE_UNKNOWN, "zid"}
    assert expected.issubset(KNOWN_SOURCES)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Manual product Pydantic input validation
# ─────────────────────────────────────────────────────────────────────────────


def test_manual_product_in_requires_title():
    """Empty title → 422 (Pydantic ValidationError). The endpoint
    additionally trims+normalises, but the model is the first gate."""
    import pytest
    from pydantic import ValidationError

    from routers.catalog import _ManualProductIn  # noqa: PLC0415

    with pytest.raises(ValidationError):
        _ManualProductIn(title="")


def test_manual_product_in_accepts_minimal_payload():
    """The contract is "title is the ONLY required field" — every
    other column should default to None / safe values."""
    from routers.catalog import _ManualProductIn  # noqa: PLC0415

    m = _ManualProductIn(title="عسل سدر 500 جرام")
    assert m.title == "عسل سدر 500 جرام"
    assert m.description is None
    assert m.price is None
    assert m.in_stock is True  # sensible default for a hand-entered row


def test_manual_product_patch_all_optional():
    """The patch model treats every field as optional so the
    endpoint can do a true PATCH (apply only what was sent)."""
    from routers.catalog import _ManualProductPatch  # noqa: PLC0415

    empty = _ManualProductPatch()
    assert empty.model_dump(exclude_unset=True) == {}

    single = _ManualProductPatch(price="120 SAR")
    assert single.model_dump(exclude_unset=True) == {"price": "120 SAR"}


def test_manual_product_in_bounds_field_lengths():
    """Sanity bounds — title <= 512, meta_retailer_id <= 255 etc.
    Prevents accidental DB write failures behind the API."""
    import pytest
    from pydantic import ValidationError

    from routers.catalog import _ManualProductIn  # noqa: PLC0415

    with pytest.raises(ValidationError):
        _ManualProductIn(title="x" * 600)
    with pytest.raises(ValidationError):
        _ManualProductIn(title="ok", meta_retailer_id="x" * 300)


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Manual product serialiser
# ─────────────────────────────────────────────────────────────────────────────


def test_serialise_manual_product_surfaces_jsonb_urls_at_top_level():
    """The dashboard expects ``image_url`` and ``product_url`` as
    top-level fields. The serialiser pulls them out of the JSONB
    blob so the frontend never has to know that detail."""
    from routers.catalog import _serialise_manual_product  # noqa: PLC0415

    p = SimpleNamespace(
        id=42, tenant_id=7,
        title="منتج اختبار",
        description=None, price="50 ر.س", sku=None,
        external_id=None, meta_retailer_id=None,
        in_stock=True, stock_quantity=None,
        source="manual",
        extra_metadata={
            "source": "manual",
            "image_url": "https://cdn.example.com/p.jpg",
            "product_url": "https://store.example.com/p/42",
            "other": "ignored",
        },
    )

    out = _serialise_manual_product(p)
    assert out["id"] == 42
    assert out["title"] == "منتج اختبار"
    assert out["source"] == "manual"
    assert out["image_url"] == "https://cdn.example.com/p.jpg"
    assert out["product_url"] == "https://store.example.com/p/42"
    # No retailer id at all → effective_retailer_id is empty.
    assert out["effective_retailer_id"] == ""


def test_serialise_manual_product_handles_missing_jsonb():
    """``extra_metadata`` can be NULL on a freshly-inserted row before
    the writer fills it — the serialiser must not crash."""
    from routers.catalog import _serialise_manual_product  # noqa: PLC0415

    p = SimpleNamespace(
        id=1, tenant_id=1,
        title="x", description=None, price=None, sku=None,
        external_id=None, meta_retailer_id="manual_rid",
        in_stock=True, stock_quantity=None,
        source="manual", extra_metadata=None,
    )
    out = _serialise_manual_product(p)
    assert out["image_url"] == ""
    assert out["product_url"] == ""
    # When meta_retailer_id is set, that becomes the effective id.
    assert out["effective_retailer_id"] == "manual_rid"
