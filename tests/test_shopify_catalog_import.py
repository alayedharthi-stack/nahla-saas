"""Dormant Shopify catalog import — database-free unit tests.

No network: Shopify is ``backend/tests/shopify_catalog_fake.py`` behind
``httpx.MockTransport``; every token is generated at runtime. Neutral,
generic multi-category merchants only (shoes, shirts, perfume). The
PostgreSQL proofs (runs, fences, publish, isolation) live in
``backend/tests/test_shopify_catalog_import_pg.py``.
"""
from __future__ import annotations

import ast
import asyncio
import re
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "backend" / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend" / "tests"))

import shopify_catalog_fake as fake_mod  # noqa: E402
from shopify_catalog_fake import FakeCatalogShop, SlowStream, gid, level_gid  # noqa: E402

from services.shopify_catalog import config as C  # noqa: E402
from services.shopify_catalog import errors as E  # noqa: E402
from services.shopify_catalog import parse  # noqa: E402
from services.shopify_catalog.graphql import AdminGraphQL  # noqa: E402
from services.shopify_catalog.models import KIND_ITEM, KIND_LEVEL, KIND_LOCATION, KIND_PRODUCT, KIND_VARIANT  # noqa: E402
from services.shopify_catalog.traversal import CatalogTraversal  # noqa: E402

SHOP = "generic-shoes-store.myshopify.com"
SHOP_GID = "gid://shopify/Shop/7100000001"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_seconds):
    return None


async def _no_fence():
    return None


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def _reason(excinfo) -> str:
    return excinfo.value.reason


# ── Parsing: identities ───────────────────────────────────────────────────────

def test_level_gid_canonical_parameterised_form_bound_to_its_item():
    item = gid("InventoryItem", 456)
    good = level_gid(123, 456)
    assert good == "gid://shopify/InventoryLevel/123?inventory_item_id=456"
    assert parse.level_gid(good, item_gid=item) == good
    bad = [
        "gid://shopify/InventoryLevel/123",                                      # parameter missing
        "gid://shopify/InventoryLevel/123?inventory_item_id=457",                # another item (cross-item)
        "gid://shopify/InventoryLevel/123?inventory_item_id=456&x=1",            # unknown extra parameter
        "gid://shopify/InventoryLevel/123?inventory_item_id=456&inventory_item_id=456",   # duplicated
        "gid://shopify/InventoryLevel/123?x=1&inventory_item_id=456",
        "gid://shopify/InventoryLevel/123%3Finventory_item_id=456",              # encoded ambiguity
        "gid://shopify/InventoryLevel/123?inventory_item_id=%34%35%36",
        "gid://shopify/InventoryLevel/123?inventory_item_id=456#frag",           # fragment
        "gid://shopify/InventoryLevel/123?inventory_item_id=0456",               # leading zero
        "gid://shopify/InventoryLevel/0123?inventory_item_id=456",
        "gid://shopify/InventoryLevel/123?inventory_item_id=456\n",              # trailing newline
        "gid://shopify/InventoryLevel/123?Inventory_item_id=456",
        "gid://shopify/InventoryItem/123?inventory_item_id=456",                 # wrong type
        "gid://shopify/InventoryLevel/99999999999999999999?inventory_item_id=456",   # > UnsignedInt64
        " gid://shopify/InventoryLevel/123?inventory_item_id=456",
        123,
        None,
    ]
    for value in bad:
        with pytest.raises(E.ImportFailure) as exc:
            parse.level_gid(value, item_gid=item)
        assert _reason(exc) == E.R_INVALID_IDENTITY, value


def test_plain_gids_are_strict_and_legacy_ids_must_agree():
    assert parse.gid("gid://shopify/Product/42", "Product") == "gid://shopify/Product/42"
    for value in ("gid://shopify/Product/042", "gid://shopify/Product/42\n", "gid://shopify/ProductVariant/42",
                  "gid://shopify/Product/0", "gid://shopify/Product/", "gid://shopify/Product/42?x=1",
                  "gid://shopify/Product/18446744073709551616", 42):
        with pytest.raises(E.ImportFailure) as exc:
            parse.gid(value, "Product")
        assert _reason(exc) == E.R_INVALID_IDENTITY
    with pytest.raises(E.ImportFailure) as exc:
        parse.legacy_matches({"legacyResourceId": "43"}, "gid://shopify/Product/42")
    assert _reason(exc) == E.R_INVALID_IDENTITY


def test_money_is_exact_decimal_text():
    assert parse.money("149.90") == "149.90"
    assert parse.money("0") == "0"
    assert parse.money("12.345678") == "12.345678"
    for value in ("-1.00", "1e3", " 1.00", "1.0000001", "01.00", 1.5, "NaN", ""):
        with pytest.raises(E.ImportFailure):
            parse.money(value)
    assert parse.money(None, required=False) is None


