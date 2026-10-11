"""tests/test_google_merchant_products_v1.py
─────────────────────────────────────────
Pure (no DB, no network) coverage for the offline Google Merchant API v1
mapper. Rows are plain ``SimpleNamespace`` objects shaped like the
``Product`` / ``ProductVariant`` ORM rows.

Scenarios cover the real honey store shape AND a neutral generic merchant
(``متجر تجريبي عام``: apparel with size/colour variants, a perfume single
SKU) so the mapper is proven category-agnostic. Assertions target payload
structure, identity, issues and state — never prose.
"""
from __future__ import annotations

import base64
import decimal
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.google_merchant_products_v1 import (  # noqa: E402
    OFFER_STATUS_BLOCKED,
    OFFER_STATUS_PASSES_OFFLINE_CHECKS,
    CrossTenantRowError,
    GoogleMerchantTarget,
    InvalidTargetError,
    map_product,
    mark_duplicate_identities,
    micros_or_error,
    parse_price_amount,
    product_input_encoded_id,
    product_input_plain_id,
    to_micros,
    validate_gtin,
)

TENANT = 7
OTHER_TENANT = 8
TARGET = GoogleMerchantTarget(tenant_id=TENANT, content_language="ar", feed_label="SA")


def _product(**kw):
    meta = {
        "status": "active",
        "source_status": "sale",
        "currency": "SAR",
        "image_url": "https://cdn.example-store.test/p/main.jpg",
        "product_url": "https://example-store.test/p/1001",
        "brand": "علامة تجريبية",
    }
    meta.update(kw.pop("meta", {}))
    base = dict(
        id=1, tenant_id=TENANT, source="salla", external_id="1001",
        title="منتج تجريبي", description="وصف المنتج من المتجر",
        price="100", in_stock=True, stock_quantity=None,
        catalog_status="active", merchant_hidden_at=None, archived_at=None,
        ownership_mode=None, extra_metadata=meta,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _variant(**kw):
    base = dict(
        id=10, tenant_id=TENANT, product_id=1, salla_variant_id=None, sku=None,
        retailer_id=None, price="100", currency="SAR", stock_quantity=None,
        in_stock=True, options=None, option_summary=None, image_url=None,
        is_default=False, extra_metadata=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _default_for(product, **kw):
    return _variant(
        id=kw.pop("id", 99), product_id=product.id, is_default=True,
        retailer_id=kw.pop("retailer_id", product.external_id), price=product.price, **kw,
    )


def _codes(offer):
    return {i.code for i in offer.issues}


def _severity(offer, code):
    return next(i.severity for i in offer.issues if i.code == code)


# ─────────────────────────────────────────────────────────────────────
# Target
# ─────────────────────────────────────────────────────────────────────


class TestTarget:

    @pytest.mark.parametrize("kwargs", [
        dict(tenant_id=0, content_language="ar", feed_label="SA"),
        dict(tenant_id=True, content_language="ar", feed_label="SA"),
        dict(tenant_id=1, content_language="ara", feed_label="SA"),
        dict(tenant_id=1, content_language="AR", feed_label="SA"),
        dict(tenant_id=1, content_language="ar", feed_label="Saudi Arabia"),
        dict(tenant_id=1, content_language="ar", feed_label="sa"),
        dict(tenant_id=1, content_language="ar", feed_label="A" * 21),
    ])
    def test_invalid_targets_rejected(self, kwargs):
        with pytest.raises(InvalidTargetError):
            GoogleMerchantTarget(**kwargs)

    def test_no_defaults_values_come_from_target(self):
        target = GoogleMerchantTarget(tenant_id=TENANT, content_language="en", feed_label="AE_2")
        offer = map_product(target, _product(), []).offers[0]
        assert offer.product_input["contentLanguage"] == "en"
        assert offer.product_input["feedLabel"] == "AE_2"


# ─────────────────────────────────────────────────────────────────────
# Single SKU (honey store shape) and generic perfume
# ─────────────────────────────────────────────────────────────────────


class TestSingleSku:

    def test_honey_single_sku_offer(self):
        p = _product(external_id="7001", title="عسل سدر 500 جرام", price="85")
        mapped = map_product(TARGET, p, [_default_for(p)])
        assert len(mapped.offers) == 1 and mapped.exclusions == []
        offer = mapped.offers[0]
        body = offer.product_input
        attrs = body["productAttributes"]
        assert body["offerId"] == "7001"
        assert (body["contentLanguage"], body["feedLabel"]) == ("ar", "SA")
        assert "itemGroupId" not in attrs
        assert attrs["price"] == {"amountMicros": "85000000", "currencyCode": "SAR"}
        assert attrs["availability"] == "IN_STOCK"
        assert attrs["link"] == "https://example-store.test/p/1001"
        assert offer.status == OFFER_STATUS_PASSES_OFFLINE_CHECKS
        assert offer.lifecycle == "active"
        assert offer.provenance["offerId"] == "product.external_id"
        assert offer.provenance["currencyCode"] == "product.currency"
        assert "live_source_identity_unverified" in _codes(offer)
        assert "image_dimensions_unknown" in _codes(offer)

    def test_generic_perfume_single_sku_without_variant_rows(self):
        p = _product(id=5, external_id="PRF-100", title="عطر ورد 100ml", price="249.50",
                     meta={"product_url": "https://generic-shop.test/perfume", "brand": ""})
        offer = map_product(TARGET, p, []).offers[0]
        attrs = offer.product_input["productAttributes"]
        assert offer.offer_id == "PRF-100"
        assert attrs["price"]["amountMicros"] == "249500000"
        assert "brand" not in attrs
        assert _severity(offer, "brand_missing") == "warning"

    def test_native_product_uses_local_retailer_id_with_info(self):
        p = _product(source="nahla_native", external_id=None)
        offer = map_product(TARGET, p, [_default_for(p, retailer_id="shoe-white-42")]).offers[0]
        assert offer.offer_id == "shoe-white-42"
        assert "identity_from_local_retailer_id" in _codes(offer)

    @pytest.mark.parametrize("rid", ["nahla_p_5", "nahla_v_9", ""])
    def test_synthetic_identity_excluded(self, rid):
        p = _product(source="nahla_native", external_id=None)
        mapped = map_product(TARGET, p, [_default_for(p, retailer_id=rid)])
        assert mapped.offers == []
        assert [e.reason for e in mapped.exclusions] == ["no_stable_source_identity"]

    def test_local_retailer_id_override_reported_not_used(self):
        p = _product(external_id="7001")
        offer = map_product(TARGET, p, [_default_for(p, retailer_id="meta-override-1")]).offers[0]
        assert offer.offer_id == "7001"
        assert _severity(offer, "local_retailer_id_differs") == "warning"


# ─────────────────────────────────────────────────────────────────────
# Variants (neutral apparel merchant)
# ─────────────────────────────────────────────────────────────────────


def _shirt_rows():
    p = _product(id=2, external_id="5550", title="قميص قطني أزرق", price="120",
                 meta={"product_url": "https://generic-shop.test/shirt",
                       "image_url": "https://generic-shop.test/img/shirt.jpg"})
    variants = [
        _variant(id=21, product_id=2, salla_variant_id="901", retailer_id="5550-901",
                 price="120", options={"size": "M", "color": "Blue"},
                 image_url="https://generic-shop.test/img/shirt-m.jpg"),
        _variant(id=22, product_id=2, salla_variant_id="902", retailer_id="5550-902",
                 price="120", options={"size": "L", "color": "Blue"}),
        _variant(id=23, product_id=2, salla_variant_id="903", retailer_id="5550-903",
                 price="125", options={"size": "XL", "color": "Blue"}, stock_quantity=0),
        _default_for(p, id=20, in_stock=False),
    ]
    return p, variants


class TestVariants:

    def test_genuine_variants_share_group_and_keep_own_identity(self):
        p, variants = _shirt_rows()
        mapped = map_product(TARGET, p, variants)
        assert [o.offer_id for o in mapped.offers] == ["5550-901", "5550-902", "5550-903"]
        assert {o.product_input["productAttributes"]["itemGroupId"] for o in mapped.offers} == {"5550"}
        assert [(e.local_variant_id, e.reason) for e in mapped.exclusions] == [(20, "superseded_default_variant")]
        first = mapped.offers[0].product_input["productAttributes"]
        assert (first["size"], first["color"]) == ("M", "Blue")
        assert mapped.offers[0].provenance["offerId"] == "product.external_id-variant.salla_variant_id"

    def test_variant_image_and_link_provenance(self):
        p, variants = _shirt_rows()
        offers = map_product(TARGET, p, variants).offers
        own, inherited = offers[0], offers[1]
        assert own.product_input["productAttributes"]["imageLink"].endswith("shirt-m.jpg")
        assert "variant_image_inherited" not in _codes(own)
        assert inherited.product_input["productAttributes"]["imageLink"].endswith("shirt.jpg")
        assert "variant_image_inherited" in _codes(inherited)
        for offer in offers:
            assert "variant_landing_page_not_variant_specific" in _codes(offer)

    def test_variant_specific_link_used_when_stored(self):
        p, variants = _shirt_rows()
        variants[0].extra_metadata = {"product_url": "https://generic-shop.test/shirt?v=901"}
        offer = map_product(TARGET, p, variants).offers[0]
        assert offer.product_input["productAttributes"]["link"].endswith("?v=901")
        assert "variant_landing_page_not_variant_specific" not in _codes(offer)

    def test_out_of_stock_variant_kept_with_unknown_lifecycle(self):
        p, variants = _shirt_rows()
        oos = map_product(TARGET, p, variants).offers[2]
        assert oos.product_input["productAttributes"]["availability"] == "OUT_OF_STOCK"
        assert oos.lifecycle == "unknown"
        assert "variant_lifecycle_unknown" in _codes(oos)

    def test_explicitly_retired_variant_excluded(self):
        p, variants = _shirt_rows()
        variants[1].extra_metadata = {"status": "retired"}
        mapped = map_product(TARGET, p, variants)
        assert "5550-902" not in [o.offer_id for o in mapped.offers]
        assert (22, "variant_retired_at_source") in [(e.local_variant_id, e.reason) for e in mapped.exclusions]

    def test_lone_live_variant_keeps_family_group_and_attributes(self):
        p, variants = _shirt_rows()
        for v in variants[1:3]:
            v.extra_metadata = {"deleted_at": "2026-10-01T00:00:00Z"}
        mapped = map_product(TARGET, p, variants)
        assert len(mapped.offers) == 1
        attrs = mapped.offers[0].product_input["productAttributes"]
        assert attrs["itemGroupId"] == "5550"
        assert (attrs["size"], attrs["color"]) == ("M", "Blue")

    def test_single_genuine_variant_family_keeps_attributes(self):
        p = _product(id=3, external_id="6600", title="حذاء رياضي أبيض")
        only = _variant(id=31, product_id=3, salla_variant_id="77", retailer_id="6600-77",
                        options={"size": "42", "color": "White"})
        attrs = map_product(TARGET, p, [only]).offers[0].product_input["productAttributes"]
        assert attrs["itemGroupId"] == "6600"
        assert (attrs["size"], attrs["color"]) == ("42", "White")

    def test_simple_sku_never_grouped(self):
        p = _product(external_id="7001")
        attrs = map_product(TARGET, p, [_default_for(p)]).offers[0].product_input["productAttributes"]
        assert "itemGroupId" not in attrs and "size" not in attrs

    def test_merchant_language_option_names_not_translated(self):
        p, variants = _shirt_rows()
        for v in variants:
            if not v.is_default:
                v.options = {"المقاس": "M", "اللون": "أزرق"}
        offer = map_product(TARGET, p, variants).offers[0]
        attrs = offer.product_input["productAttributes"]
        assert "size" not in attrs and "color" not in attrs
        assert "variant_attributes_unmapped" in _codes(offer)

    @pytest.mark.parametrize("kw", [
        dict(has_variants=True),
        dict(meta={"has_required_options": True}),
    ])
    def test_salla_parent_with_options_but_no_variant_rows_excluded(self, kw):
        p = _product(**kw)
        mapped = map_product(TARGET, p, [_default_for(p)])
        assert mapped.offers == []
        assert [e.reason for e in mapped.exclusions] == ["variant_rows_missing"]

    def test_salla_variant_without_platform_id_excluded(self):
        p, variants = _shirt_rows()
        variants[0].salla_variant_id = None
        mapped = map_product(TARGET, p, variants)
        assert (21, "no_stable_source_identity") in [(e.local_variant_id, e.reason) for e in mapped.exclusions]

    def test_parent_out_of_stock_overrides_variant_claim(self):
        p, variants = _shirt_rows()
        p.in_stock = False
        offer = map_product(TARGET, p, variants).offers[0]
        assert offer.product_input["productAttributes"]["availability"] == "OUT_OF_STOCK"
        assert "stock_parent_variant_conflict" in _codes(offer)

    def test_variant_currency_has_no_fallback(self):
        p, variants = _shirt_rows()
        variants[0].currency = None
        offer = map_product(TARGET, p, variants).offers[0]
        assert "currencyCode" not in offer.product_input["productAttributes"]["price"]
        assert _severity(offer, "currency_missing") == "blocking"
        assert offer.status == OFFER_STATUS_BLOCKED


# ─────────────────────────────────────────────────────────────────────
# Price
# ─────────────────────────────────────────────────────────────────────


class TestPrice:

    @pytest.mark.parametrize("value,micros", [
        ("129.99", 129990000), ("0.1", 100000), ("85", 85000000),
        (85, 85000000), (Decimal("19.999999"), 19999999), ({"amount": "49.5"}, 49500000),
    ])
    def test_exact_micros(self, value, micros):
        amount, err = parse_price_amount(value)
        assert err is None
        assert to_micros(amount) == micros

    @pytest.mark.parametrize("value,code", [
        (129.99, "price_float_rejected"), ("129.99 SAR", "price_unparseable"),
        ("1,299.00", "price_unparseable"), ("-5", "price_unparseable"),
        ("1e3", "price_unparseable"), (True, "price_unparseable"),
        ("0", "price_not_positive"), (Decimal("NaN"), "price_unparseable"),
    ])
    def test_rejected_inputs(self, value, code):
        assert parse_price_amount(value)[1] == code

    @pytest.mark.parametrize("value,expected", [
        ("1." + "0" * 5000, (1000000, None)),
        ("0.000001", (1, None)),
        ("0.0000001", (None, "price_precision")),
        ("1.00000000000000000000000000001", (None, "price_precision")),
        ("0." + "0" * 40 + "1", (None, "price_precision")),
        ("9223372036854.775807", (2**63 - 1, None)),
        ("9223372036854.775808", (None, "price_out_of_range")),
        ("9" * 400, (None, "price_out_of_range")),
    ])
    def test_exact_integer_micros_and_int64_bounds(self, value, expected):
        amount, err = parse_price_amount(value)
        assert err is None
        assert micros_or_error(amount) == expected

    @pytest.mark.parametrize("prec", [3, 28, 60])
    def test_ambient_decimal_precision_never_changes_result(self, prec):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            assert micros_or_error(Decimal("129.99"))[0] == 129990000
            assert micros_or_error(Decimal("1.00000000000000000000000000001"))[1] == "price_precision"
            offer = map_product(TARGET, _product(price="199.99", meta={
                "regular_price": "199.99", "sale_price": "199.98"}), []).offers[0]
            assert offer.product_input["productAttributes"]["price"]["amountMicros"] == "199990000"
            assert "salePrice" not in offer.product_input["productAttributes"]

    def test_extreme_decimal_inputs_do_not_hang_or_round(self):
        assert micros_or_error(Decimal("1E+999999999"))[1] == "price_out_of_range"
        assert micros_or_error(Decimal("1E-999999999"))[1] == "price_precision"
        assert parse_price_amount(10**60)[1] == "price_out_of_range"

    def test_sub_micro_precision_is_blocking_not_rounded(self):
        p = _product(price="1.0000001")
        offer = map_product(TARGET, p, []).offers[0]
        assert "price" not in offer.product_input["productAttributes"]
        assert _severity(offer, "price_precision") == "blocking"
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_amount_micros_serialized_as_decimal_string(self):
        offer = map_product(TARGET, _product(price="129.99"), []).offers[0]
        price = offer.product_input["productAttributes"]["price"]
        assert price["amountMicros"] == "129990000"
        assert isinstance(price["amountMicros"], str)
        assert json.loads(json.dumps(offer.to_dict()))["product_input"] == offer.product_input

    def test_regular_and_sale(self):
        p = _product(price="120", meta={"regular_price": "150", "sale_price": "120"})
        attrs = map_product(TARGET, p, []).offers[0].product_input["productAttributes"]
        assert attrs["price"]["amountMicros"] == "150000000"
        assert attrs["salePrice"] == {"amountMicros": "120000000", "currencyCode": "SAR"}

    @pytest.mark.parametrize("meta,code", [
        ({"regular_price": "150", "sale_price": "150"}, "sale_price_not_below_regular"),
        ({"regular_price": "150", "sale_price": "180"}, "sale_price_not_below_regular"),
        ({"sale_price": "120"}, "sale_price_ambiguous"),
        ({"regular_price": "150", "sale_price": "abc"}, "sale_price_invalid"),
    ])
    def test_unprovable_sale_not_emitted(self, meta, code):
        offer = map_product(TARGET, _product(price="120", meta=meta), []).offers[0]
        assert "salePrice" not in offer.product_input["productAttributes"]
        assert code in _codes(offer)

    def test_zero_regular_price_means_not_set(self):
        offer = map_product(TARGET, _product(price="90", meta={"regular_price": "0"}), []).offers[0]
        assert offer.product_input["productAttributes"]["price"]["amountMicros"] == "90000000"
        assert offer.provenance["price"] == "product.price"
        assert offer.status == OFFER_STATUS_PASSES_OFFLINE_CHECKS

    def test_zero_sale_means_no_sale(self):
        offer = map_product(TARGET, _product(meta={"regular_price": "100", "sale_price": "0"}), []).offers[0]
        assert "salePrice" not in offer.product_input["productAttributes"]
        assert not {c for c in _codes(offer) if c.startswith("sale_price")}

    def test_currency_never_defaulted(self):
        p = _product(meta={"currency": None})
        offer = map_product(TARGET, p, []).offers[0]
        assert offer.product_input["productAttributes"]["price"] == {"amountMicros": "100000000"}
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_lowercase_currency_is_invalid_not_rewritten(self):
        offer = map_product(TARGET, _product(meta={"currency": "sar"}), []).offers[0]
        assert "currency_invalid" in _codes(offer)


class TestEffectivePriceConflict:
    """Effective (current) price must be explained by regular or a proven sale."""

    def _offer(self, *, price="129", **meta):
        return map_product(TARGET, _product(price=price, meta=meta), []).offers[0]

    @pytest.mark.parametrize("meta,sale_code", [
        ({"regular_price": "199"}, None),
        ({"regular_price": "199", "sale_price": "abc"}, "sale_price_invalid"),
        ({"regular_price": "199", "sale_price": "250"}, "sale_price_not_below_regular"),
        ({"regular_price": "199", "sale_price": "150"}, None),
        ({"regular_price": "199", "sale_price": "0"}, None),
    ])
    def test_unexplained_effective_price_fails_closed(self, meta, sale_code):
        offer = self._offer(**meta)
        attrs = offer.product_input["productAttributes"]
        assert "price" not in attrs and "salePrice" not in attrs
        assert _severity(offer, "price_conflict_unresolved") == "blocking"
        assert offer.status == OFFER_STATUS_BLOCKED
        if sale_code:
            assert sale_code in _codes(offer)

    def test_valid_sale_matching_effective_price(self):
        offer = self._offer(regular_price="199", sale_price="129")
        attrs = offer.product_input["productAttributes"]
        assert attrs["price"] == {"amountMicros": "199000000", "currencyCode": "SAR"}
        assert attrs["salePrice"] == {"amountMicros": "129000000", "currencyCode": "SAR"}
        assert offer.status == OFFER_STATUS_PASSES_OFFLINE_CHECKS

    def test_no_sale_effective_equals_regular(self):
        offer = self._offer(price="199", regular_price="199")
        assert offer.product_input["productAttributes"]["price"]["amountMicros"] == "199000000"
        assert "price_conflict_unresolved" not in _codes(offer)
        assert offer.status == OFFER_STATUS_PASSES_OFFLINE_CHECKS

    def test_sale_not_in_effect_is_not_emitted(self):
        offer = self._offer(price="199", regular_price="199", sale_price="129")
        attrs = offer.product_input["productAttributes"]
        assert attrs["price"]["amountMicros"] == "199000000"
        assert "salePrice" not in attrs
        assert _severity(offer, "sale_price_not_effective") == "warning"

    def test_no_explicit_regular_with_different_sale_fails_closed(self):
        offer = self._offer(price="199", sale_price="129")
        assert "price" not in offer.product_input["productAttributes"]
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_regular_and_sale_without_effective_price(self):
        offer = self._offer(price=None, regular_price="199", sale_price="129")
        attrs = offer.product_input["productAttributes"]
        assert attrs["price"]["amountMicros"] == "199000000"
        assert attrs["salePrice"]["amountMicros"] == "129000000"

    def test_generic_variant_effective_regular_conflict_fails_closed(self):
        p, variants = _shirt_rows()
        variants[0].price = "129"
        variants[0].extra_metadata = {"regular_price": "199"}
        offer = map_product(TARGET, p, variants).offers[0]
        assert "price" not in offer.product_input["productAttributes"]
        assert offer.provenance.get("price") is None
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_precision_beyond_decimal_context_is_not_rounded(self):
        assert to_micros(Decimal("1.00000000000000000000000000001")) is None
        offer = self._offer(price="1.00000000000000000000000000001")
        assert _severity(offer, "price_precision") == "blocking"


class TestMalformedUrls:
    """Malformed stored URLs are item-scoped blocking issues, never crashes."""

    BAD = "https://[bad"

    def test_malformed_product_link(self):
        offer = map_product(TARGET, _product(meta={"product_url": self.BAD}), []).offers[0]
        assert "link" not in offer.product_input["productAttributes"]
        assert _severity(offer, "link_invalid") == "blocking"
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_malformed_variant_link_does_not_fall_back(self):
        p, variants = _shirt_rows()
        variants[0].extra_metadata = {"product_url": self.BAD}
        offer = map_product(TARGET, p, variants).offers[0]
        assert "link" not in offer.product_input["productAttributes"]
        assert _severity(offer, "link_invalid") == "blocking"

    def test_malformed_main_image(self):
        offer = map_product(TARGET, _product(meta={"image_url": self.BAD}), []).offers[0]
        assert "imageLink" not in offer.product_input["productAttributes"]
        assert _severity(offer, "image_link_invalid") == "blocking"

    def test_malformed_variant_image(self):
        p, variants = _shirt_rows()
        variants[0].image_url = self.BAD
        offer = map_product(TARGET, p, variants).offers[0]
        assert "imageLink" not in offer.product_input["productAttributes"]
        assert _severity(offer, "image_link_invalid") == "blocking"

    @pytest.mark.parametrize("url", [
        "https://shop.test/p\x01", "https://shop.test/p\x7f", "https://shop.test:99999/p",
        "https://shop.test:abc/p", "https://shop.test/a b", "https://shop.test/a\tb",
        "https:///p", "javascript:alert(1)", "https://[bad", "",
    ])
    def test_invalid_link_forms_are_item_scoped_blocking(self, url):
        offer = map_product(TARGET, _product(meta={"product_url": url}), []).offers[0]
        assert "link" not in offer.product_input["productAttributes"]
        assert {"link_invalid", "link_missing"} & _codes(offer)
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_non_ascii_path_left_to_google(self):
        offer = map_product(TARGET, _product(meta={"product_url": "https://shop.test/عسل-سدر"}), []).offers[0]
        assert offer.product_input["productAttributes"]["link"] == "https://shop.test/عسل-سدر"
        assert "link_invalid" not in _codes(offer)

    def test_malformed_additional_image(self):
        ok = "https://cdn.example-store.test/p/extra.jpg"
        offer = map_product(TARGET, _product(meta={"additional_images": [self.BAD, ok]}), []).offers[0]
        assert offer.product_input["productAttributes"]["additionalImageLinks"] == [ok]
        assert _severity(offer, "additional_image_link_invalid") == "blocking"
        assert offer.status == OFFER_STATUS_BLOCKED


# ─────────────────────────────────────────────────────────────────────
# Lifecycle / eligibility
# ─────────────────────────────────────────────────────────────────────


class TestLifecycle:

    @pytest.mark.parametrize("kw,reason", [
        (dict(meta={"source_status": "hidden", "status": "hidden"}), "hidden_at_source"),
        (dict(meta={"source_status": "deleted", "status": "deleted"}), "hidden_at_source"),
        (dict(source="nahla_native", meta={"status": "deleted"}), "source_lifecycle_not_active"),
        (dict(catalog_status="archived"), "catalog_archived"),
        (dict(archived_at="2026-10-01T00:00:00Z"), "catalog_archived"),
        (dict(catalog_status="merchant_hidden"), "merchant_hidden"),
        (dict(merchant_hidden_at="2026-10-01T00:00:00Z"), "merchant_hidden"),
        (dict(source="meta_existing"), "meta_owned_source"),
        (dict(ownership_mode="archived_or_disconnected"), "source_disconnected"),
    ])
    def test_excluded_products(self, kw, reason):
        mapped = map_product(TARGET, _product(**kw), [])
        assert mapped.offers == []
        assert [e.reason for e in mapped.exclusions] == [reason]

    def test_removed_from_meta_is_not_a_google_exclusion(self):
        offer = map_product(TARGET, _product(catalog_status="removed_from_meta"), []).offers[0]
        assert _severity(offer, "meta_channel_status_ignored") == "info"

    def test_out_of_stock_single_sku_kept(self):
        offer = map_product(TARGET, _product(in_stock=False, meta={"source_status": "out"}), []).offers[0]
        assert offer.product_input["productAttributes"]["availability"] == "OUT_OF_STOCK"
        assert offer.lifecycle == "active"


# ─────────────────────────────────────────────────────────────────────
# Identifiers / honest gaps
# ─────────────────────────────────────────────────────────────────────


class TestIdentifiers:

    def test_missing_identifiers_and_description_are_reported_not_invented(self):
        p = _product(description="", meta={"brand": None})
        offer = map_product(TARGET, p, []).offers[0]
        attrs = offer.product_input["productAttributes"]
        for key in ("brand", "gtins", "mpn", "identifierExists", "condition", "description"):
            assert key not in attrs
        assert {"brand_missing", "identifiers_not_available_in_source", "description_missing"} <= _codes(offer)
        assert offer.status == OFFER_STATUS_BLOCKED

    def test_valid_stored_gtin_and_mpn_preserved(self):
        p = _product(meta={"gtin": "4006381333931", "mpn": "AB-123"})
        offer = map_product(TARGET, p, []).offers[0]
        attrs = offer.product_input["productAttributes"]
        assert attrs["gtins"] == ["4006381333931"]
        assert attrs["mpn"] == "AB-123"
        assert "identifiers_not_available_in_source" not in _codes(offer)

    def test_invalid_or_restricted_gtin_dropped(self):
        p = _product(meta={"gtins": ["4006381333932", "2000000000008"]})
        offer = map_product(TARGET, p, []).offers[0]
        assert "gtins" not in offer.product_input["productAttributes"]
        assert {"gtin_invalid", "gtin_restricted_range"} <= _codes(offer)

    def test_barcode_used_only_when_it_validates_as_gtin(self):
        offer = map_product(TARGET, _product(meta={"barcode": "036000291452"}), []).offers[0]
        assert offer.product_input["productAttributes"]["gtins"] == ["036000291452"]
        assert "gtin_from_barcode_field" in _codes(offer)
        assert validate_gtin("INTERNAL-77")[0] is None

    def test_parent_gtin_not_copied_to_variants(self):
        p, variants = _shirt_rows()
        p.extra_metadata["gtin"] = "4006381333931"
        variants[1].extra_metadata = {"gtin": "036000291452"}
        offers = map_product(TARGET, p, variants).offers
        assert "gtins" not in offers[0].product_input["productAttributes"]
        assert offers[1].product_input["productAttributes"]["gtins"] == ["036000291452"]

    def test_brand_object_name_used(self):
        attrs = map_product(TARGET, _product(meta={"brand": {"id": 3, "name": "Brand X"}}), []).offers[0] \
            .product_input["productAttributes"]
        assert attrs["brand"] == "Brand X"


# ─────────────────────────────────────────────────────────────────────
# Resource ids / identity limits
# ─────────────────────────────────────────────────────────────────────


class TestResourceIds:

    def test_documented_encoded_example(self):
        assert product_input_encoded_id("en", "US", "sku/123") == "ZW5-VVN-c2t1LzEyMw"
        assert product_input_plain_id("en", "US", "sku/123") is None
        assert product_input_plain_id("ar", "SA", "5550-901") == "ar~SA~5550-901"

    @pytest.mark.parametrize("offer_id", [
        "a~b", "a%2F", "a/b", "sku?123", "sku#1", "a&b", "a+b", "a:b", "a b", "عسل-1",
    ])
    def test_reserved_characters_only_encoded_and_offer_id_untouched(self, offer_id):
        p = _product(external_id=offer_id)
        out = map_product(TARGET, p, []).offers[0].to_dict()
        assert out["product_input_id"] is None
        assert out["offer_id"] == offer_id
        assert out["product_input"]["offerId"] == offer_id
        encoded = out["product_input_id_encoded"]
        assert "=" not in encoded
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        assert decoded == f"ar~SA~{offer_id}"

    @pytest.mark.parametrize("offer_id", ["5550-901", "SKU_1.2", "abc-DEF_9"])
    def test_unreserved_offer_ids_have_plain_name(self, offer_id):
        out = map_product(TARGET, _product(external_id=offer_id), []).offers[0].to_dict()
        assert out["product_input_id"] == f"ar~SA~{offer_id}"

    def test_offer_id_limits_block(self):
        long_offer = map_product(TARGET, _product(external_id="9" * 51), []).offers[0]
        assert _severity(long_offer, "offer_id_too_long") == "blocking"
        spaced = map_product(TARGET, _product(external_id="12  34"), []).offers[0]
        assert spaced.status == OFFER_STATUS_BLOCKED


# ─────────────────────────────────────────────────────────────────────
# Duplicate final identities
# ─────────────────────────────────────────────────────────────────────


class TestDuplicateIdentity:

    def test_same_product_variant_collision_blocks_both(self):
        p = _product(id=4, source="nahla_native", external_id="TSHIRT")
        rows = [
            _variant(id=41, product_id=4, retailer_id="TS-1", options={"size": "M"}),
            _variant(id=42, product_id=4, retailer_id="TS-1", options={"size": "L"}),
            _variant(id=43, product_id=4, retailer_id="TS-2", options={"size": "XL"}),
        ]
        offers = map_product(TARGET, p, rows).offers
        status = {o.local_variant_id: o.status for o in offers}
        assert status == {41: OFFER_STATUS_BLOCKED, 42: OFFER_STATUS_BLOCKED,
                          43: OFFER_STATUS_PASSES_OFFLINE_CHECKS}
        assert "duplicate_offer_identity" in _codes(offers[0])

    def test_cross_product_and_case_only_collisions(self):
        a = map_product(TARGET, _product(id=1, external_id="DUP"), []).offers
        b = map_product(TARGET, _product(id=2, external_id="DUP"), []).offers
        c = map_product(TARGET, _product(id=3, external_id="dup"), []).offers
        d = map_product(TARGET, _product(id=4, external_id="UNIQUE"), []).offers
        everything = a + b + c + d
        assert mark_duplicate_identities(everything) == 3
        assert mark_duplicate_identities(everything) == 3  # idempotent
        assert [o.status for o in a + b + c] == [OFFER_STATUS_BLOCKED] * 3
        assert sum(i.code == "duplicate_offer_identity" for i in a[0].issues) == 1
        assert d[0].status == OFFER_STATUS_PASSES_OFFLINE_CHECKS


# ─────────────────────────────────────────────────────────────────────
# Tenant isolation
# ─────────────────────────────────────────────────────────────────────


class TestTenantIsolation:

    def test_cross_tenant_product_rejected(self):
        with pytest.raises(CrossTenantRowError):
            map_product(TARGET, _product(tenant_id=OTHER_TENANT), [])

    def test_cross_tenant_variant_rejected(self):
        p, variants = _shirt_rows()
        variants[0].tenant_id = OTHER_TENANT
        with pytest.raises(CrossTenantRowError):
            map_product(TARGET, p, variants)

    def test_variant_of_other_product_rejected(self):
        p, variants = _shirt_rows()
        variants[0].product_id = 999
        with pytest.raises(CrossTenantRowError):
            map_product(TARGET, p, variants)


def test_blocked_offer_is_never_reported_as_passing():
    offer = map_product(TARGET, _product(meta={"image_url": None}), []).offers[0]
    assert offer.blocking
    assert offer.status == OFFER_STATUS_BLOCKED
    assert offer.to_dict()["status"] == OFFER_STATUS_BLOCKED
