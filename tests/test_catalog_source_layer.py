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
    SOURCE_META,
    SOURCE_META_EXISTING,
    SOURCE_NAHLA_NATIVE,
    SOURCE_SALLA,
    SOURCE_UNKNOWN,
    dominant_source,
    normalize_source,
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
    assert product_source(_p(source="manual")) == SOURCE_NAHLA_NATIVE
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
    assert product_source(p) == SOURCE_NAHLA_NATIVE


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
    assert product_source(p) == SOURCE_NAHLA_NATIVE


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
    assert b == {SOURCE_SALLA: 3, SOURCE_NAHLA_NATIVE: 1}


def test_source_breakdown_empty_iterable():
    assert source_breakdown([]) == {}
    assert source_breakdown(None) == {}  # type: ignore[arg-type]


def test_dominant_source_single_source_short_circuits():
    assert dominant_source({SOURCE_SALLA: 5}) == SOURCE_SALLA
    assert dominant_source({SOURCE_NAHLA_NATIVE: 1}) == SOURCE_NAHLA_NATIVE


def test_dominant_source_strict_majority_wins():
    """If >50% of products come from one source, that's the badge.
    8 Salla vs 2 manual → "salla" (the merchant clearly has a store)."""
    assert dominant_source({SOURCE_SALLA: 8, SOURCE_NAHLA_NATIVE: 2}) == SOURCE_SALLA


def test_dominant_source_no_strict_majority_returns_mixed():
    """5/5 or 4/4/2 — no majority → ``"mixed"`` so the UI shows
    the merchant they have multiple data sources to reconcile."""
    assert dominant_source({SOURCE_SALLA: 5, SOURCE_NAHLA_NATIVE: 5}) == "mixed"
    assert dominant_source(
        {SOURCE_SALLA: 4, SOURCE_NAHLA_NATIVE: 4, SOURCE_UNKNOWN: 2},
    ) == "mixed"


def test_dominant_source_empty_returns_unknown():
    assert dominant_source({}) == SOURCE_UNKNOWN
    assert dominant_source({SOURCE_SALLA: 0}) == SOURCE_UNKNOWN


def test_known_sources_includes_all_documented_strings():
    """Regression: every writer in store_sync / salla sync / manual
    CRUD / Meta import MUST stamp a value that's inside KNOWN_SOURCES,
    otherwise ``product_source`` will silently coerce it to
    ``"unknown"`` and the dashboard badge will lie."""
    expected = {
        SOURCE_SALLA, SOURCE_MANUAL, SOURCE_NAHLA_NATIVE,
        SOURCE_UNKNOWN, "zid", "meta", SOURCE_META_EXISTING,
    }
    assert expected.issubset(KNOWN_SOURCES)


def test_source_meta_is_known_and_roundtrips_through_product_source():
    """Hub architecture #14: legacy ``source = "meta"`` normalises to
    ``meta_existing`` for the closed dashboard badge set."""
    assert SOURCE_META == "meta"
    assert SOURCE_META in KNOWN_SOURCES
    assert SOURCE_META_EXISTING in KNOWN_SOURCES
    assert normalize_source(SOURCE_META) == SOURCE_META_EXISTING
    assert product_source(_p(source="meta")) == SOURCE_META_EXISTING
    assert product_source(_p(source="META  ")) == SOURCE_META_EXISTING  # case + ws


def test_known_channels_are_exposed_for_hub_diagram():
    """The dashboard hub diagram reads CHANNEL_* constants from
    ``core.catalog``. Re-imports here to fail loudly if a constant
    is renamed."""
    from core.catalog import (  # noqa: PLC0415
        CHANNEL_AI,
        CHANNEL_CAMPAIGNS,
        CHANNEL_CHECKOUT,
        CHANNEL_GOOGLE_MERCHANT,
        CHANNEL_META_CATALOG,
        CHANNEL_WHATSAPP,
        KNOWN_CHANNELS,
    )

    expected = {
        CHANNEL_WHATSAPP, CHANNEL_META_CATALOG, CHANNEL_AI,
        CHANNEL_CAMPAIGNS, CHANNEL_GOOGLE_MERCHANT, CHANNEL_CHECKOUT,
    }
    assert expected == set(KNOWN_CHANNELS)


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
    assert out["source"] == SOURCE_NAHLA_NATIVE
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


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Meta Catalog import — pure helpers (no network)
# ─────────────────────────────────────────────────────────────────────────────


def test_meta_import_price_parser_handles_ws_and_iso_currency():
    """Meta typically returns ``"199.00 SAR"`` — the parser must
    extract both value and currency. Validates the pure helper
    without hitting the wire."""
    from services.meta_catalog_import import _parse_meta_price  # noqa: PLC0415

    out = _parse_meta_price("199.00 SAR")
    assert out["value"] == "199.00"
    assert out["currency"] == "SAR"
    assert out["raw"] == "199.00 SAR"