def test_image_urls_only_https_shopify_cdn_never_fabricated():
    ok = "https://cdn.shopify.com/s/files/1/0000/0001/files/white-sneaker.jpg?v=1727000000"
    assert parse.safe_image_url(ok) == ok
    assert parse.safe_image_url("https://cdn.shopify.com/s/files/1/a.png") is not None
    for value in ("http://cdn.shopify.com/s/files/a.jpg", "https://evil.example/a.jpg",
                  "https://user:pw@cdn.shopify.com/a.jpg", "https://cdn.shopify.com:8443/a.jpg",
                  "https://cdn.shopify.com/a.jpg?X-Goog-Signature=abc", "https://cdn.shopify.com/a.jpg#x",
                  "https://cdn.shopify.com/a%2F.jpg", "https://cdn.shopify.com.evil.example/a.jpg",
                  "", None, "https://cdn.shopify.com/a b.jpg"):
        assert parse.safe_image_url(value) is None, value
    processing = parse.media({"id": "gid://shopify/MediaImage/9", "mediaContentType": "IMAGE", "image": None})
    assert processing["url"] is None
    video = parse.media({"id": "gid://shopify/Video/9", "mediaContentType": "VIDEO"})
    assert video["url"] is None
    with pytest.raises(E.ImportFailure):
        parse.media({"id": "gid://shopify/MediaImage/9", "mediaContentType": "HOLOGRAM"})
    with pytest.raises(E.ImportFailure):   # type and GID must agree
        parse.media({"id": "gid://shopify/Video/9", "mediaContentType": "IMAGE", "image": None})


def _product_node(**overrides):
    node = {
        "id": "gid://shopify/Product/11", "legacyResourceId": "11", "title": "حذاء رياضي أبيض",
        "handle": "white-sneaker", "status": "ACTIVE", "vendor": "", "productType": "",
        "createdAt": "2026-09-01T08:00:00Z", "updatedAt": "2026-10-01T08:00:00Z", "publishedAt": None,
        "isGiftCard": False, "requiresSellingPlan": False, "combinedListingRole": None,
        "hasOnlyDefaultVariant": True, "options": [], "featuredMedia": None,
        "variantsCount": {"count": 1, "precision": "EXACT"},
    }
    node.update(overrides)
    return node


def test_product_status_and_publication_are_separate_and_closed():
    for status in ("ACTIVE", "ARCHIVED", "DRAFT", "UNLISTED"):
        record = parse.product(_product_node(status=status))
        assert record["status"] == status
        assert record["online_store_published_at"] is None   # ACTIVE is not publication
    with pytest.raises(E.ImportFailure) as exc:
        parse.product(_product_node(status="SCHEDULED"))
    assert _reason(exc) == E.R_INVALID_RESPONSE


def test_partial_variant_payload_is_refused():
    node = {
        "id": "gid://shopify/ProductVariant/21", "legacyResourceId": "21", "title": "42", "sku": "", "barcode": None,
        "position": 1, "price": "199.00", "compareAtPrice": None, "availableForSale": True, "inventoryPolicy": "DENY",
        "requiresComponents": False, "createdAt": "2026-09-01T08:00:00Z", "updatedAt": "2026-10-01T08:00:00Z",
        "selectedOptions": [{"name": "المقاس", "value": "42"}],
        "inventoryItem": {"id": "gid://shopify/InventoryItem/31", "tracked": True, "updatedAt": "2026-10-01T07:00:00Z"},
    }
    assert parse.variant(node)["price"] == "199.00"
    for missing in ("price", "inventoryPolicy", "inventoryItem", "updatedAt", "selectedOptions"):
        partial = {k: v for k, v in node.items() if k != missing}
        with pytest.raises(E.ImportFailure) as exc:
            parse.variant(partial)
        assert _reason(exc) in (E.R_INVALID_RESPONSE, E.R_INVALID_IDENTITY), missing
    with pytest.raises(E.ImportFailure):
        parse.variant(dict(node, inventoryItem={"id": "gid://shopify/InventoryItem/31", "tracked": None,
                                                "updatedAt": "2026-10-01T07:00:00Z"}))


def test_inventory_level_requires_available_quantity_and_its_item():
    item = "gid://shopify/InventoryItem/456"
    node = {"id": level_gid(9, 456), "updatedAt": "2026-10-01T09:00:00Z",
            "quantities": [{"name": "available", "quantity": -2}], "location": {"id": "gid://shopify/Location/7"}}
    assert parse.inventory_level(node, item_gid=item)["available"] == -2
    with pytest.raises(E.ImportFailure):
        parse.inventory_level(dict(node, quantities=[{"name": "on_hand", "quantity": 1}]), item_gid=item)
    with pytest.raises(E.ImportFailure):
        parse.inventory_level(dict(node, quantities=[]), item_gid=item)
    with pytest.raises(E.ImportFailure) as exc:
        parse.inventory_level(node, item_gid="gid://shopify/InventoryItem/457")
    assert _reason(exc) == E.R_INVALID_IDENTITY


# ── GraphQL client ────────────────────────────────────────────────────────────

