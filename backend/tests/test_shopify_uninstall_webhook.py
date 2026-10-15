"""Per-shop ``app/uninstalled`` subscription: Admin GraphQL client and the
bounded list → create-once → read-back attempt. No database, no network
(``httpx.MockTransport`` models Shopify's GraphQL answers).

Neutral generic shops only (platform-wide behaviour).
"""
import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "backend", ROOT / "database"):
    sys.path.insert(0, str(path))

from services.shopify_connection import config as shopify_config  # noqa: E402
from services.shopify_connection import oauth, uninstall_webhook  # noqa: E402
from services.shopify_connection.oauth import ShopifyApi, ShopifyApiError, WebhookSubscription  # noqa: E402

SHOP = "generic-shoes-store.myshopify.com"
TOKEN = "shpat_" + "a" * 32
PRODUCTION_REDIRECT = "https://api.nahlah.ai/merchant/integrations/shopify/callback"
URI = "https://api.nahlah.ai/webhooks/shopify/app-uninstalled"


def run(coro):
    return asyncio.run(coro)


async def direct(call):
    return await call()


class GraphQLShop:
    """Stateful Admin GraphQL model of one shop's subscriptions for this app.

    ``list_script`` / ``create_script``: per-call overrides consumed in order —
    an ``httpx.Response`` to answer with, an exception to raise, ``"lost"``
    (create performed, then the connection drops) or ``None`` (normal).
    """

    def __init__(self, *, page_size=2):
        self.subs = []
        self.page_size = page_size
        self.requests = []
        self.list_script, self.create_script = [], []
        self.next_id = 100

    def add(self, *, uri=URI, fmt="JSON", topic="APP_UNINSTALLED", include=None, flt=None):
        self.next_id += 1
        node = {"id": f"gid://shopify/WebhookSubscription/{self.next_id}", "topic": topic, "uri": uri,
                "format": fmt, "includeFields": include or [], "filter": flt}
        self.subs.append(node)
        return node

    def count(self, kind):
        return sum(1 for k, _ in self.requests if k == kind)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = "create" if "webhookSubscriptionCreate" in body["query"] else "list"
        self.requests.append((kind, {"url": str(request.url), "token": request.headers.get("X-Shopify-Access-Token"),
                                     "body": body}))
        script = self.create_script if kind == "create" else self.list_script
        step = script.pop(0) if script else None
        if isinstance(step, httpx.Response):
            return step
        if isinstance(step, Exception):
            raise step
        if kind == "create":
            sub = body["variables"]["subscription"]
            if any(s["uri"] == sub["uri"] and s["topic"] == "APP_UNINSTALLED" for s in self.subs):
                return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                    "webhookSubscription": None,
                    "userErrors": [{"field": ["webhookSubscription", "callbackUrl"],
                                    "message": "Address for this topic has already been taken"}]}}})
            node = self.add(uri=sub["uri"], fmt=sub["format"])
            if step == "lost":
                raise httpx.ReadTimeout("synthetic: created, response lost")
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": node, "userErrors": []}}})
        nodes = [s for s in self.subs if s["topic"] == "APP_UNINSTALLED"]
        after = body["variables"]["after"]
        first = body["variables"]["first"]
        start = int(after) if after else 0
        page = nodes[start:start + min(first, self.page_size)]
        more = start + len(page) < len(nodes)
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {
            "nodes": page, "pageInfo": {"hasNextPage": more, "endCursor": str(start + len(page)) if more else None}}}})


def api_for(shop: GraphQLShop, version="2026-07"):
    return ShopifyApi(client_id="synthetic-client", client_secret="synthetic-secret", api_version=version,
                      transport=httpx.MockTransport(shop))


def ensure(shop, api=None):
    return run(uninstall_webhook.ensure(direct, api or api_for(shop), shop_domain=SHOP, access_token=TOKEN, uri=URI))


# ── Target, scope and version invariants ─────────────────────────────────────

def test_callback_uri_is_the_production_uninstall_endpoint_on_the_oauth_api_host():
    assert uninstall_webhook.callback_uri(PRODUCTION_REDIRECT) == URI
    assert uninstall_webhook.callback_uri(
        "https://api.nahlah.ai/merchant/integrations/shopify/install/callback") == URI
    with pytest.raises(uninstall_webhook.RegistrationFailed):
        uninstall_webhook.callback_uri("http://api.nahlah.ai/merchant/integrations/shopify/callback")


