"""tests/test_google_merchant_protocol_v1.py
──────────────────────────────────────────
Pure protocol coverage for the Merchant API v1 request / read-back /
retry layer. No transport, no DB, no network: account and data source
ids are fake test values. Assertions target request shape, structured
status and classification — never prose.
"""
from __future__ import annotations

import ast
import sys
from email.utils import formatdate
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.google_merchant_products_v1 import GoogleMerchantTarget, product_input_encoded_id  # noqa: E402
from services.google_merchant_protocol_v1 import (  # noqa: E402
    PUBLICATION_APPROVED,
    PUBLICATION_DISAPPROVED,
    PUBLICATION_PENDING,
    PUBLICATION_UNKNOWN,
    READBACK_FOREIGN_SOURCE,
    READBACK_IDENTITY_MATCHES,
    READBACK_IDENTITY_MISMATCH,
    RESP_ACCEPTED,
    RESP_NOT_FOUND,
    RESP_PERMANENT_AUTH,
    RESP_PERMANENT_PERMISSION,
    RESP_PERMANENT_VALIDATION,
    RESP_RATE_LIMITED,
    RESP_TRANSIENT_READ,
    RESP_UNCERTAIN_WRITE,
    InvalidBindingError,
    MerchantApiBinding,
    ProtocolShapeError,
    RetryPolicy,
    TransportResponse,
    attributes_match,
    classify_response,
    delete_request,
    get_product_request,
    insert_request,
    list_products_request,
    next_retry_at,
    parse_product_readback,
    parse_retry_after,
    patch_request,
    payload_fingerprint,
    readback_identity_state,
    validate_binding,
)

NOW = 1_800_000_000
ACCOUNT = "1111111111"
SOURCE = "2222222222"
TARGET = GoogleMerchantTarget(tenant_id=7, content_language="ar", feed_label="SA")


def _binding(**kw):
    base = dict(
        tenant_id=7, account_id=ACCOUNT, data_source_id=SOURCE, data_source_input="API",
        content_language="ar", feed_label="SA", observed_at=NOW - 60,
    )
    base.update(kw)
    return MerchantApiBinding(**base)


ATTRS = {
    "title": "عطر ورد 100ml",
    "price": {"amountMicros": "249500000", "currencyCode": "SAR"},
    "availability": "IN_STOCK",
    "link": "https://generic-shop.test/perfume",
}


def _input(offer_id="PRF-100", attrs=None, **kw):
    body = {"offerId": offer_id, "contentLanguage": "ar", "feedLabel": "SA",
            "productAttributes": dict(ATTRS if attrs is None else attrs)}
    body.update(kw)
    return body


# ─────────────────────────────────────────────────────────────────────
# Binding
# ─────────────────────────────────────────────────────────────────────


class TestBinding:

    def test_valid_api_binding_has_no_reasons(self):
        assert validate_binding(_binding(), TARGET, now=NOW, max_age_s=3600) == ()

    def test_missing_binding_is_reported(self):
        assert validate_binding(None, TARGET, now=NOW, max_age_s=3600) == ("binding_missing",)

    @pytest.mark.parametrize("kind", ["FILE", "UI", "AUTOFEED", "URL", "SCHEDULED_FETCH", "", "api"])
    def test_non_api_source_is_not_usable(self, kind):
        assert "data_source_not_api" in validate_binding(_binding(data_source_input=kind), TARGET, now=NOW, max_age_s=3600)

    def test_tenant_language_and_label_mismatch(self):
        reasons = validate_binding(
            _binding(tenant_id=8, content_language="en", feed_label="AE"), TARGET, now=NOW, max_age_s=3600,
        )
        assert set(reasons) == {"binding_tenant_mismatch", "binding_language_or_label_mismatch"}

    def test_stale_and_future_evidence(self):
        assert validate_binding(_binding(observed_at=NOW - 7200), TARGET, now=NOW, max_age_s=3600) == (
            "binding_evidence_stale",)
        assert validate_binding(_binding(observed_at=NOW + 1), TARGET, now=NOW, max_age_s=3600) == (
            "binding_evidence_future",)

    @pytest.mark.parametrize("field,value", [
        ("account_id", "1111111111\n"), ("data_source_id", "2222222222\n"), ("account_id", " 1111111111"),
        ("content_language", "ar\n"), ("feed_label", "SA\n"), ("feed_label", "SA\r\n"),
        ("account_id", "acc-1"), ("account_id", ""), ("account_id", 1111111111),
        ("data_source_id", "0123"), ("tenant_id", 0), ("tenant_id", True),
        ("content_language", "AR"), ("feed_label", "sa"), ("observed_at", -1),
    ])
    def test_malformed_binding_values_raise(self, field, value):
        with pytest.raises(InvalidBindingError):
            _binding(**{field: value})


