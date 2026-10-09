"""Catalog version isolation with in-process HTTP only; no provider credentials."""
from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from core import config
from core.meta_catalog_graph import (
    CATALOG_GRAPH_VERSION_ENV,
    CatalogGraphVersionError,
    catalog_graph_api_version,
)
from services import catalog_durable_images as images
from services import meta_catalog_access as access
from services import meta_catalog_consent as consent
from services import meta_catalog_import as importer
from services import meta_catalog_linking as linking
from services import meta_catalog_push as push
from services import meta_catalog_reconcile as reconcile


@pytest.fixture(params=[None, "", "v26.0"])
def version(request, monkeypatch):
    monkeypatch.setattr(config, "META_GRAPH_API_VERSION", "v21.0")
    monkeypatch.setattr(linking, "META_GRAPH_API_VERSION", "v21.0")
    if request.param is None:
        monkeypatch.delenv(CATALOG_GRAPH_VERSION_ENV, raising=False)
    else:
        monkeypatch.setenv(CATALOG_GRAPH_VERSION_ENV, request.param)
    return request.param or "v21.0"


def test_selector_preserves_shared_configuration(version):
    assert catalog_graph_api_version() == version
    assert config.META_GRAPH_API_VERSION == "v21.0"
    assert linking.META_GRAPH_API_VERSION == "v21.0"
    assert consent._graph_version() == version
    assert push._graph_base("CAT", "products") == f"https://graph.facebook.com/{version}/CAT/products"
    assert push._graph_product_url("ITEM") == f"https://graph.facebook.com/{version}/ITEM"


@pytest.mark.parametrize("invalid", ["26.0", "v26", "v0.0", "v26.0/products", "v26.0?x=1", "v26.0#x", "v26.0\nX"])
def test_malformed_override_never_falls_back_or_sends(invalid, monkeypatch):
    monkeypatch.setenv(CATALOG_GRAPH_VERSION_ENV, invalid)
    client = MagicMock()
    with pytest.raises(CatalogGraphVersionError, match="catalog_graph_version_invalid"):
        access.probe_catalog_readable(secrets.token_hex(12), "CAT", client=client)
    client.get.assert_not_called()
    with pytest.raises(CatalogGraphVersionError):
        push._graph_product_url("ITEM")
    with pytest.raises(CatalogGraphVersionError):
        consent._graph_version()


@pytest.mark.parametrize("path", [
    "oauth/access_token", "debug_token", "me", "me/permissions", "CAT",
    "BUSINESS/owned_product_catalogs", "me/business_users",
])
def test_every_consent_http_endpoint_uses_selector(version, monkeypatch, path):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(consent.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    status, _ = asyncio.run(consent._graph_get(path, {}, token=secrets.token_hex(12)))
    assert status == 200
    assert [str(request.url) for request in seen] == [f"https://graph.facebook.com/{version}/{path}"]


def test_catalog_access_and_reconciliation_use_selector(version, monkeypatch):
    seen = []
    token = secrets.token_hex(12)

    def respond(request):
        seen.append(request)
        body = {"data": []} if request.url.path.endswith("/products") else {"id": "CAT"}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(reconcile, "select_catalog_graph_token", lambda *args: {"token": token})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert access.probe_catalog_readable(token, "CAT", client=client)["ok"]
        _, report = reconcile.fetch_meta_catalog_live_products(None, "CAT", client=client)
    assert report["complete"]
    assert [request.url.path for request in seen] == [f"/{version}/CAT", f"/{version}/CAT/products"]


def test_import_discovery_and_edge_probe_use_selector(version):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "CAT", "data": [], "metadata": {"connections": {}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = importer._preflight_catalog_discovery(
            client, tenant_id=7, catalog_id="CAT", token=secrets.token_hex(12),
        )
        assert result.ok
        assert importer._probe_products_page(client, catalog_id="CAT", token=secrets.token_hex(12))["ok"]
    assert [request.url.path for request in seen] == [f"/{version}/CAT", f"/{version}/CAT", f"/{version}/CAT/products"]


def test_catalog_image_refresh_uses_selector(version, monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "ITEM", "image_url": "https://cdn.example.test/item.jpg"})

    original = httpx.Client
    monkeypatch.setattr(images.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    product = SimpleNamespace(id=1, tenant_id=7, meta_item_id="ITEM", extra_metadata={})
    assert images.fetch_live_graph_image_url(None, product, token_cache={7: secrets.token_hex(12)})
    assert [request.url.path for request in seen] == [f"/{version}/ITEM"]


def test_mixed_linking_keeps_waba_calls_on_shared_version(version):
    seen = []
    token = secrets.token_hex(12)

    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "CAT", "data": [], "owner_business_info": {"id": "BUSINESS"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert linking._probe_catalog_exists("CAT", token, client=client)
        linking.list_catalog_agency_business_ids("CAT", token, client=client)
        linking.share_catalog_with_business("CAT", "BUSINESS", token, confirm=True, client=client)
        linking._fetch_waba_product_catalogs("WABA", token, client=client)
        linking.fetch_waba_owner_business_id("WABA", token, client=client)
        linking.link_waba_to_catalog("WABA", "CAT", token, confirm=True, client=client)
    assert seen == [
        ("GET", f"/{version}/CAT"),
        ("GET", f"/{version}/CAT/agencies"),
        ("GET", f"/{version}/CAT/agencies"),
        ("POST", f"/{version}/CAT/agencies"),
        ("GET", "/v21.0/WABA/product_catalogs"),
        ("GET", "/v21.0/WABA"),
        ("GET", "/v21.0/WABA/product_catalogs"),
        ("POST", "/v21.0/WABA/product_catalogs"),
        ("GET", "/v21.0/WABA/product_catalogs"),
    ]


def test_onboarding_owned_catalogs_opt_in_but_permissions_stay_shared(version):
    from services import meta_catalog_onboarding as onboarding

    seen = []
    token = secrets.token_hex(12)

    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "CAT", "data": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        onboarding._list_owned_catalog_ids("BUSINESS", token, client=client)
        onboarding._create_owned_catalog("BUSINESS", token, "Generic catalog", client=client)
        onboarding._catalog_management_granted(token, client=client)
    assert seen == [
        ("GET", f"/{version}/BUSINESS/owned_product_catalogs"),
        ("POST", f"/{version}/BUSINESS/owned_product_catalogs"),
        ("GET", "/v21.0/me/permissions"),
    ]


def test_override_does_not_change_embedded_signup_or_template_graph(monkeypatch):
    from routers import whatsapp_embedded
    from core.commerce_lifecycle import order_confirmation_meta_header

    embedded_graph = whatsapp_embedded.GRAPH
    template_graph = order_confirmation_meta_header.GRAPH
    shared_version = config.META_GRAPH_API_VERSION
    monkeypatch.setenv(CATALOG_GRAPH_VERSION_ENV, "v26.0")
    assert catalog_graph_api_version() == "v26.0"
    assert config.META_GRAPH_API_VERSION == shared_version
    assert whatsapp_embedded.GRAPH == embedded_graph
    assert order_confirmation_meta_header.GRAPH == template_graph