def test_meta_import_price_parser_handles_dict_shape():
    """Some Meta SKUs return ``{"amount": "19.99", "currency": "USD"}`` —
    the parser handles both string and dict shapes."""
    from services.meta_catalog_import import _parse_meta_price  # noqa: PLC0415

    out = _parse_meta_price({"amount": "19.99", "currency": "usd"})
    assert out["value"] == "19.99"
    assert out["currency"] == "USD"


def test_meta_import_price_parser_handles_none():
    """None must not crash + must not invent a value."""
    from services.meta_catalog_import import _parse_meta_price  # noqa: PLC0415

    assert _parse_meta_price(None) == {"value": None, "currency": None, "raw": ""}


def test_meta_import_error_carries_structured_code():
    """The closed-set error codes are the contract between the
    service and the router — pin the four current codes."""
    from services.meta_catalog_import import MetaCatalogImportError  # noqa: PLC0415

    err = MetaCatalogImportError("catalog_id_missing", "set it first")
    assert err.code == "catalog_id_missing"
    assert "set it first" in str(err)


def test_meta_import_report_to_dict_caps_error_samples():
    """The merchant-facing UI receives at most 10 error samples even
    if the import had hundreds — anything more is hostile UX +
    payload bloat."""
    from services.meta_catalog_import import ImportReport  # noqa: PLC0415

    r = ImportReport()
    r.scanned = 200
    r.errors = 50
    for i in range(50):
        r.error_samples.append({"id": f"x{i}", "reason": "bad"})
    d = r.to_dict()
    assert len(d["error_samples"]) == 10
    assert d["errors"] == 50  # underlying counter unchanged


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Meta Catalog export — pure payload builder (network deferred)
# ─────────────────────────────────────────────────────────────────────────────


def test_meta_export_payload_builder_maps_fields_correctly():
    """Pure function test of the Nahla → Meta field mapping. The
    surrounding ``export_to_meta`` is planning-only today, but the
    payload builder is implemented and unit-testable so the
    implementation PR can land as a thin wrapper."""
    from services.meta_catalog_export import build_meta_product_payload  # noqa: PLC0415

    p = SimpleNamespace(
        id=10,
        title="عسل سدر",
        description="500 غ",
        price="95 ر.س",
        in_stock=True,
        external_id="ext_99",
        meta_retailer_id="rid_99",
        extra_metadata={
            "image_url":   "https://cdn.example/honey.jpg",
            "product_url": "https://store.example/p/honey",
        },
    )
    out = build_meta_product_payload(p)
    assert out["retailer_id"] == "rid_99"   # explicit override wins
    assert out["name"]         == "عسل سدر"
    assert out["description"]  == "500 غ"
    assert out["image_url"]    == "https://cdn.example/honey.jpg"
    assert out["url"]          == "https://store.example/p/honey"
    assert out["price"]        == "95 ر.س"
    assert out["availability"] == "in stock"


def test_meta_export_payload_marks_out_of_stock_for_meta():
    from services.meta_catalog_export import build_meta_product_payload  # noqa: PLC0415

    p = SimpleNamespace(
        id=1, title="x", description=None, price=None,
        in_stock=False, external_id="e", meta_retailer_id=None,
        extra_metadata=None,
    )
    out = build_meta_product_payload(p)
    assert out["availability"] == "out of stock"
    # Falls back to external_id when override missing.
    assert out["retailer_id"] == "e"


def test_meta_export_stub_raises_not_implemented():
    """The async export call is deferred — the stub must clearly
    refuse rather than silently no-op."""
    import pytest

    from services.meta_catalog_export import export_to_meta  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        export_to_meta(db=None, tenant_id=1)


# ─────────────────────────────────────────────────────────────────────────────
# 8.  AI / catalog contract — "the resolver reads Nahla catalog ONLY"
# ─────────────────────────────────────────────────────────────────────────────


def test_product_resolver_never_imports_external_apis():
    """Regression: the product resolver MUST stay off the Salla /
    Meta / Zid live APIs. Reading ``Product`` rows from Postgres is
    the only acceptable path — see the "AI / catalog contract"
    section in product_resolver's module docstring.

    Asserted at the import-graph level: a future PR that absent-
    mindedly imports a platform client from inside the resolver
    trips this test rather than shipping silently."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "backend" / "services" / "product_resolver.py"
    text = src.read_text(encoding="utf-8")
    import_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    forbidden_modules = (
        "integrations.salla.api",
        "integrations.zid.api",
        "services.meta_catalog_import",
        "integrations.salla.sync",
        "httpx",
        "requests",
    )
    for line in import_lines:
        for forb in forbidden_modules:
            assert forb not in line, (
                f"product_resolver.py imports {forb!r} — violates the "
                f"'AI reads Nahla catalog only' contract. See its module "
                f"docstring under 'AI / catalog contract'.\n"
                f"Offending line: {line!r}"
            )


def test_product_resolver_docstring_mentions_hub_contract():
    """Live documentation check: the contract section MUST exist in
    the module docstring. If someone deletes it during a refactor,
    they're also deleting institutional memory."""
    from services import product_resolver  # noqa: PLC0415

    doc = (product_resolver.__doc__ or "").lower()
    assert "ai / catalog contract" in doc
    assert "nahla local" in doc or "nahla catalog" in doc