# ─────────────────────────────────────────────────────────────────────
# Request shapes
# ─────────────────────────────────────────────────────────────────────


class TestRequests:

    def test_insert_shape(self):
        req = insert_request(_binding(), _input(), offer_id="PRF-100")
        assert (req.method, req.path) == ("POST", f"/products/v1/accounts/{ACCOUNT}/productInputs:insert")
        assert req.query == (("dataSource", f"accounts/{ACCOUNT}/dataSources/{SOURCE}"),)
        assert req.body == _input()
        assert req.body["productAttributes"]["price"]["amountMicros"] == "249500000"
        assert req.url.startswith("https://merchantapi.googleapis.com/products/v1/accounts/")
        assert req.is_write

    def test_insert_body_is_a_copy(self):
        body = _input()
        req = insert_request(_binding(), body, offer_id="PRF-100")
        body["productAttributes"]["title"] = "changed"
        assert req.body["productAttributes"]["title"] == ATTRS["title"]

    @pytest.mark.parametrize("override", [{"contentLanguage": "en"}, {"feedLabel": "AE"}, {"offerId": ""}])
    def test_insert_refuses_target_mismatch_or_missing_identity(self, override):
        with pytest.raises(ProtocolShapeError):
            insert_request(_binding(), _input(**override), offer_id="PRF-100")

    @pytest.mark.parametrize("field,value", [
        ("legacyLocal", True), ("legacyLocal", False), ("customAttributes", [{"name": "x", "value": "y"}]),
        ("versionNumber", "7"), ("name", "accounts/1/productInputs/x"), ("product", "accounts/1/products/x"),
    ])
    def test_insert_refuses_any_extra_top_level_field(self, field, value):
        with pytest.raises(ProtocolShapeError, match="product_input_field_not_allowed"):
            insert_request(_binding(), _input(**{field: value}), offer_id="PRF-100")

    def test_insert_body_has_exactly_the_allowed_fields(self):
        req = insert_request(_binding(), _input(), offer_id="PRF-100")
        assert set(req.body) == {"offerId", "contentLanguage", "feedLabel", "productAttributes"}

    @pytest.mark.parametrize("payload_id,decided", [("FOREIGN-B", "PRF-100"), ("prf-100", "PRF-100"), ("PRF-100", "")])
    def test_insert_payload_identity_must_equal_decided_offer(self, payload_id, decided):
        with pytest.raises(ProtocolShapeError):
            insert_request(_binding(), _input(offer_id=payload_id), offer_id=decided)

    def test_insert_refuses_attribute_outside_supported_schema(self):
        with pytest.raises(ProtocolShapeError, match="attribute_not_in_supported_schema"):
            insert_request(_binding(), _input(attrs={**ATTRS, "customLabel0": "x"}), offer_id="PRF-100")

    def test_patch_masks_changed_and_removed_only(self):
        previous = {**ATTRS, "brand": "علامة", "salePrice": {"amountMicros": "1000000", "currencyCode": "SAR"}}
        desired = {**ATTRS, "title": "عطر ورد 50ml"}
        req = patch_request(_binding(), "PRF-100", desired, previous)
        rid = product_input_encoded_id("ar", "SA", "PRF-100")
        assert (req.method, req.path) == ("PATCH", f"/products/v1/accounts/{ACCOUNT}/productInputs/{rid}")
        query = dict(req.query)
        assert query["dataSource"] == f"accounts/{ACCOUNT}/dataSources/{SOURCE}"
        assert query["updateMask"] == "productAttributes.brand,productAttributes.salePrice,productAttributes.title"
        assert "*" not in query["updateMask"]
        # Removed fields are masked but absent from the body (= deletion).
        assert req.body == {"productAttributes": {"title": "عطر ورد 50ml"}}

    def test_patch_without_changes_or_with_unknown_field_refused(self):
        with pytest.raises(ProtocolShapeError, match="patch_without_changes"):
            patch_request(_binding(), "PRF-100", ATTRS, dict(ATTRS))
        with pytest.raises(ProtocolShapeError, match="attribute_not_in_supported_schema"):
            patch_request(_binding(), "PRF-100", {**ATTRS, "pause": "ads"}, ATTRS)

    def test_delete_has_empty_body_and_data_source(self):
        req = delete_request(_binding(), "5550-901")
        rid = product_input_encoded_id("ar", "SA", "5550-901")
        assert (req.method, req.path, req.body) == ("DELETE", f"/products/v1/accounts/{ACCOUNT}/productInputs/{rid}", None)
        assert req.query == (("dataSource", f"accounts/{ACCOUNT}/dataSources/{SOURCE}"),)

    def test_get_and_list_shapes(self):
        rid = product_input_encoded_id("ar", "SA", "7001")
        get = get_product_request(_binding(), "7001")
        assert (get.method, get.path, get.query, get.body, get.is_write) == (
            "GET", f"/products/v1/accounts/{ACCOUNT}/products/{rid}", (), None, False)
        page = list_products_request(_binding(), 250, "tok-2")
        assert (page.method, page.path) == ("GET", f"/products/v1/accounts/{ACCOUNT}/products")
        assert page.query == (("pageSize", "250"), ("pageToken", "tok-2"))
        for bad in (0, 1001, True):
            with pytest.raises(ProtocolShapeError):
                list_products_request(_binding(), bad)

    def test_encoded_id_is_unpadded_base64url(self):
        req = get_product_request(_binding(), "SKU/with space~x")
        segment = req.path.rsplit("/", 1)[1]
        assert "=" not in segment and "/" not in segment and "~" not in segment

    def test_fingerprint_is_canonical(self):
        a = {"title": "قميص قطني أزرق", "price": {"currencyCode": "SAR", "amountMicros": "120000000"}}
        b = {"price": {"amountMicros": "120000000", "currencyCode": "SAR"}, "title": "قميص قطني أزرق"}
        assert payload_fingerprint(a) == payload_fingerprint(b)
        assert payload_fingerprint(a) != payload_fingerprint({**a, "title": "x"})