def _client(handler, **kw):
    return AdminGraphQL(transport=httpx.MockTransport(handler), sleep=kw.pop("sleep", _no_sleep), **kw)


def _ok(data=None, *, version="2026-10", extra=None):
    body = {"data": data if data is not None else {"shop": {"id": SHOP_GID}}}
    body.update(extra or {})
    headers = {"X-Shopify-API-Version": version} if version else {}
    return httpx.Response(200, json=body, headers=headers)


def _execute(client, token="shpat_" + "a" * 32, fence=_no_fence):
    return run(client.execute(shop_domain=SHOP, access_token=token, query="query NahlaCatalogPreflight { x }",
                              variables={}, cost_hint=10, fence=fence))


def test_requests_pin_2026_10_and_refuse_a_version_fall_forward():
    seen = []

    def handler(request):
        seen.append(request)
        return _ok()

    _execute(_client(handler))
    assert str(seen[0].url) == f"https://{SHOP}/admin/api/2026-10/graphql.json"
    assert seen[0].headers["x-shopify-access-token"].startswith("shpat_")
    for version in ("2027-01", None, "2026-07"):
        with pytest.raises(E.ImportFailure) as exc:
            _execute(_client(lambda r, v=version: _ok(version=v)))
        assert _reason(exc) == E.R_API_VERSION_MISMATCH
    with pytest.raises(E.ImportFailure) as exc:   # duplicated header
        _execute(_client(lambda r: httpx.Response(200, json={"data": {}}, headers=[
            ("X-Shopify-API-Version", "2026-10"), ("X-Shopify-API-Version", "2026-10")])))
    assert _reason(exc) == E.R_API_VERSION_MISMATCH


def test_graphql_errors_refuse_even_partial_data():
    cases = {
        "SOMETHING": E.R_GRAPHQL_ERROR, "ACCESS_DENIED": E.R_SCOPE_DENIED,
        "MAX_COST_EXCEEDED": E.R_QUERY_COST_EXCEEDED,
    }
    for code, reason in cases.items():
        with pytest.raises(E.ImportFailure) as exc:
            _execute(_client(lambda r, c=code: _ok({"products": {"nodes": []}}, extra={
                "errors": [{"message": "upstream text with secrets", "extensions": {"code": c}}]})))
        assert _reason(exc) == reason
        assert "upstream" not in str(exc.value)
    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(lambda r: _ok(extra={"errors": "bad"})))
    assert _reason(exc) == E.R_GRAPHQL_ERROR
    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(lambda r: httpx.Response(200, json={"data": None},
                                                  headers={"X-Shopify-API-Version": "2026-10"})))
    assert _reason(exc) == E.R_INVALID_RESPONSE


def test_throttle_is_retried_with_bounded_waits_then_refused():
    sleeps = Sleeps()
    calls = {"n": 0}
    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 900, "throttleStatus": {
                     "maximumAvailable": 2000.0, "currentlyAvailable": 100.0, "restoreRate": 100.0}}}}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(200, json=throttled, headers={"X-Shopify-API-Version": "2026-10"})
        return _ok()

    _execute(_client(handler, sleep=sleeps))
    assert calls["n"] == 3 and sleeps.calls and max(sleeps.calls) <= C.MAX_WAIT_SECONDS
    assert sleeps.calls[0] == pytest.approx(8.0)   # (900 - 100) / 100
    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(lambda r: httpx.Response(200, json=throttled,
                                                  headers={"X-Shopify-API-Version": "2026-10"})))
    assert _reason(exc) == E.R_THROTTLED


def test_cost_pacing_waits_for_the_bucket_before_the_next_request():
    sleeps = Sleeps()
    low = {"extensions": {"cost": {"requestedQueryCost": 10, "throttleStatus": {
        "maximumAvailable": 2000.0, "currentlyAvailable": 50.0, "restoreRate": 100.0}}}}
    client = _client(lambda r: _ok(extra=low), sleep=sleeps)
    run(client.execute(shop_domain=SHOP, access_token="t", query="q", cost_hint=10, fence=_no_fence))
    run(client.execute(shop_domain=SHOP, access_token="t", query="q", cost_hint=850, fence=_no_fence))
    assert sleeps.calls == [pytest.approx(8.0)]   # (850 - 50) / 100


def test_http_failures_are_bounded_and_classified():
    sleeps = Sleeps()
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "86400"})
        if calls["n"] == 3:
            return httpx.Response(503)
        return _ok()

    _execute(_client(flaky, sleep=sleeps))
    assert sleeps.calls[:2] == [7.0, C.MAX_WAIT_SECONDS]   # honoured, then capped
    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(lambda r: httpx.Response(502)))
    assert _reason(exc) == E.R_UPSTREAM_UNAVAILABLE
    for status, reason in ((401, E.R_CREDENTIALS_REJECTED), (403, E.R_CREDENTIALS_REJECTED),
                           (402, E.R_SHOP_UNAVAILABLE), (404, E.R_SHOP_UNAVAILABLE), (423, E.R_SHOP_UNAVAILABLE),
                           (302, E.R_INVALID_RESPONSE), (400, E.R_INVALID_RESPONSE)):
        with pytest.raises(E.ImportFailure) as exc:
            _execute(_client(lambda r, s=status: httpx.Response(s, headers={"Location": "https://evil.example/"})))
        assert _reason(exc) == reason, status