def test_scope_version_and_install_flow_inputs_are_unchanged():
    # read_products only; configured Admin API version default unchanged; the
    # offline authorize URL still carries scope=read_products and no per-user grant.
    assert shopify_config.REQUESTED_SCOPES == ("read_products",)
    assert shopify_config.DEFAULT_API_VERSION == "2026-07"
    url = oauth.build_authorize_url(shop_domain=SHOP, client_id="c", redirect_uri=PRODUCTION_REDIRECT, state="s")
    assert "scope=read_products" in url and "grant_options" not in url
    # Only the one topic, the JSON format and the uri are ever sent.
    assert "topics: [APP_UNINSTALLED]" in oauth.UNINSTALL_WEBHOOKS_QUERY
    assert "topic: APP_UNINSTALLED" in oauth.UNINSTALL_WEBHOOK_CREATE
    assert "Delete" not in oauth.UNINSTALL_WEBHOOK_CREATE and "Update" not in oauth.UNINSTALL_WEBHOOK_CREATE


# ── Client requests and parsing ──────────────────────────────────────────────

def test_requests_use_configured_version_header_token_and_exact_variables():
    shop = GraphQLShop()
    ensure(shop, api_for(shop, version="2026-10"))
    (k1, listed), (k2, created), (k3, read_back) = shop.requests
    assert (k1, k2, k3) == ("list", "create", "list")
    for request in (listed, created, read_back):
        assert request["url"] == f"https://{SHOP}/admin/api/2026-10/graphql.json"
        assert request["token"] == TOKEN
    assert listed["body"]["variables"] == {"first": oauth.WEBHOOK_PAGE_SIZE, "after": None}
    assert created["body"]["variables"] == {"subscription": {"uri": URI, "format": "JSON"}}
    assert TOKEN not in json.dumps([r["body"] for _, r in shop.requests])   # never in a body or URL