# ─────────────────────────────────────────────────────────────────────
# Read-back parsing
# ─────────────────────────────────────────────────────────────────────


def _product(offer_id="PRF-100", *, status=None, data_source=None, attrs=None, **extra):
    raw = {
        "name": f"accounts/{ACCOUNT}/products/ar~SA~{offer_id}",
        "base64EncodedName": f"accounts/{ACCOUNT}/products/{product_input_encoded_id('ar', 'SA', offer_id)}",
        "offerId": offer_id, "contentLanguage": "ar", "feedLabel": "SA",
        "dataSource": data_source or f"accounts/{ACCOUNT}/dataSources/{SOURCE}",
        "productAttributes": dict(ATTRS if attrs is None else attrs),
    }
    if status is not None:
        raw["productStatus"] = status
    raw.update(extra)
    return raw


def _dest(context="SHOPPING_ADS", approved=("SA",), pending=(), disapproved=()):
    return {"reportingContext": context, "approvedCountries": list(approved),
            "pendingCountries": list(pending), "disapprovedCountries": list(disapproved)}


class TestReadback:

    def test_approved_requires_all_destinations_approved(self):
        rb = parse_product_readback(_product(status={"destinationStatuses": [_dest(), _dest("FREE_LISTINGS")]}))
        assert rb.publication_state == PUBLICATION_APPROVED
        assert rb.flags == ()

    def test_pending_and_disapproved(self):
        pending = parse_product_readback(_product(status={"destinationStatuses": [_dest(approved=(), pending=("SA",))]}))
        assert pending.publication_state == PUBLICATION_PENDING
        dis = parse_product_readback(_product(status={
            "destinationStatuses": [_dest()],
            "itemLevelIssues": [{"code": "image_too_small", "severity": "DISAPPROVED", "attribute": "image_link",
                                 "reportingContext": "SHOPPING_ADS", "resolution": "merchant_action",
                                 "applicableCountries": ["SA"]}],
        }))
        assert dis.publication_state == PUBLICATION_DISAPPROVED
        issue = dis.issues[0]
        assert (issue.code, issue.severity, issue.attribute, issue.applicable_countries) == (
            "image_too_small", "DISAPPROVED", "image_link", ("SA",))

    def test_demoted_issue_does_not_block_approval(self):
        rb = parse_product_readback(_product(status={
            "destinationStatuses": [_dest()],
            "itemLevelIssues": [{"code": "missing_gtin", "severity": "DEMOTED", "reportingContext": "SHOPPING_ADS"}],
        }))
        assert rb.publication_state == PUBLICATION_APPROVED

    @pytest.mark.parametrize("raw,flag", [
        (_product(), "status_field_missing"),
        (_product(productStatuses=[{"destination": "Shopping", "status": "approved"}]), "obsolete_status_field"),
        (_product(status={"destinationStatuses": [_dest("FUTURE_SURFACE")]}), "unknown_reporting_context"),
        (_product(status={"destinationStatuses": [_dest()],
                          "itemLevelIssues": [{"code": "x", "severity": "SEVERITY_UNSPECIFIED"}]}),
         "unknown_issue_severity"),
        (_product(status={"destinationStatuses": [_dest()], "itemLevelIssues": [{"code": "x", "severity": "NEW"}]}),
         "unknown_issue_severity"),
    ])
    def test_unknown_or_obsolete_status_is_never_approved(self, raw, flag):
        rb = parse_product_readback(raw)
        assert flag in rb.flags
        assert rb.publication_state == PUBLICATION_UNKNOWN

    def test_empty_200_body_is_unknown(self):
        rb = parse_product_readback({})
        assert rb.publication_state == PUBLICATION_UNKNOWN
        assert rb.product_attributes is None

    def test_no_destinations_is_unknown(self):
        assert parse_product_readback(_product(status={})).publication_state == PUBLICATION_UNKNOWN

    def test_identity_and_source_checks(self):
        b = _binding()
        assert readback_identity_state(parse_product_readback(_product()), b, "PRF-100") == READBACK_IDENTITY_MATCHES
        no_encoded = {k: v for k, v in _product().items() if k != "base64EncodedName"}
        assert readback_identity_state(parse_product_readback(no_encoded), b, "PRF-100") == READBACK_IDENTITY_MATCHES
        foreign = _product(data_source=f"accounts/{ACCOUNT}/dataSources/3333333333")
        assert readback_identity_state(parse_product_readback(foreign), b, "PRF-100") == READBACK_FOREIGN_SOURCE
        other_offer = _product("PRF-200")
        assert readback_identity_state(parse_product_readback(other_offer), b, "PRF-100") == READBACK_IDENTITY_MISMATCH

    def test_official_raw_name_with_reserved_characters_and_encoded_name(self):
        # Official form: raw ``name`` keeps "/" and "~"-free offer ids verbatim;
        # ``base64EncodedName`` carries the unpadded base64url last segment.
        b = _binding()
        rb = parse_product_readback(_product("sku/123"))
        assert rb.name == f"accounts/{ACCOUNT}/products/ar~SA~sku/123"
        assert readback_identity_state(rb, b, "sku/123") == READBACK_IDENTITY_MATCHES
        assert readback_identity_state(rb, b, "sku") == READBACK_IDENTITY_MISMATCH

    @pytest.mark.parametrize("override", [
        {"name": f"accounts/4444444444/products/ar~SA~PRF-100"},
        {"name": f"accounts/{ACCOUNT}/products/ar~SA~prf-100"},
        {"name": f"accounts/{ACCOUNT}/products/{product_input_encoded_id('ar', 'SA', 'PRF-100')}"},
        {"base64EncodedName": f"accounts/{ACCOUNT}/products/{product_input_encoded_id('ar', 'SA', 'PRF-200')}"},
        {"base64EncodedName": f"accounts/4444444444/products/{product_input_encoded_id('ar', 'SA', 'PRF-100')}"},
        {"base64EncodedName": ""},
        {"offerId": "prf-100"},
        {"feedLabel": "AE"},
    ])
    def test_inconsistent_or_case_variant_identity_is_mismatch(self, override):
        rb = parse_product_readback(_product(**override))
        assert readback_identity_state(rb, _binding(), "PRF-100") == READBACK_IDENTITY_MISMATCH

    @pytest.mark.parametrize("raw", [
        _product(status=1),
        _product(status={"destinationStatuses": 1}),
        _product(status={"destinationStatuses": "SHOPPING_ADS"}),
        _product(status={"destinationStatuses": {"reportingContext": "SHOPPING_ADS"}}),
        _product(status={"destinationStatuses": [1]}),
        _product(status={"destinationStatuses": [{"approvedCountries": ["SA"]}]}),
        _product(status={"destinationStatuses": [{"reportingContext": 7, "approvedCountries": ["SA"]}]}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": 1}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": "x"}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": [1]}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": [{"severity": "DEMOTED"}]}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": [{"code": 5, "severity": "DEMOTED"}]}),
        _product(status={"destinationStatuses": [_dest()], "itemLevelIssues": [{"code": "x", "severity": 2}]}),
        _product(status={"destinationStatuses": [_dest()],
                         "itemLevelIssues": [{"code": "x", "severity": "DEMOTED", "attribute": 3}]}),
        _product(status={"destinationStatuses": [_dest()],
                         "itemLevelIssues": [{"code": "x", "severity": "DEMOTED", "applicableCountries": "SA"}]}),
        _product(status={"destinationStatuses": [{**_dest(), "disapprovedCountries": "SA"}]}),
        _product(status={"destinationStatuses": [{**_dest(), "pendingCountries": "SA"}]}),
        _product(status={"destinationStatuses": [{**_dest(), "pendingCountries": 1}]}),
        _product(status={"destinationStatuses": [_dest(approved=("",))]}),
        _product(status={"destinationStatuses": [_dest(approved=(1,))]}),
        _product(status={"destinationStatuses": [_dest(approved=("sa",))]}),
        _product(status={"destinationStatuses": [{**_dest(), "approvedCountries": "SA"}]}),
        _product(status={"destinationStatuses": [_dest()]}, productAttributes=1),
        _product(status={"destinationStatuses": [_dest()]}, name=1),
        _product(status={"destinationStatuses": [_dest()]}, base64EncodedName=1),
        _product(status={"destinationStatuses": [_dest()]}, dataSource=["x"]),
    ])
    def test_malformed_containers_and_members_fail_closed(self, raw):
        rb = parse_product_readback(raw)  # never raises
        assert rb.malformed
        assert any(f.endswith("_malformed") for f in rb.flags)
        assert rb.publication_state == PUBLICATION_UNKNOWN

    @pytest.mark.parametrize("field", ["approvedCountries", "pendingCountries", "disapprovedCountries",
                                       "applicableCountries"])
    @pytest.mark.parametrize("bad", ["SA\n", "SA\r\n", "SA ", " SA", "S A", "SA\t", "SA\x00", "sa", "Sa",
                                     "\uff33\uff21", "SAU", "S", ""])
    def test_every_country_array_requires_exact_two_letter_codes(self, field, bad):
        dest = _dest(approved=("SA",))
        issue = {"code": "x", "severity": "DEMOTED", "reportingContext": "SHOPPING_ADS", "applicableCountries": ["SA"]}
        if field == "applicableCountries":
            issue[field] = ["SA", bad]
        else:
            dest[field] = ["SA", bad] if field == "approvedCountries" else [bad]
        rb = parse_product_readback(_product(status={"destinationStatuses": [dest], "itemLevelIssues": [issue]}))
        assert rb.malformed and "countries_malformed" in rb.flags
        assert rb.publication_state == PUBLICATION_UNKNOWN

    def test_exact_country_codes_still_parse(self):
        rb = parse_product_readback(_product(status={"destinationStatuses": [_dest(approved=("SA", "AE"))]}))
        assert (rb.malformed, rb.publication_state, rb.destinations[0].approved_countries) == (
            False, PUBLICATION_APPROVED, ("SA", "AE"))

    @pytest.mark.parametrize("raw", [None, 1, "x", [], [_product()]])
    def test_non_object_readback_is_malformed(self, raw):
        rb = parse_product_readback(raw)
        assert rb.flags == ("readback_malformed", "status_field_missing")
        assert rb.publication_state == PUBLICATION_UNKNOWN

    def test_attribute_projection_match(self):
        assert attributes_match(ATTRS, {**ATTRS, "googleAddedField": "x"})
        assert not attributes_match(ATTRS, {**ATTRS, "title": "stale title"})
        assert not attributes_match(ATTRS, {k: v for k, v in ATTRS.items() if k != "price"})
        assert not attributes_match(ATTRS, {**ATTRS, "brand": "old"}, removed=("brand",))
        assert not attributes_match(ATTRS, None)


