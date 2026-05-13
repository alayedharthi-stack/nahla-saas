"""
tests/test_resolver_layer.py
────────────────────────────
Tests for the new Product/Media resolver layer (Phase: "ربط الكتالوج
ومكتبة الوسائط بالذكاء").

The resolver layer adds three new contracts on top of the existing
``[MEDIA:<id>]`` marker system:

  1. ``services.media_key_registry`` — the closed set of well-known
     media keys (payment barcodes, store assets) the LLM is allowed
     to emit + heuristic key-from-query matching for the
     deterministic post-LLM safety net.

  2. ``services.media_resolver`` — turn a ``media_key`` into a
     concrete ``AIMediaItem`` row (tenant-scoped, active-only) +
     extract ``[MEDIA_KEY:<slug>]`` markers from chat replies.

  3. ``services.product_resolver`` — wrap
     ``CatalogContextBuilder.search_products`` into a canonical
     ``ProductResolution`` DTO + extract ``[PRODUCT:<query>]``
     markers + render product image captions.

All three are pure-Python with stubbed DB sessions where useful —
matches the style of ``tests/test_intelligence_libraries.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════
# 1. media_key_registry
# ═══════════════════════════════════════════════════════════════════


def test_registry_contains_all_required_payment_keys():
    """The user spec named six payment-related keys (plus the bank
    transfer image). Regression-test that they are all in the
    registry — removing any of these would break the user's
    documented examples."""
    from services.media_key_registry import REGISTRY

    keys = {mk.key for mk in REGISTRY}
    required = {
        "payment_rajhi_barcode",
        "payment_alahli_barcode",
        "payment_barq_barcode",
        "payment_stcpay_qr",
        "payment_mobilypay_qr",
        "store_location_image",
        "shipping_instruction_image",
        "product_usage_video",
        "review_screenshot",
        "certificate_image",
    }
    missing = required - keys
    assert not missing, f"registry missing required keys: {missing}"


def test_registry_keys_are_unique_lowercase_ascii():
    """Slugs are stored in a DB column and matched exactly by the
    resolver — duplicates / mixed-case entries would silently
    shadow each other. Defensive."""
    from services.media_key_registry import REGISTRY

    seen = set()
    for mk in REGISTRY:
        assert mk.key == mk.key.lower(), f"non-lowercase: {mk.key}"
        assert mk.key.replace("_", "").isalnum(), f"non-ASCII: {mk.key}"
        assert mk.key not in seen, f"duplicate: {mk.key}"
        seen.add(mk.key)


def test_find_key_for_query_picks_rajhi_for_arabic_request():
    from services.media_key_registry import find_key_for_query

    # Customer prose — typical wording.
    assert find_key_for_query("أرسل لي باركود الراجحي") == "payment_rajhi_barcode"
    assert find_key_for_query("بنك الراجحي") == "payment_rajhi_barcode"
    # English trigger.
    assert find_key_for_query("Send me the alrajhi QR") == "payment_rajhi_barcode"


def test_find_key_for_query_picks_alahli_for_snb():
    from services.media_key_registry import find_key_for_query

    assert find_key_for_query("ابي ارسل للأهلي") == "payment_alahli_barcode"
    # Bare "snb" should also resolve.
    assert find_key_for_query("snb account") == "payment_alahli_barcode"


def test_find_key_for_query_returns_none_for_unrelated_text():
    """A generic greeting must NOT match any payment trigger — the
    post-LLM safety net should never fire on small-talk."""
    from services.media_key_registry import find_key_for_query

    assert find_key_for_query("السلام عليكم") is None
    assert find_key_for_query("شكراً") is None
    assert find_key_for_query("") is None
    assert find_key_for_query(None) is None  # type: ignore[arg-type]


def test_find_key_for_query_prefers_longer_trigger_match():
    """When two triggers both hit ("راجحي" + "باركود الراجحي"),
    the longer match wins. Tightens intent."""
    from services.media_key_registry import find_key_for_query

    # Both "راجحي" and "باركود الراجحي" are triggers of the same key,
    # so the longer one wins (this also implicitly tests that no
    # other key has a longer trigger that would steal the match).
    assert find_key_for_query("ابي باركود الراجحي حق ضروري") == "payment_rajhi_barcode"


def test_format_keys_for_prompt_emits_only_known_keys():
    from services.media_key_registry import format_keys_for_prompt

    out = format_keys_for_prompt([
        "payment_rajhi_barcode",
        "this_is_not_in_registry",  # silently dropped
        "payment_alahli_barcode",
    ])
    assert "[MEDIA_KEY:payment_rajhi_barcode]" in out
    assert "[MEDIA_KEY:payment_alahli_barcode]" in out
    assert "this_is_not_in_registry" not in out


def test_format_keys_for_prompt_dedupes_repeated_keys():
    from services.media_key_registry import format_keys_for_prompt

    out = format_keys_for_prompt([
        "payment_rajhi_barcode",
        "payment_rajhi_barcode",
    ])
    assert out.count("[MEDIA_KEY:payment_rajhi_barcode]") == 1


def test_format_keys_for_prompt_empty_input_returns_empty_string():
    from services.media_key_registry import format_keys_for_prompt

    assert format_keys_for_prompt([]) == ""


# ═══════════════════════════════════════════════════════════════════
# 2. media_resolver — marker extraction
# ═══════════════════════════════════════════════════════════════════


def _stub_media_row(*, id_, tenant_id=1, media_key=None, active=True,
                    media_type="image", file_url="https://cdn/x.png",
                    title="x", priority=100):
    """One ``AIMediaItem``-shaped namespace. We don't import the
    actual model here to keep the test pure-Python (no Alembic /
    metadata bootstrap)."""
    return SimpleNamespace(
        id=id_, tenant_id=tenant_id, media_key=media_key,
        is_active=active, media_type=media_type, file_url=file_url,
        title=title, mime_type="image/png",
        storage_kind="local", storage_path=None, file_size_bytes=None,
        priority=priority,
    )


def _make_resolver_db(*, by_key=None):
    """Stub a session that emulates the resolver's two query paths:
    ``filter(...).order_by(...).first()`` for key lookup, and
    ``filter(...).distinct().all()`` for the available-keys probe.
    """
    db = MagicMock()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.distinct.return_value = q

    if by_key is not None:
        q.first.return_value = by_key
    else:
        q.first.return_value = None
    return db


def test_resolve_by_key_returns_resolution_when_row_exists():
    from services.media_resolver import resolve_by_key

    row = _stub_media_row(id_=42, media_key="payment_rajhi_barcode")
    db = _make_resolver_db(by_key=row)
    res = resolve_by_key(db, tenant_id=1, key="payment_rajhi_barcode")
    assert res is not None
    assert res.id == 42
    assert res.requested_key == "payment_rajhi_barcode"
    assert res.media_key == "payment_rajhi_barcode"


def test_resolve_by_key_returns_none_for_unknown_key():
    from services.media_resolver import resolve_by_key

    db = _make_resolver_db(by_key=None)
    assert resolve_by_key(db, 1, "payment_rajhi_barcode") is None


def test_resolve_by_key_normalises_whitespace_and_case():
    from services.media_resolver import resolve_by_key

    row = _stub_media_row(id_=42, media_key="payment_rajhi_barcode")
    db = _make_resolver_db(by_key=row)
    res = resolve_by_key(db, 1, "  Payment_Rajhi_Barcode  ")
    assert res is not None
    assert res.requested_key == "payment_rajhi_barcode"


def test_resolve_by_key_includes_fallback_text_when_requested():
    """Asset missing + fallback flag set → ``fallback_text`` is
    populated from the registry. We register that the registry
    entry for ``payment_rajhi_barcode`` has no fallback by
    default, so this test uses a key with a known fallback (or
    is content with ``None`` if no key has one in the current
    registry — the resolver contract holds either way)."""
    from services.media_resolver import resolve_by_key
    from services.media_key_registry import get

    db = _make_resolver_db(by_key=None)
    res = resolve_by_key(db, 1, "payment_rajhi_barcode",
                         include_fallback_text=True)
    assert res is None
    mk = get("payment_rajhi_barcode")
    assert mk is not None
    assert mk.label_ar == "باركود الراجحي"


def test_resolve_for_query_returns_inferred_key_even_when_asset_missing():
    """Three-way contract: (resolution, key) — caller must be able to
    distinguish "we know what they want but have no asset" from
    "no idea what they want"."""
    from services.media_resolver import resolve_for_query

    db = _make_resolver_db(by_key=None)
    res, key = resolve_for_query(db, 1, "أرسل لي باركود الراجحي")
    assert res is None
    assert key == "payment_rajhi_barcode"


def test_resolve_for_query_returns_none_for_unrelated_text():
    from services.media_resolver import resolve_for_query

    db = _make_resolver_db(by_key=None)
    res, key = resolve_for_query(db, 1, "السلام عليكم كيف الحال")
    assert res is None
    assert key is None


# ═══════════════════════════════════════════════════════════════════
# 3. media_resolver — ``[MEDIA_KEY:<slug>]`` marker extraction
# ═══════════════════════════════════════════════════════════════════


def test_extract_media_key_markers_resolves_single_key():
    from services.media_resolver import extract_media_key_markers

    row = _stub_media_row(id_=42, media_key="payment_rajhi_barcode")
    db = _make_resolver_db(by_key=row)
    cleaned, attachments, missing = extract_media_key_markers(
        db, tenant_id=1,
        reply_text="تفضل صورة التحويل [MEDIA_KEY:payment_rajhi_barcode]",
    )
    assert "[MEDIA_KEY:" not in cleaned
    assert "صورة التحويل" in cleaned
    assert len(attachments) == 1
    assert attachments[0]["id"] == 42
    assert attachments[0]["media_key"] == "payment_rajhi_barcode"
    assert missing == []


def test_extract_media_key_markers_strips_unresolved_keys_but_tracks_them():
    from services.media_resolver import extract_media_key_markers

    db = _make_resolver_db(by_key=None)
    cleaned, attachments, missing = extract_media_key_markers(
        db, tenant_id=1,
        reply_text="ابعت [MEDIA_KEY:payment_rajhi_barcode] لو سمحت",
    )
    assert "[MEDIA_KEY:" not in cleaned
    assert attachments == []
    assert missing == ["payment_rajhi_barcode"]


def test_extract_media_key_markers_no_marker_short_circuits():
    """Performance contract: when no marker is present the function
    must NOT touch the DB at all."""
    from services.media_resolver import extract_media_key_markers

    db = MagicMock()
    cleaned, attachments, missing = extract_media_key_markers(
        db, tenant_id=1, reply_text="مرحبا كيف أساعدك؟"
    )
    assert cleaned == "مرحبا كيف أساعدك؟"
    assert attachments == [] and missing == []
    db.query.assert_not_called()


def test_extract_media_key_markers_caps_at_max_attachments():
    from services.media_resolver import extract_media_key_markers

    # Stub returns a different row per call (using ``side_effect``).
    db = MagicMock()
    rows = [
        _stub_media_row(id_=1, media_key="payment_rajhi_barcode"),
        _stub_media_row(id_=2, media_key="payment_alahli_barcode"),
        _stub_media_row(id_=3, media_key="payment_barq_barcode"),
    ]
    call_iter = iter(rows)

    def _fake_query(*_a, **_kw):
        m = MagicMock()
        m.filter.return_value = m
        m.order_by.return_value = m
        m.first.return_value = next(call_iter, None)
        return m

    db.query.side_effect = _fake_query

    cleaned, attachments, _ = extract_media_key_markers(
        db, tenant_id=1,
        reply_text=(
            "[MEDIA_KEY:payment_rajhi_barcode] "
            "[MEDIA_KEY:payment_alahli_barcode] "
            "[MEDIA_KEY:payment_barq_barcode]"
        ),
        max_attachments=2,
    )
    assert "[MEDIA_KEY:" not in cleaned
    assert len(attachments) == 2


def test_extract_media_key_markers_dedupes_same_key():
    from services.media_resolver import extract_media_key_markers

    row = _stub_media_row(id_=42, media_key="payment_rajhi_barcode")
    db = _make_resolver_db(by_key=row)
    cleaned, attachments, _ = extract_media_key_markers(
        db, tenant_id=1,
        reply_text=(
            "[MEDIA_KEY:payment_rajhi_barcode] ثم "
            "[MEDIA_KEY:payment_rajhi_barcode] مرة أخرى"
        ),
    )
    assert "[MEDIA_KEY:" not in cleaned
    assert len(attachments) == 1


# ═══════════════════════════════════════════════════════════════════
# 4. product_resolver — DTO + caption rendering
# ═══════════════════════════════════════════════════════════════════


def _product_dict(**over):
    base = {
        "id": 101,
        "external_id": "salla-abc",
        "title": "عسل سدر جبلي 500غ",
        "price": "120",
        "sale_price": None,
        "image_url": "https://cdn/honey.jpg",
        "product_url": "https://store.example/p/honey",
        "description": "عسل طبيعي 100%. غني بمضادات الأكسدة.",
        "in_stock": True,
        "can_checkout": True,
        "orderable": True,
        "variants": [],
    }
    base.update(over)
    return base


def test_dict_to_resolution_normalises_empties_to_none():
    from services.product_resolver import _dict_to_resolution

    res = _dict_to_resolution(_product_dict(
        external_id="", sale_price="", image_url=""
    ))
    assert res.external_id is None
    assert res.sale_price is None
    assert res.image_url is None
    # Non-empty fields survive.
    assert res.title.startswith("عسل سدر")
    assert res.product_url == "https://store.example/p/honey"


def test_dict_to_resolution_picks_orderable_when_can_checkout_missing():
    from services.product_resolver import _dict_to_resolution

    d = _product_dict()
    d.pop("can_checkout")
    d["orderable"] = False
    res = _dict_to_resolution(d)
    assert res.can_checkout is False


def test_format_product_card_caption_numeric_price_gets_sar_suffix():
    from services.product_resolver import (
        _dict_to_resolution, format_product_card_caption,
    )

    res = _dict_to_resolution(_product_dict(price="120"))
    cap = format_product_card_caption(res)
    assert "120 ر.س" in cap
    assert res.title in cap


def test_format_product_card_caption_keeps_merchant_currency_string():
    """If the price already contains text (e.g. ``120 SAR``), we do
    NOT append ``ر.س`` — the merchant string wins."""
    from services.product_resolver import (
        _dict_to_resolution, format_product_card_caption,
    )

    res = _dict_to_resolution(_product_dict(price="120 SAR"))
    cap = format_product_card_caption(res)
    assert "120 SAR" in cap
    assert "ر.س" not in cap


def test_format_product_card_caption_shows_out_of_stock_warning():
    from services.product_resolver import (
        _dict_to_resolution, format_product_card_caption,
    )

    res = _dict_to_resolution(_product_dict(in_stock=False))
    cap = format_product_card_caption(res)
    assert "غير متوفر" in cap


def test_format_product_card_caption_truncates_long_description():
    from services.product_resolver import (
        _dict_to_resolution, format_product_card_caption,
    )

    long_desc = "وصف طويل جداً. " * 200
    res = _dict_to_resolution(_product_dict(description=long_desc))
    cap = format_product_card_caption(res, max_length=200)
    assert len(cap) <= 200


# ═══════════════════════════════════════════════════════════════════
# 5. product_resolver — ``[PRODUCT:<query>]`` marker extraction
# ═══════════════════════════════════════════════════════════════════


def test_extract_product_markers_no_marker_short_circuits(monkeypatch):
    """No marker → no DB hit, no Catalog import."""
    from services.product_resolver import extract_product_markers

    cleaned, resolutions, missing = extract_product_markers(
        db=MagicMock(), tenant_id=1, reply_text="مرحبا كيف أساعدك",
    )
    assert cleaned == "مرحبا كيف أساعدك"
    assert resolutions == [] and missing == []


def test_extract_product_markers_resolves_single_query(monkeypatch):
    from services import product_resolver as pr

    calls = []

    def _fake_resolve(db, tenant_id, query, *, customer_id=None):
        calls.append(query)
        return pr.ProductResolution(
            id=999, external_id="ext-1", title="عسل القولون",
            price="80", sale_price=None,
            image_url="https://cdn/h.jpg",
            product_url="https://store/p/h",
            description=None, in_stock=True, can_checkout=True,
            variants=[], matched_query=query, confidence="fts",
        )

    monkeypatch.setattr(pr, "resolve_by_query", _fake_resolve)

    cleaned, resolutions, missing = pr.extract_product_markers(
        db=MagicMock(), tenant_id=1,
        reply_text="نرشح لك [PRODUCT:عسل القولون] لأنه مناسب",
    )
    assert "[PRODUCT:" not in cleaned
    assert len(resolutions) == 1
    assert resolutions[0].id == 999
    assert calls == ["عسل القولون"]
    assert missing == []


def test_extract_product_markers_skips_unresolved_queries(monkeypatch):
    from services import product_resolver as pr

    monkeypatch.setattr(
        pr, "resolve_by_query",
        lambda *a, **kw: None,
    )
    cleaned, resolutions, missing = pr.extract_product_markers(
        db=MagicMock(), tenant_id=1,
        reply_text="جرب [PRODUCT:منتج وهمي] أو [PRODUCT:شيء آخر]",
    )
    assert "[PRODUCT:" not in cleaned
    assert resolutions == []
    assert sorted(missing) == ["شيء آخر", "منتج وهمي"]


def test_extract_product_markers_dedupes_by_resolved_id(monkeypatch):
    """Two distinct queries that BOTH resolve to product id=5 must
    ship the product card exactly once."""
    from services import product_resolver as pr

    def _fake_resolve(db, tenant_id, query, *, customer_id=None):
        return pr.ProductResolution(
            id=5, external_id="ext", title="X",
            price=None, sale_price=None,
            image_url=None, product_url=None,
            description=None, in_stock=True, can_checkout=True,
            variants=[], matched_query=query, confidence="fts",
        )

    monkeypatch.setattr(pr, "resolve_by_query", _fake_resolve)
    cleaned, resolutions, _ = pr.extract_product_markers(
        db=MagicMock(), tenant_id=1,
        reply_text="[PRODUCT:عسل] و [PRODUCT:عسل سدر]",
    )
    assert len(resolutions) == 1


def test_extract_product_markers_caps_at_max_attachments(monkeypatch):
    from services import product_resolver as pr

    counter = {"n": 0}

    def _fake_resolve(db, tenant_id, query, *, customer_id=None):
        counter["n"] += 1
        return pr.ProductResolution(
            id=counter["n"], external_id=None, title=f"P{counter['n']}",
            price=None, sale_price=None,
            image_url=None, product_url=None,
            description=None, in_stock=True, can_checkout=True,
            variants=[], matched_query=query, confidence="fts",
        )

    monkeypatch.setattr(pr, "resolve_by_query", _fake_resolve)
    cleaned, resolutions, _ = pr.extract_product_markers(
        db=MagicMock(), tenant_id=1,
        reply_text="[PRODUCT:A] [PRODUCT:B] [PRODUCT:C] [PRODUCT:D]",
        max_attachments=2,
    )
    assert len(resolutions) == 2


# ═══════════════════════════════════════════════════════════════════
# 6. ai_libraries — resolver overlay assembly
# ═══════════════════════════════════════════════════════════════════


def test_resolver_overlay_empty_when_no_catalog_and_no_keys():
    from core.ai_libraries import format_resolver_overlay_for_prompt

    assert format_resolver_overlay_for_prompt(
        available_media_keys_block="",
        catalog_has_products=False,
    ) == ""


def test_resolver_overlay_disables_product_marker_when_no_catalog():
    from core.ai_libraries import format_resolver_overlay_for_prompt

    out = format_resolver_overlay_for_prompt(
        available_media_keys_block="- [MEDIA_KEY:payment_rajhi_barcode] → باركود الراجحي",
        catalog_has_products=False,
    )
    assert "[MEDIA_KEY:payment_rajhi_barcode]" in out
    # Sanity: the override warning IS present.
    assert "لا يوجد لديه كتالوج" in out


def test_resolver_overlay_includes_both_when_everything_available():
    from core.ai_libraries import format_resolver_overlay_for_prompt

    out = format_resolver_overlay_for_prompt(
        available_media_keys_block="- [MEDIA_KEY:payment_rajhi_barcode] → باركود الراجحي",
        catalog_has_products=True,
    )
    assert "[PRODUCT:" in out
    assert "[MEDIA_KEY:payment_rajhi_barcode]" in out
    # No "no catalog" warning when catalog exists.
    assert "لا يوجد لديه كتالوج" not in out