@pytest.mark.parametrize("response,kind,code", [
    (httpx.Response(429, json={}), "transient", "upstream_unavailable"),
    (httpx.Response(500, json={}), "transient", "upstream_unavailable"),
    (httpx.Response(503, json={}), "transient", "upstream_unavailable"),
    (httpx.Response(401, json={}), "rejected", "credentials_rejected"),
    (httpx.Response(403, json={}), "rejected", "credentials_rejected"),
    (httpx.Response(404, json={}), "invalid_response", "unexpected_status"),
    (httpx.Response(200, json={"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}),
     "transient", "throttled"),
    (httpx.Response(200, json={"errors": [{"message": "denied", "extensions": {"code": "ACCESS_DENIED"}}]}),
     "permanent", "access_denied"),
    (httpx.Response(200, json={"errors": "Not Found"}), "invalid_response", "graphql_errors"),
    (httpx.Response(200, json={"data": None}), "invalid_response", "webhook_response_invalid"),
    (httpx.Response(200, json={"data": {"webhookSubscriptions": {"nodes": [{"id": "x"}],
                                                                 "pageInfo": {"hasNextPage": False}}}}),
     "invalid_response", "webhook_response_invalid"),
    (httpx.Response(200, json={"data": {"webhookSubscriptions": {"nodes": [],
                                                                 "pageInfo": {"hasNextPage": True}}}}),
     "invalid_response", "webhook_cursor_invalid"),
])
def test_list_failures_are_classified_never_read_as_empty(response, kind, code):
    shop = GraphQLShop()
    shop.list_script = [response]
    with pytest.raises(ShopifyApiError) as info:
        run(api_for(shop).uninstall_webhook_page(shop_domain=SHOP, access_token=TOKEN))
    assert (info.value.kind, info.value.code) == (kind, code)


def test_list_parses_nodes_and_follows_the_cursor(monkeypatch):
    monkeypatch.setattr(oauth, "WEBHOOK_PAGE_SIZE", 2)
    shop = GraphQLShop(page_size=2)
    for index in range(3):
        shop.add(uri=f"https://other-{index}.example.test/hook")
    api = api_for(shop)
    first, cursor = run(api.uninstall_webhook_page(shop_domain=SHOP, access_token=TOKEN))
    assert [type(s) for s in first] == [WebhookSubscription] * 2 and cursor == "2"
    second, cursor = run(api.uninstall_webhook_page(shop_domain=SHOP, access_token=TOKEN, after=cursor))
    assert len(second) == 1 and cursor is None
    assert shop.requests[1][1]["body"]["variables"]["after"] == "2"


@pytest.mark.parametrize("payload,kind,code", [
    ({"webhookSubscription": None, "userErrors": [{"field": ["uri"], "message": "Address is invalid"}]},
     "permanent", "webhook_user_errors"),
    ({"webhookSubscription": None, "userErrors": []}, "invalid_response", "webhook_response_invalid"),
    (None, "invalid_response", "webhook_response_invalid"),
])
def test_create_user_errors_and_malformed_payloads_raise(payload, kind, code):
    shop = GraphQLShop()
    shop.create_script = [httpx.Response(200, json={"data": {"webhookSubscriptionCreate": payload}})]
    with pytest.raises(ShopifyApiError) as info:
        run(api_for(shop).create_uninstall_webhook(shop_domain=SHOP, access_token=TOKEN, uri=URI))
    assert (info.value.kind, info.value.code) == (kind, code)


# ── ensure(): exact match, create once, always read back ─────────────────────

def test_existing_exact_subscription_is_the_proof_and_nothing_is_created():
    shop = GraphQLShop()
    node = shop.add()
    proof = ensure(shop)
    assert (proof.subscription_id, proof.created, proof.exact_count) == (node["id"], False, 1)
    assert shop.count("create") == 0 and shop.count("list") == 1


def test_exact_subscription_on_a_later_page_is_found_without_creating(monkeypatch):
    monkeypatch.setattr(oauth, "WEBHOOK_PAGE_SIZE", 2)
    shop = GraphQLShop(page_size=2)
    for index in range(4):
        shop.add(uri=f"https://unrelated-{index}.example.test/hooks")
    node = shop.add()
    proof = ensure(shop)
    assert proof.subscription_id == node["id"] and not proof.created
    assert shop.count("list") == 3 and shop.count("create") == 0


@pytest.mark.parametrize("near_miss", [
    {"uri": "https://api.nahlah.ai/webhooks/shopify/app-uninstalled/"},
    {"uri": "https://staging.nahlah.ai/webhooks/shopify/app-uninstalled"},
    {"topic": "SHOP_UPDATE"},
])
def test_near_misses_elsewhere_are_not_proof_and_one_exact_subscription_is_created(near_miss):
    shop = GraphQLShop()
    other = shop.add(**near_miss)
    proof = ensure(shop)
    assert proof.created and shop.count("create") == 1 and proof.subscription_id != other["id"]
    [exact] = [s for s in shop.subs if uninstall_webhook.is_exact(oauth.parse_webhook_subscription(s), URI)]
    assert proof.subscription_id == exact["id"] and other in shop.subs   # the near miss is left untouched


@pytest.mark.parametrize("near_miss", [{"fmt": "XML"}, {"include": ["id"]}, {"flt": "id:1"}])
def test_same_address_taken_by_a_non_exact_subscription_fails_closed(near_miss):
    # Format XML, or a payload-narrowing includeFields / filter, on our own
    # address: never proof. Shopify refuses a second subscription for the
    # address, the read-back finds no exact one, nothing is deleted or ready.
    shop = GraphQLShop()
    shop.add(**near_miss)
    with pytest.raises(uninstall_webhook.RegistrationFailed) as info:
        ensure(shop)
    assert info.value.code == "webhook_user_errors"
    assert shop.count("create") == 1 and len(shop.subs) == 1


def test_timeout_after_successful_create_reads_back_and_never_recreates():
    shop = GraphQLShop()
    shop.create_script = ["lost"]
    proof = ensure(shop)
    assert proof.created and shop.count("create") == 1 and len(shop.subs) == 1
    assert [k for k, _ in shop.requests] == ["list", "create", "list"]


@pytest.mark.parametrize("failure,code", [
    (httpx.Response(503, json={}), "upstream_unavailable"),
    (httpx.Response(429, json={}), "upstream_unavailable"),
    (httpx.Response(200, json={"errors": [{"extensions": {"code": "THROTTLED"}}]}), "throttled"),
    (httpx.ConnectError("synthetic: never sent"), "transport_error"),
    (httpx.Response(200, json={"errors": [{"extensions": {"code": "ACCESS_DENIED"}}]}), "access_denied"),
])
def test_failed_create_is_read_back_once_and_not_retried(failure, code):
    shop = GraphQLShop()
    shop.create_script = [failure]
    with pytest.raises(uninstall_webhook.RegistrationFailed) as info:
        ensure(shop)
    assert info.value.code == code
    assert [k for k, _ in shop.requests] == ["list", "create", "list"] and shop.subs == []


def test_create_refused_as_taken_by_a_concurrent_exact_create_is_proven_by_read_back():
    shop = GraphQLShop()
    original = shop.__call__

    def racing(request):
        body = json.loads(request.content)
        if "webhookSubscriptionCreate" in body["query"] and not shop.subs:
            shop.add()   # another worker's create lands first
        return original(request)

    proof = run(uninstall_webhook.ensure(direct, ShopifyApi(
        client_id="c", client_secret="s", api_version="2026-07", transport=httpx.MockTransport(racing)),
        shop_domain=SHOP, access_token=TOKEN, uri=URI))
    assert proof.created and len(shop.subs) == 1 and shop.count("create") == 1


@pytest.mark.parametrize("failure,code", [
    (httpx.Response(429, json={}), "upstream_unavailable"),
    (httpx.Response(502, json={}), "upstream_unavailable"),
    (httpx.Response(200, json={"errors": [{"extensions": {"code": "THROTTLED"}}]}), "throttled"),
])
def test_failed_first_list_never_creates(failure, code):
    shop = GraphQLShop()
    shop.list_script = [failure]
    with pytest.raises(ShopifyApiError) as info:
        ensure(shop)
    assert info.value.code == code and shop.count("create") == 0


def test_read_back_failure_after_create_is_not_proof_and_next_attempt_lists_first():
    shop = GraphQLShop()
    shop.create_script = ["lost"]
    shop.list_script = [None, httpx.Response(503, json={})]
    with pytest.raises(ShopifyApiError):
        ensure(shop)
    assert len(shop.subs) == 1 and shop.count("create") == 1
    proof = ensure(shop)   # the retry starts with a list: found, no second create
    assert not proof.created and shop.count("create") == 1 and len(shop.subs) == 1


def test_too_many_pages_fails_closed_without_creating(monkeypatch):
    monkeypatch.setattr(oauth, "WEBHOOK_PAGE_SIZE", 1)
    shop = GraphQLShop(page_size=1)
    for index in range(uninstall_webhook.MAX_PAGES + 1):
        shop.add(uri=f"https://unrelated-{index}.example.test/hooks")
    with pytest.raises(uninstall_webhook.RegistrationFailed) as info:
        ensure(shop)
    assert info.value.code == "too_many_subscriptions"
    assert shop.count("list") == uninstall_webhook.MAX_PAGES and shop.count("create") == 0


def test_guard_refusal_before_create_dispatch_propagates_and_creates_nothing():
    shop = GraphQLShop()

    class LeaseGone(Exception):
        pass

    calls = []

    async def guarded(call):
        calls.append(call)
        if len(calls) == 2:
            raise LeaseGone()
        return await call()

    with pytest.raises(LeaseGone):
        run(uninstall_webhook.ensure(guarded, api_for(shop), shop_domain=SHOP, access_token=TOKEN, uri=URI))
    assert shop.count("create") == 0 and shop.subs == []


def test_proof_detail_is_secret_free():
    proof = uninstall_webhook.WebhookProof("gid://shopify/WebhookSubscription/1", URI, True, 1)
    detail = proof.detail("2026-07")
    assert detail == {"subscription_id": "gid://shopify/WebhookSubscription/1", "topic": "APP_UNINSTALLED",
                      "uri": URI, "format": "JSON", "created": True, "exact_count": 1, "api_version": "2026-07"}
    assert "token" not in json.dumps(detail).lower()