# ─────────────────────────────────────────────────────────────────────
# Retry classification and scheduling
# ─────────────────────────────────────────────────────────────────────


class TestClassification:

    def test_429_may_resend_with_retry_after(self):
        for write in (True, False):
            cls = classify_response(TransportResponse(429, {"retry-after": "30"}), write=write, now=NOW)
            assert (cls.kind, cls.resend_allowed, cls.requires_readback, cls.retry_after_s) == (
                RESP_RATE_LIMITED, True, False, 30)

    def test_retry_after_forms(self):
        assert parse_retry_after("120", NOW) == 120
        assert parse_retry_after(formatdate(NOW + 30, usegmt=True), NOW) == 30
        assert parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT", NOW) == 0  # past date clamps
        for bad in ("soon", "-5", "", None, "1.5", "Mon, 99 Foo 2027 00:00:00 GMT"):
            assert parse_retry_after(bad, NOW) is None

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 408, None])
    def test_write_5xx_or_timeout_requires_readback_and_no_resend(self, status):
        resp = TransportResponse(status, error=None if status else "timeout")
        cls = classify_response(resp, write=True, now=NOW)
        assert (cls.kind, cls.resend_allowed, cls.requires_readback) == (RESP_UNCERTAIN_WRITE, False, True)

    @pytest.mark.parametrize("status", [500, 503, 408, None])
    def test_read_5xx_or_timeout_is_transient(self, status):
        cls = classify_response(TransportResponse(status, error=None if status else "timeout"), write=False, now=NOW)
        assert (cls.kind, cls.resend_allowed) == (RESP_TRANSIENT_READ, True)

    @pytest.mark.parametrize("status,kind", [
        (401, RESP_PERMANENT_AUTH), (403, RESP_PERMANENT_PERMISSION),
        (400, RESP_PERMANENT_VALIDATION), (409, RESP_PERMANENT_VALIDATION), (422, RESP_PERMANENT_VALIDATION),
    ])
    def test_permanent_errors_never_resend(self, status, kind):
        for write in (True, False):
            cls = classify_response(TransportResponse(status), write=write, now=NOW)
            assert (cls.kind, cls.resend_allowed, cls.requires_readback) == (kind, False, False)

    def test_404_read_vs_write(self):
        assert classify_response(TransportResponse(404), write=False, now=NOW).kind == RESP_NOT_FOUND
        assert classify_response(TransportResponse(404), write=True, now=NOW).kind == RESP_PERMANENT_VALIDATION

    def test_2xx_write_is_accepted_pending_readback_only(self):
        cls = classify_response(TransportResponse(200, body={}), write=True, now=NOW)
        assert (cls.kind, cls.requires_readback, cls.resend_allowed) == (RESP_ACCEPTED, True, False)

    def test_unexpected_status_on_write_is_uncertain(self):
        assert classify_response(TransportResponse(302), write=True, now=NOW).kind == RESP_UNCERTAIN_WRITE

    def test_backoff_schedule_is_deterministic_and_bounded(self):
        policy = RetryPolicy(max_attempts=4, base_delay_s=2, max_delay_s=5)
        assert [next_retry_at(policy, n, 0, None) for n in (1, 2, 3, 4)] == [2, 4, 5, None]
        assert next_retry_at(policy, 1, 100, 4) == 104  # Retry-After honoured over shorter backoff
        assert next_retry_at(policy, 1, 100, 6) is None  # Retry-After beyond cap gives up
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0, base_delay_s=1, max_delay_s=1)


# ─────────────────────────────────────────────────────────────────────
# Static safety: no transport imports, no hardcoded live ids
# ─────────────────────────────────────────────────────────────────────

_RUNTIME = [
    REPO_ROOT / "backend/services/google_merchant_protocol_v1.py",
    REPO_ROOT / "backend/services/google_merchant_lifecycle_v1.py",
]
_FORBIDDEN_IMPORTS = {"socket", "ssl", "http", "urllib.request", "requests", "httpx", "aiohttp",
                      "sqlalchemy", "models", "database", "os", "time", "asyncio", "threading"}


@pytest.mark.parametrize("path", _RUNTIME, ids=lambda p: p.name)
def test_runtime_modules_have_no_transport_or_clock_and_no_account_ids(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            assert name not in _FORBIDDEN_IMPORTS and name.split(".")[0] not in {"socket", "requests", "httpx", "http"}
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
            text = str(node.value)
            assert not (text.isdigit() and len(text) >= 8), f"numeric id-like literal {text!r}"