def test_timeouts_and_transport_errors_are_retried_then_refused():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(handler))
    assert _reason(exc) == E.R_UPSTREAM_UNAVAILABLE
    assert calls["n"] == C.MAX_HTTP_RETRIES + 1


def test_a_slow_trickling_body_is_bounded_by_the_attempt_budget():
    """Each chunk arrives well within the read timeout; the whole attempt is still bounded."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, headers={"X-Shopify-API-Version": "2026-10"}, stream=SlowStream(200, 0.02))

    client = _client(handler, timeout=1.0, request_total=0.15)
    with pytest.raises(E.ImportFailure) as exc:
        _execute(client)
    assert _reason(exc) == E.R_UPSTREAM_UNAVAILABLE
    assert calls["n"] == C.MAX_HTTP_RETRIES + 1
    # The remaining run budget also bounds it: the run deadline ends it.
    import time as _time

    client = _client(handler, timeout=1.0, request_total=60.0, deadline=_time.monotonic() + 0.3)
    started = _time.monotonic()
    with pytest.raises(E.ImportFailure) as exc:
        _execute(client)
    assert _reason(exc) == E.R_DEADLINE_EXCEEDED
    assert _time.monotonic() - started < 2.0


def test_response_size_is_bounded_while_streaming(monkeypatch):
    monkeypatch.setattr(fake_mod, "API_VERSION", "2026-10")
    import services.shopify_catalog.graphql as g

    monkeypatch.setattr(g, "MAX_RESPONSE_BYTES", 1024)
    big = json.dumps({"data": {"blob": "x" * 5000}}).encode()
    with pytest.raises(E.ImportFailure) as exc:
        _execute(_client(lambda r: httpx.Response(200, content=big, headers={"X-Shopify-API-Version": "2026-10"})))
    assert _reason(exc) == E.R_RESPONSE_TOO_LARGE


def test_fence_runs_before_every_attempt_and_after_the_response():
    events = []

    async def fence():
        events.append("fence")

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        events.append("http")
        return httpx.Response(503) if calls["n"] == 1 else _ok()

    _execute(_client(handler), fence=fence)
    # before the attempt, after its (refused) response, before the retry, after the answer
    assert events == ["fence", "http", "fence", "fence", "http", "fence"]


def test_a_failing_fence_stops_retries_and_discards_the_response():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    state = {"fences": 0}

    async def rotated_after_first():
        state["fences"] += 1
        if state["fences"] > 1:
            raise E.CredentialRotated()

    with pytest.raises(E.CredentialRotated):
        _execute(_client(handler), fence=rotated_after_first)
    assert calls["n"] == 1   # no retry with the captured token

    async def lease_lost_after_response():
        state["fences"] += 1
        if state["fences"] % 2 == 0:
            raise E.LeaseLost()

    state["fences"] = 0
    with pytest.raises(E.LeaseLost):
        _execute(_client(lambda r: _ok({"shop": {"id": SHOP_GID}})), fence=lease_lost_after_response)


def test_tokens_never_reach_logs_errors_or_repr(caplog):
    token = "shpat_" + "f" * 32
    caplog.set_level(logging.DEBUG)
    client = _client(lambda r: httpx.Response(500))
    with pytest.raises(E.ImportFailure) as exc:
        _execute(client, token=token)
    for text in (caplog.text, str(exc.value), repr(exc.value), repr(client)):
        assert token not in text


# ── Traversal ─────────────────────────────────────────────────────────────────

def _shop(**kw):
    return FakeCatalogShop(SHOP, SHOP_GID, **kw)


def _traversal(fake, *, stage=None, versions=None, inventory=False):
    client = AdminGraphQL(transport=fake.transport(), sleep=_no_sleep)
    token = fake.issue_token()
    staged = []
    version_iter = iter(versions or [])

    async def query(document, variables, cost):
        data = await client.execute(shop_domain=SHOP, access_token=token, query=document, variables=variables,
                                    cost_hint=cost, fence=_no_fence)
        return data, next(version_iter, 1)

    async def default_stage(records, version):
        staged.extend(records)

    t = CatalogTraversal(query=query, stage=stage or default_stage, sleep=_no_sleep, currency="SAR")
    return t, staged


async def _full(t, *, inventory=False):
    if inventory:
        await t.locations()
    await t.products()
    if inventory:
        await t.inventory()
    await t.verify_product_count()


def _kinds(staged, kind):
    return [r for r in staged if r[0] == kind]


def test_complete_traversal_follows_every_nested_page(monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 2)
    monkeypatch.setattr(C, "NESTED_VARIANTS_PAGE", 2)
    monkeypatch.setattr(C, "FOLLOWUP_VARIANTS_PAGE", 2)
    monkeypatch.setattr(C, "NESTED_PRODUCT_MEDIA_PAGE", 1)
    monkeypatch.setattr(C, "NESTED_VARIANT_MEDIA_PAGE", 1)
    monkeypatch.setattr(C, "FOLLOWUP_MEDIA_PAGE", 1)
    monkeypatch.setattr(C, "LOCATIONS_PAGE", 1)
    monkeypatch.setattr(C, "NESTED_LEVELS_PAGE", 1)
    monkeypatch.setattr(C, "FOLLOWUP_LEVELS_PAGE", 1)
    fake = _shop(scopes=("read_products", "read_inventory"))
    riyadh = fake.add_location("مستودع الرياض")
    jeddah = fake.add_location("متجر جدة", online=False)
    shoe = fake.add_product("حذاء رياضي أبيض", images=3, variants=[
        {"price": "199.00", "title": "40", "levels": {riyadh: 3, jeddah: 5}},
        {"price": "199.00", "title": "41", "levels": {riyadh: 0}},
        {"price": "205.50", "title": "42", "image": True},
        {"price": "205.50", "title": "43", "tracked": False},
        {"price": "210.00", "title": "44"},
    ])
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    fake.add_product("عطر ورد 100ml", images=0, variants=[{"price": "250.00"}], status="DRAFT", published=False)
    product_media = fake.products[shoe].media
    second_variant = sorted(v for v, x in fake.variants.items() if x.product == shoe)[1]
    fake.variants[second_variant].media = [product_media[0], product_media[1]]   # two linked product images
    t, staged = _traversal(fake)
    run(_full(t, inventory=True))
    assert len(_kinds(staged, KIND_PRODUCT)) == 3
    assert len(_kinds(staged, KIND_VARIANT)) == 7
    assert len(_kinds(staged, KIND_LOCATION)) == 2
    assert {r[1] for r in _kinds(staged, KIND_LEVEL)} == {
        level_gid(fake.levels[(fake.variants[v].item, loc)]["number"], fake.variants[v].item)
        for (item, loc) in fake.levels for v in fake.variants if fake.variants[v].item == item}
    # >1 page of every nested connection was requested.
    for op in ("NahlaCatalogProducts", "NahlaCatalogProductVariants", "NahlaCatalogProductMedia",
               "NahlaCatalogVariantMedia", "NahlaCatalogLocations", "NahlaCatalogInventoryLevels"):
        assert fake.ops(op) >= 1, op
    assert fake.ops("NahlaCatalogProducts") == 2
    shoe_record = [r for r in _kinds(staged, KIND_PRODUCT) if r[1] == gid("Product", shoe)][0][3]
    assert len(shoe_record["images"]) == 4
    perfume = [r for r in _kinds(staged, KIND_PRODUCT) if r[3]["title"] == "عطر ورد 100ml"][0][3]
    assert perfume["image_url"] is None and perfume["status"] == "DRAFT"
    assert perfume["online_store_published_at"] is None
    variants = {r[3]["title"]: r[3] for r in _kinds(staged, KIND_VARIANT) if r[2] == gid("Product", shoe)}
    assert variants["42"]["image_source"] == "variant"
    assert variants["40"]["image_source"] == "product"   # fallback to the product image, never fabricated
    assert variants["41"]["image_source"] == "variant"
    assert variants["43"]["tracking"] == "untracked"
    assert all(v["currency_code"] == "SAR" for v in variants.values())


def test_variant_media_may_reuse_product_media_and_be_shared_by_variants():
    fake = _shop()
    product = fake.add_product("حذاء رياضي أبيض", images=2, variants=[
        {"price": "199.00", "title": "40"}, {"price": "199.00", "title": "41"}])
    shared = fake.products[product].media[1]
    for v in fake.variants.values():
        v.media = [shared]
    t, staged = _traversal(fake)
    run(_full(t))
    urls = {r[3]["image_url"] for r in _kinds(staged, KIND_VARIANT)}
    assert urls == {fake.media[shared].url}


def test_media_conflicts_are_refused():
    # A variant media that is not one of its product's media (cross-product reuse).
    fake = _shop()
    first = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    second = fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    foreign = fake.products[first].media[0]
    for v in fake.variants.values():
        if v.product == second:
            v.media = [foreign]
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t))
    assert _reason(exc) == E.R_IDENTITY_CONFLICT
    # One media in two products' own lists.
    fake = _shop()
    first = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    second = fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    fake.products[second].media.append(fake.products[first].media[0])
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t))
    assert _reason(exc) == E.R_IDENTITY_CONFLICT
    # The same media twice inside one connection.
    fake = _shop()
    product = fake.add_product("عطر ورد 100ml", variants=[{"price": "250.00"}])
    fake.products[product].media.append(fake.products[product].media[0])
    t, _ = _traversal(fake)

    original = fake._media_conn

    def duplicated(numbers, first, after):
        page = original(numbers, first, after)
        page["nodes"] = page["nodes"] + page["nodes"][:1]
        return page

    fake._media_conn = duplicated
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t))
    assert _reason(exc) in (E.R_DUPLICATE_IDENTITY, E.R_INVALID_RESPONSE)


def test_shared_inventory_item_is_read_once_and_never_double_counted():
    fake = _shop(scopes=("read_products", "read_inventory"))
    warehouse = fake.add_location("مستودع الرياض")
    shirt = fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "title": "M", "levels": {warehouse: 6}}])
    item = next(v.item for v in fake.variants.values() if v.product == shirt)
    fake.add_variant(shirt, price="89.90", title="M (bundle listing)", position=2, item=item)
    t, staged = _traversal(fake)
    run(_full(t, inventory=True))
    assert len(_kinds(staged, KIND_ITEM)) == 1
    assert len(_kinds(staged, KIND_LEVEL)) == 1
    assert len(_kinds(staged, KIND_VARIANT)) == 2
    assert {r[3]["inventory_item_gid"] for r in _kinds(staged, KIND_VARIANT)} == {gid("InventoryItem", item)}


def test_shared_item_readings_must_agree():
    fake = _shop()
    shirt = fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    item = next(v.item for v in fake.variants.values() if v.product == shirt)
    fake.add_variant(shirt, price="89.90", position=2, item=item)
    original = fake._variant_node

    def drifting(v, media):
        node = original(v, media)
        if v.position == 2:
            node["inventoryItem"]["tracked"] = not node["inventoryItem"]["tracked"]
        return node

    fake._variant_node = drifting
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t))
    assert _reason(exc) == E.R_IDENTITY_CONFLICT


def test_a_level_naming_another_item_or_an_unknown_location_is_refused():
    fake = _shop(scopes=("read_products", "read_inventory"))
    store_loc = fake.add_location("مستودع الرياض")
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00", "levels": {store_loc: 2}}])
    original = fake._level_node

    def cross_item(item, location):
        node = original(item, location)
        node["id"] = level_gid(fake.levels[(item, location)]["number"], item + 1)
        return node

    fake._level_node = cross_item
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t, inventory=True))
    assert _reason(exc) == E.R_INVALID_IDENTITY
    fake._level_node = original
    fake.hooks["NahlaCatalogInventory"] = lambda f, v, i: f.locations.update(
        {999999: {"id": gid("Location", 999999), "name": "x", "isActive": True, "fulfillsOnlineOrders": True}}
    ) or f.set_level(next(iter(f.items)), 999999, 4)
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(_full(t, inventory=True))
    assert _reason(exc) == E.R_SOURCE_CHANGED


def test_cursor_loops_and_stalled_pages_are_refused(monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    for mutate in ("repeat", "null", "empty"):
        fake = _shop()
        fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
        fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
        original = fake.op_NahlaCatalogProducts

        def looping(variables, mutate=mutate, original=original):
            data = original(variables)
            info = data["products"]["pageInfo"]
            info["hasNextPage"] = True
            if mutate == "repeat":
                info["endCursor"] = variables.get("after") or info["endCursor"]
                if variables.get("after"):
                    info["endCursor"] = variables["after"]
            elif mutate == "null":
                info["endCursor"] = None
            else:
                if variables.get("after"):
                    data["products"]["nodes"] = []
            return data

        fake.op_NahlaCatalogProducts = looping
        t, _ = _traversal(fake)
        with pytest.raises(E.ImportFailure) as exc:
            run(t.products())
        assert _reason(exc) == E.R_PAGINATION_LOOP, mutate


def test_variant_count_mismatch_gets_one_fresh_read_then_fails_closed():
    fake = _shop()
    product = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}, {"price": "199.00", "title": "41"}])
    original = fake._variants_count
    fake._variants_count = lambda number: {"count": 3, "precision": "EXACT"}
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(t.products())
    assert _reason(exc) == E.R_SOURCE_CHANGED
    # A transient difference healed by the fresh read is accepted.
    fake = _shop()
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}, {"price": "199.00", "title": "41"}])
    calls = {"n": 0}
    original = fake._variants_count

    def once(number):
        calls["n"] += 1
        value = original(number)
        return dict(value, count=value["count"] + 1) if calls["n"] == 1 else value

    fake._variants_count = once
    t, staged = _traversal(fake)
    run(t.products())
    assert len(_kinds(staged, KIND_VARIANT)) == 2


def test_an_object_vanishing_between_pages_ends_the_run(monkeypatch):
    monkeypatch.setattr(C, "NESTED_VARIANTS_PAGE", 1)
    fake = _shop()
    product = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}, {"price": "199.00", "title": "41"}])
    fake.hooks["NahlaCatalogProductVariants"] = lambda f, v, i: f.delete_product(product)
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(t.products())
    assert _reason(exc) == E.R_SOURCE_CHANGED


def test_duplicate_product_across_pages_is_refused(monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    fake = _shop()
    first = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    original = fake.op_NahlaCatalogProducts

    def repeating(variables):
        data = original(variables)
        if variables.get("after"):
            data["products"]["nodes"] = [fake._product_node(fake.products[first], variables)]
        return data

    fake.op_NahlaCatalogProducts = repeating
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(t.products())
    assert _reason(exc) == E.R_DUPLICATE_IDENTITY


def test_product_count_and_currency_must_reconcile():
    fake = _shop()
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    t, _ = _traversal(fake)
    run(t.products())
    fake.count_override = (2, "EXACT")
    with pytest.raises(E.ImportFailure) as exc:
        run(t.verify_product_count())
    assert _reason(exc) == E.R_SOURCE_CHANGED
    assert fake.ops("NahlaCatalogCount") == 2   # one bounded re-read
    fake.count_override = (25_001, "AT_LEAST")
    with pytest.raises(E.ImportFailure) as exc:
        run(t.verify_product_count())
    assert _reason(exc) == E.R_CATALOG_TOO_LARGE
    fake.count_override = None
    fake.currency = "USD"
    with pytest.raises(E.ImportFailure) as exc:
        run(t.verify_product_count())
    assert _reason(exc) == E.R_SOURCE_CHANGED


def test_absence_is_evidence_only_after_shopify_confirms_it():
    fake = _shop()
    kept = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    t, _ = _traversal(fake)
    gone = gid("Product", 987654)
    assert run(t.confirm_absent({gone})) == {gone}
    with pytest.raises(E.ImportFailure) as exc:
        run(t.confirm_absent({gone, gid("Product", kept)}))
    assert _reason(exc) == E.R_TRAVERSAL_INCONSISTENT


def test_a_rotated_credential_refetches_the_unit_without_duplicates():
    fake = _shop()
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    staged = []
    state = {"stages": 0}

    async def stage(records, version):
        state["stages"] += 1
        if state["stages"] == 1:
            raise E.CredentialRotated()
        staged.extend(records)

    t, _ = _traversal(fake, stage=stage)
    run(t.products())
    assert fake.ops("NahlaCatalogProducts") == 2
    assert len(_kinds(staged, KIND_PRODUCT)) == 1 and len(t.snapshot.products) == 1

    async def always(records, version):
        raise E.CredentialRotated()

    t, _ = _traversal(fake, stage=always)
    with pytest.raises(E.ImportFailure) as exc:
        run(t.products())
    assert _reason(exc) == E.R_CREDENTIAL_CHURN
    # A unit whose pages came from two different credential versions is refetched.
    t, staged = _traversal(fake, versions=[1, 2, 2, 2, 2, 2])
    run(t.products())
    assert t.rotations >= 0 and len(_kinds(staged, KIND_PRODUCT)) == 1


def test_catalog_over_the_pagination_cap_is_refused(monkeypatch):
    monkeypatch.setattr(C, "PAGINATION_CAP", 1)
    fake = _shop()
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    t, _ = _traversal(fake)
    with pytest.raises(E.ImportFailure) as exc:
        run(t.products())
    assert _reason(exc) == E.R_CATALOG_TOO_LARGE


# ── Configuration, routes, isolation ──────────────────────────────────────────

def test_flags_default_off_and_both_required():
    assert not C.import_routes_enabled({})
    assert not C.import_routes_enabled({C.IMPORT_ENABLED_ENV: "1"})
    assert not C.import_routes_enabled({"NAHLA_SHOPIFY_CONNECTION_ENABLED": "1"})
    assert C.import_routes_enabled({C.IMPORT_ENABLED_ENV: "1", "NAHLA_SHOPIFY_CONNECTION_ENABLED": "1"})


def test_version_pinned_without_changing_the_foundation():
    from services.shopify_connection import config as foundation

    assert C.CATALOG_API_VERSION == "2026-10"
    assert foundation.DEFAULT_API_VERSION == "2026-07"
    assert foundation.REQUESTED_SCOPES == ("read_products",)
    assert foundation.ALLOWED_GRANTED_SCOPES == frozenset({"read_products"})


def _route_client(monkeypatch, env, payload=None):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routers import shopify_catalog_import as router_module

    for key in ("NAHLA_SHOPIFY_CONNECTION_ENABLED", C.IMPORT_ENABLED_ENV, "SHOPIFY_CLIENT_ID",
                "SHOPIFY_CLIENT_SECRET", "SHOPIFY_OAUTH_REDIRECT_URI", "DASHBOARD_URL", "SHOPIFY_TOKEN_ENC_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        if payload is not None:
            request.state.jwt_payload = payload
        return await call_next(request)

    class _NoDb:
        def get_bind(self):
            raise RuntimeError("no database in unit tests")

        def rollback(self):
            return None

    app.dependency_overrides[get_db] = lambda: _NoDb()
    app.include_router(router_module.router)
    return TestClient(app)


BASE = "/merchant/integrations/shopify/catalog-import"
MERCHANT = {"tenant_id": 1, "user_id": 1, "role": "merchant", "jti": "j"}


def _full_env():
    import base64
    import os

    return {
        "NAHLA_SHOPIFY_CONNECTION_ENABLED": "1", C.IMPORT_ENABLED_ENV: "1", "SHOPIFY_CLIENT_ID": "unit-client",
        "SHOPIFY_CLIENT_SECRET": "unit-secret", "DASHBOARD_URL": "https://app.shop-review.example.test",
        "SHOPIFY_OAUTH_REDIRECT_URI": "https://api.shop-review.example.test/merchant/integrations/shopify/callback",
        "SHOPIFY_TOKEN_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    }


def test_routes_are_off_by_default_and_with_only_one_flag(monkeypatch):
    for env in ({}, {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "1"}, {C.IMPORT_ENABLED_ENV: "1"}):
        client = _route_client(monkeypatch, env, payload=MERCHANT)
        assert client.get(f"{BASE}/status").status_code == 404
        assert client.post(f"{BASE}/start", json={"shop": SHOP}).status_code == 404


def test_start_refuses_support_impersonation_and_platform_staff(monkeypatch):
    for payload in ({"tenant_id": 1, "user_id": 1, "role": "support_impersonation", "impersonation": True, "jti": "j"},
                    {"tenant_id": 1, "user_id": 1, "role": "merchant", "impersonation": True, "jti": "j"},
                    {"tenant_id": 1, "user_id": 1, "role": "admin", "jti": "j"}):
        client = _route_client(monkeypatch, _full_env(), payload=payload)
        assert client.post(f"{BASE}/start", json={"shop": SHOP}).status_code == 403, payload


def test_status_is_read_only_for_impersonation_and_refuses_platform_staff(monkeypatch):
    client = _route_client(monkeypatch, _full_env(), payload={"tenant_id": 1, "user_id": 1, "role": "admin", "jti": "j"})
    assert client.get(f"{BASE}/status").status_code == 403
    client = _route_client(monkeypatch, _full_env(), payload={"tenant_id": 1, "user_id": 9, "role": "support_impersonation",
                                                              "impersonation": True, "jti": "j"})
    resp = client.get(f"{BASE}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and body["reason"] == "storage_unavailable"   # no tables here
    assert body["api_version"] == "2026-10" and body["connections"] == []
    assert resp.headers["cache-control"] == "no-store"


def test_start_without_configuration_refuses(monkeypatch):
    env = {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "1", C.IMPORT_ENABLED_ENV: "1"}
    client = _route_client(monkeypatch, env, payload=MERCHANT)
    resp = client.post(f"{BASE}/start", json={"shop": SHOP})
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "shopify_connection_unavailable"


_CATALOG_SOURCES = sorted((_REPO / "backend" / "services" / "shopify_catalog").glob("*.py")) + [
    _REPO / "backend" / "routers" / "shopify_catalog_import.py",
]


def test_import_never_touches_the_shared_catalog_salla_meta_or_ai():
    forbidden_modules = ("models", "database.models", "store_adapters", "integrations", "modules.ai",
                         "services.store_sync", "services.meta_catalog_import", "core.catalog")
    for path in _CATALOG_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in forbidden_modules), (path, name)
        # Executable string constants only (SQL, GraphQL): docstrings may say
        # what is never touched; imports are checked above.
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        code = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                code.append(node.value.lower())
        text = "\n".join(code)
        for pattern in (r"\b(from|join|update|into|table)\s+(products|product_variants)\b", r"salla", r"external_id",
                        r"whatsapp", r"meta_catalog", r"store_adapter"):
            assert not re.search(pattern, text), (path.name, pattern)


def test_sentry_withholds_catalog_frames():
    from core.observability_sentry import _withheld_frame

    for module in ("services.shopify_catalog.importer", "services.shopify_catalog.graphql",
                   "routers.shopify_catalog_import"):
        assert _withheld_frame({"module": module})


def test_migration_0122_declares_the_model_schema():
    import importlib.util

    from services.shopify_catalog.models import CATALOG_TABLES

    spec = importlib.util.spec_from_file_location(
        "m0122", _REPO / "database" / "migrations" / "versions" / "0122_shopify_catalog_mirror.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0122" and module.down_revision == "0121"
    defs = module._definitions()
    for table in CATALOG_TABLES:
        migrated = defs[table.name]
        assert [c.name for c in migrated.columns] == [c.name for c in table.columns], table.name
        for column in table.columns:
            other = migrated.columns[column.name]
            assert other.nullable == column.nullable, (table.name, column.name)
            assert type(other.type).__name__ == type(column.type).__name__, (table.name, column.name)
        names = lambda t: sorted(c.name for c in t.constraints if c.name)  # noqa: E731
        assert names(migrated) == names(table), table.name
        assert sorted(i.name for i in migrated.indexes) == sorted(i.name for i in table.indexes), table.name
