"""
tests/test_meta_catalog_discovery.py
────────────────────────────────────
Coverage for the catalog preflight discovery added May 2026 (#19g).

Production symptom that drove this work:
    Both ``GET /v21.0/{catalog_id}/items`` AND ``/products`` AND
    ``/product_items`` returned ``code=100 — nonexisting field`` for
    a merchant whose catalog_id was actually a Commerce Account ID,
    not a ProductCatalog ID.

Rather than continue trial-and-erroring through edge names, the
importer now calls the catalog OBJECT itself first to learn:

    * Is this a real catalog object Meta knows about?
    * What's its vertical (commerce / hotels / vehicles / …)?
    * Which edges does ``?metadata=1`` say it supports?

These tests pin the contract for that preflight + the edge-choice
helper, plus the structured failure paths.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────
# Fake httpx client — lets us drive the preflight without touching
# the network and assert exactly which URLs the importer hits.
# ──────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None,
                 text: Optional[str] = None):
        self.status_code = status_code
        self._payload = payload or {}
        if text is None:
            import json as _j
            self.text = _j.dumps(self._payload, ensure_ascii=False)
        else:
            self.text = text
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class _ScriptedClient:
    """Mimics ``httpx.Client(...)`` enough for the discovery hops.

    Constructed with a list of ``(url_substring, response)`` rules.
    The first rule whose ``url_substring`` is found in the requested
    URL is fired; rules are CONSUMED (one-shot) so ordered scripts
    are easy to express.
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def get(self, url, params=None):
        # Compose the full URL we'd actually hit so the matcher can
        # look at the ``?metadata=1`` etc. query the importer sent.
        full_url = url
        if params:
            try:
                full_url = str(httpx.URL(url, params=params))
            except Exception:
                full_url = url + "?" + "&".join(
                    f"{k}={v}" for k, v in (params or {}).items()
                )
        self.calls.append(full_url)
        for i, (needle, response) in enumerate(self._script):
            if needle in full_url:
                self._script.pop(i)
                return response
        raise AssertionError(
            f"_ScriptedClient got unexpected URL {full_url!r} "
            f"(remaining matchers: {[m[0] for m in self._script]!r})"
        )

    # Context-manager protocol so ``with httpx.Client(...) as c:`` works.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ══════════════════════════════════════════════════════════════════
# 1. _choose_item_edge — pure unit
# ══════════════════════════════════════════════════════════════════


class TestChooseItemEdge:
    def test_prefers_products_when_advertised(self):
        from services.meta_catalog_import import CatalogDiscovery, _choose_item_edge
        d = CatalogDiscovery(catalog_id="X",
                             supported_edges=["products", "feeds"])
        assert _choose_item_edge(d) == ("products", "product_items")

    def test_falls_back_to_product_items_for_legacy_catalogs(self):
        from services.meta_catalog_import import CatalogDiscovery, _choose_item_edge
        d = CatalogDiscovery(catalog_id="X",
                             supported_edges=["product_items", "feeds"])
        assert _choose_item_edge(d) == ("product_items", "items")

    def test_picks_items_for_transactable_items_catalogs(self):
        from services.meta_catalog_import import CatalogDiscovery, _choose_item_edge
        d = CatalogDiscovery(catalog_id="X",
                             supported_edges=["items", "feeds"])
        assert _choose_item_edge(d) == ("items", "product_items")

    def test_unknown_catalog_falls_back_to_legacy_defaults(self):
        from services.meta_catalog_import import (
            CatalogDiscovery, _choose_item_edge,
            META_CATALOG_EDGE_PRIMARY, META_CATALOG_EDGE_FALLBACK,
        )
        d = CatalogDiscovery(catalog_id="X", supported_edges=[])
        assert _choose_item_edge(d) == (
            META_CATALOG_EDGE_PRIMARY,
            META_CATALOG_EDGE_FALLBACK,
        )


# ══════════════════════════════════════════════════════════════════
# 2. _preflight_catalog_discovery — end-to-end with FakeClient
# ══════════════════════════════════════════════════════════════════


class TestPreflightDiscoveryHappyPath:
    def test_commerce_catalog_with_metadata(self, caplog):
        from services import meta_catalog_import as mci

        info_payload = {
            "id":            "1234567890",
            "name":          "Nahla Merchant Catalog",
            "vertical":      "commerce",
            "catalog_type":  "PRODUCTS",
            "product_count": 142,
            "feed_count":    1,
            "business":      {"id": "9988", "name": "Nahla BM"},
        }
        meta_payload = {
            "id":       "1234567890",
            "metadata": {
                "fields": [
                    {"name": "id"}, {"name": "name"},
                    {"name": "vertical"}, {"name": "product_count"},
                ],
                "connections": {
                    "products":       "https://graph.facebook.com/.../products",
                    "product_feeds":  "https://graph.facebook.com/.../product_feeds",
                    "product_sets":   "https://graph.facebook.com/.../product_sets",
                    "assigned_users": "https://graph.facebook.com/.../assigned_users",
                },
            },
        }
        client = _ScriptedClient([
            ("fields=", _FakeResponse(200, info_payload)),
            ("metadata=1", _FakeResponse(200, meta_payload)),
        ])

        with caplog.at_level("INFO"):
            d = mci._preflight_catalog_discovery(
                client,
                tenant_id=42,
                catalog_id="1234567890",
                token="MERCHANT_TOKEN",
            )

        assert d.ok is True
        assert d.http_status == 200
        assert d.name == "Nahla Merchant Catalog"
        assert d.vertical == "commerce"
        assert d.catalog_type == "PRODUCTS"
        assert d.product_count == 142
        assert d.feed_count == 1
        assert d.business_id == "9988"
        assert d.business_name == "Nahla BM"
        # The discovered edges include the real ``products`` edge
        # so the edge-choice helper will prefer it on the next leg.
        assert "products" in d.supported_edges
        assert "product_sets" in d.supported_edges
        assert len(d.supported_fields) >= 3

        # Structured logs both emitted.
        log_msgs = " ".join(rec.message for rec in caplog.records)
        assert "[META_IMPORT][CATALOG_INFO]" in log_msgs
        assert "[META_IMPORT][CATALOG_METADATA]" in log_msgs
        # The catalog_type / vertical / product_count must be logged.
        assert "vertical='commerce'" in log_msgs
        assert "product_count=142" in log_msgs

    def test_metadata_call_failing_does_not_break_discovery(self):
        """Hop 2 (``?metadata=1``) is best-effort — if Meta returns
        400 for it but the object itself was readable, discovery is
        still ok (the caller falls back to default edges)."""
        from services import meta_catalog_import as mci
        info_payload = {
            "id": "X", "name": "Y", "vertical": "commerce",
            "product_count": 3, "business": {"id": "B"},
        }
        client = _ScriptedClient([
            ("fields=", _FakeResponse(200, info_payload)),
            ("metadata=1", _FakeResponse(400, {
                "error": {"code": 100, "message": "metadata not supported"}
            })),
        ])
        d = mci._preflight_catalog_discovery(
            client, tenant_id=1, catalog_id="X", token="T",
        )
        assert d.ok is True
        assert d.vertical == "commerce"
        assert d.supported_edges == []  # we couldn't enumerate them


class TestPreflightDiscoveryHardFailures:
    def test_catalog_object_404_returns_not_ok_with_error_envelope(self):
        from services import meta_catalog_import as mci
        client = _ScriptedClient([
            ("fields=", _FakeResponse(404, {
                "error": {
                    "code":    803,
                    "message": "Some of the aliases you requested do not exist",
                    "type":    "OAuthException",
                    "fbtrace_id": "AbCdEf",
                },
            })),
        ])
        d = mci._preflight_catalog_discovery(
            client, tenant_id=1, catalog_id="BOGUS", token="T",
        )
        assert d.ok is False
        assert d.http_status == 404
        assert d.error.get("meta_code") == 803
        assert "do not exist" in d.error.get("meta_message", "").lower()
        assert d.error.get("fbtrace_id") == "AbCdEf"

    def test_transport_failure_returns_not_ok(self):
        from services import meta_catalog_import as mci

        class _Boom:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, params=None):
                raise httpx.ConnectError("simulated")

        d = mci._preflight_catalog_discovery(
            _Boom(), tenant_id=1, catalog_id="X", token="T",
        )
        assert d.ok is False
        assert d.error.get("stage") == "catalog_info_transport"
        assert d.error.get("exc_class") == "ConnectError"


# ══════════════════════════════════════════════════════════════════
# 3. import_from_meta — preflight branches end-to-end
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def _stub_db(monkeypatch):
    """A bare-bones DB stub with the WhatsAppConnection row the
    importer expects — provider=meta + a non-empty access_token so
    we never trip the token-selection error path."""

    class _Conn:
        meta_catalog_id = "123"
        provider        = "meta"
        connection_type = "embedded"
        access_token    = "MERCHANT_TOKEN"

    class _Query:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return _Conn()

    class _DB:
        def query(self, *a, **kw):
            return _Query()

        def commit(self):
            pass

        def rollback(self):
            pass

    return _DB()


def _patch_preflight(monkeypatch, *, discovery):
    """Monkey-patch the preflight function in the importer module so
    we don't need a real httpx round-trip."""
    from services import meta_catalog_import as mci

    def _fake(client, *, tenant_id, catalog_id, token):
        return discovery

    monkeypatch.setattr(mci, "_preflight_catalog_discovery", _fake)


def _patch_httpx_client(monkeypatch, scripted):
    """Replace the real ``httpx.Client`` used in the paging loop
    with our scripted fake so we can assert which edge URL the
    importer hit."""
    from services import meta_catalog_import as mci

    class _Factory:
        def __init__(self, *a, **kw):
            self._client = scripted

        def __enter__(self):
            return self._client

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mci.httpx, "Client", _Factory)


class TestImportRaisesOnCatalogNotFound:
    def test_404_from_preflight_raises_catalog_not_found(
        self, monkeypatch, _stub_db,
    ):
        from services.meta_catalog_import import (
            CatalogDiscovery, MetaCatalogImportError, import_from_meta,
        )

        bad = CatalogDiscovery(
            catalog_id="123",
            ok=False,
            http_status=404,
            error={
                "stage":      "catalog_info_http_error",
                "status":     404,
                "meta_code":  803,
                "meta_message": "Some of the aliases you requested do not exist",
            },
        )
        _patch_preflight(monkeypatch, discovery=bad)

        with pytest.raises(MetaCatalogImportError) as exc_info:
            import_from_meta(_stub_db, tenant_id=1)
        assert exc_info.value.code == "catalog_not_found"
        # Detail must carry the discovery payload so the dashboard
        # surfaces the Meta error verbatim.
        assert exc_info.value.detail["discovery"]["http_status"] == 404
        assert exc_info.value.detail["discovery"]["error"]["meta_code"] == 803


class TestImportRaisesOnUnsupportedVertical:
    def test_vehicle_catalog_is_refused(self, monkeypatch, _stub_db):
        from services.meta_catalog_import import (
            CatalogDiscovery, MetaCatalogImportError, import_from_meta,
        )
        veh = CatalogDiscovery(
            catalog_id="123", ok=True, http_status=200,
            vertical="vehicles", catalog_type="VEHICLES",
            product_count=10,
        )
        _patch_preflight(monkeypatch, discovery=veh)
        with pytest.raises(MetaCatalogImportError) as exc_info:
            import_from_meta(_stub_db, tenant_id=1)
        assert exc_info.value.code == "catalog_type_unsupported"
        assert "vehicles" in str(exc_info.value).lower()


class TestDiscoveryOnlyParser:
    """The kill-switch parser must accept the canonical Railway
    truthy strings AND ignore the noise that historically tripped
    other ``os.getenv`` consumers ("True ", "ON", " 1 ", etc.)."""

    @pytest.mark.parametrize("raw,expected", [
        ("true",      True),
        ("TRUE",      True),
        ("True",      True),
        ("  true  ",  True),
        ("1",         True),
        ("yes",       True),
        ("YES",       True),
        ("on",        True),
        ("ON",        True),
        ("false",     False),
        ("0",         False),
        ("no",        False),
        ("",          False),
        # Strings the dashboard might paste accidentally — must NOT
        # be treated as truthy.
        ("enabled",   False),  # we intentionally exclude this
        ("disabled",  False),
        ("y",         False),
        ("n",         False),
    ])
    def test_parser_matrix(self, monkeypatch, raw, expected):
        from services.meta_catalog_import import _discovery_only_enabled
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", raw)
        enabled, returned_raw = _discovery_only_enabled()
        assert enabled is expected
        assert returned_raw == raw

    def test_parser_returns_raw_empty_when_unset(self, monkeypatch):
        from services.meta_catalog_import import _discovery_only_enabled
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        enabled, raw = _discovery_only_enabled()
        assert enabled is False
        assert raw == ""


class TestImportDiscoveryOnly:
    def test_discovery_only_short_circuits_without_calling_items(
        self, monkeypatch, _stub_db,
    ):
        """Hard contract: when META_CATALOG_DISCOVERY_ONLY=true,
        NOT A SINGLE httpx GET past the preflight pair may fire —
        especially nothing on /products / /product_items / /items.
        The previous regression (May 2026 #19g) was that the
        kill-switch existed but a stale deploy never ran the
        check; this test pins the post-deploy behaviour."""
        from services.meta_catalog_import import (
            CatalogDiscovery, import_from_meta,
        )
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", "true")
        good = CatalogDiscovery(
            catalog_id="123", ok=True, http_status=200,
            name="A", vertical="commerce", catalog_type="PRODUCTS",
            product_count=5, supported_edges=["products"],
        )
        _patch_preflight(monkeypatch, discovery=good)

        # ANY httpx.Client.get call past preflight would blow up.
        class _ExplodingClient:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, params=None):  # pragma: no cover — guard
                raise AssertionError(
                    "discovery_only must short-circuit before any "
                    f"item-edge GET fires (got URL={url!r})"
                )

        from services import meta_catalog_import as mci

        class _Factory:
            def __init__(self, *a, **kw):
                self._c = _ExplodingClient()

            def __enter__(self):
                return self._c

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mci.httpx, "Client", _Factory)

        report = import_from_meta(_stub_db, tenant_id=1)
        assert report.discovery_only is True
        assert report.discovery is good
        assert report.edge_used == ""
        assert report.scanned == 0

    @pytest.mark.parametrize("truthy_value", ["true", "True", "1", "yes", "on"])
    def test_no_item_edge_url_is_ever_built_for_any_truthy_value(
        self, monkeypatch, _stub_db, truthy_value,
    ):
        """Stronger version of the test above: instead of an
        exploding fake we use a recording fake and ASSERT against
        the URL list at the end. Covers all Railway-style truthy
        spellings to prove the kill-switch isn't case- or
        spacing-sensitive in a way that bites operators."""
        from services.meta_catalog_import import (
            CatalogDiscovery, import_from_meta,
        )
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", truthy_value)
        good = CatalogDiscovery(
            catalog_id="2426534581035003",
            ok=True, http_status=200, name="X",
            vertical="commerce", catalog_type="PRODUCTS",
            supported_edges=["products", "product_items"],
        )
        _patch_preflight(monkeypatch, discovery=good)

        seen_urls: list = []

        class _RecordingClient:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, params=None):
                seen_urls.append(url)
                raise AssertionError(
                    f"NO httpx GET should fire past preflight when "
                    f"META_CATALOG_DISCOVERY_ONLY={truthy_value!r} — "
                    f"but got URL={url!r}"
                )

        from services import meta_catalog_import as mci

        class _Factory:
            def __init__(self, *a, **kw):
                self._c = _RecordingClient()

            def __enter__(self):
                return self._c

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mci.httpx, "Client", _Factory)

        report = import_from_meta(_stub_db, tenant_id=1)
        # Hard guarantee — no /products /product_items /items URL
        # was ever constructed.
        for u in seen_urls:
            assert "/products" not in u
            assert "/product_items" not in u
            assert "/items" not in u
        assert report.discovery_only is True
        assert report.edge_used == ""

    def test_env_log_emits_raw_and_parsed_for_every_call(
        self, monkeypatch, _stub_db, caplog,
    ):
        """[META_IMPORT][ENV] must fire RIGHT AFTER [START] on every
        single import_from_meta call so support can prove what the
        process actually saw in os.environ at the moment of the
        request — even for the un-set case."""
        from services.meta_catalog_import import (
            CatalogDiscovery, import_from_meta,
        )
        # Case 1: env unset → raw='' parsed=False
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        _patch_preflight(monkeypatch, discovery=CatalogDiscovery(
            catalog_id="123", ok=True, vertical="commerce",
            supported_edges=["products"],
        ))
        # Block the real http call (we just want the env log).
        from services import meta_catalog_import as mci

        class _Bypass:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, *a, **kw):
                # Return an "empty page → break" response.
                class R:
                    status_code = 200
                    text = '{"data": [], "paging": {}}'
                    content = text.encode("utf-8")

                    def json(self):
                        return {"data": [], "paging": {}}
                return R()

        class _Factory:
            def __init__(self, *a, **kw):
                self._c = _Bypass()

            def __enter__(self):
                return self._c

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mci.httpx, "Client", _Factory)

        with caplog.at_level("INFO"):
            import_from_meta(_stub_db, tenant_id=7)
        env_lines = [r for r in caplog.records
                     if "[META_IMPORT][ENV]" in r.message]
        assert len(env_lines) == 1
        assert "raw=''" in env_lines[0].message
        assert "parsed=False" in env_lines[0].message

        caplog.clear()

        # Case 2: env=true → raw='true' parsed=True
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", "true")
        with caplog.at_level("INFO"):
            import_from_meta(_stub_db, tenant_id=7)
        env_lines = [r for r in caplog.records
                     if "[META_IMPORT][ENV]" in r.message]
        assert len(env_lines) == 1
        assert "raw='true'" in env_lines[0].message
        assert "parsed=True" in env_lines[0].message

    def test_discovery_only_stop_log_is_warning_level(
        self, monkeypatch, _stub_db, caplog,
    ):
        """The [DISCOVERY_ONLY_STOP] line must be WARNING, not INFO,
        so it survives default Railway log filters. Spec from the
        May 2026 #19g hardening ticket."""
        from services.meta_catalog_import import (
            CatalogDiscovery, import_from_meta,
        )
        monkeypatch.setenv("META_CATALOG_DISCOVERY_ONLY", "true")
        _patch_preflight(monkeypatch, discovery=CatalogDiscovery(
            catalog_id="123", ok=True, vertical="commerce",
            supported_edges=["products", "product_items"],
        ))

        from services import meta_catalog_import as mci

        class _Factory:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                class _C:
                    def get(self, *a, **kw):
                        raise AssertionError("no GET allowed in discovery-only")
                return _C()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mci.httpx, "Client", _Factory)

        with caplog.at_level("WARNING"):
            import_from_meta(_stub_db, tenant_id=99)
        stop_lines = [r for r in caplog.records
                      if "[META_IMPORT][DISCOVERY_ONLY_STOP]" in r.message]
        assert len(stop_lines) == 1
        assert stop_lines[0].levelname == "WARNING"
        # The log must carry the edge_choice payload so operators
        # can sanity-check the routing without flipping the switch.
        assert "edge_choice=" in stop_lines[0].message
        assert "primary" in stop_lines[0].message


class TestImportUsesDiscoveredEdge:
    def test_products_edge_chosen_when_discovery_says_so(
        self, monkeypatch, _stub_db,
    ):
        """The paging loop must hit ``/{catalog_id}/products`` —
        NOT the hard-coded ``/items`` — when discovery says the
        ``products`` connection is supported."""
        from services.meta_catalog_import import (
            CatalogDiscovery, import_from_meta,
        )
        monkeypatch.delenv("META_CATALOG_DISCOVERY_ONLY", raising=False)
        good = CatalogDiscovery(
            catalog_id="123", ok=True, http_status=200,
            name="A", vertical="commerce", catalog_type="PRODUCTS",
            supported_edges=["products", "product_feeds"],
        )
        _patch_preflight(monkeypatch, discovery=good)

        page_payload = {
            "data": [
                {"id": "p1", "retailer_id": "SKU1", "name": "Item 1",
                 "price": "10.00 SAR"},
            ],
            "paging": {},
        }
        scripted = _ScriptedClient([
            ("/123/products", _FakeResponse(200, page_payload)),
        ])
        _patch_httpx_client(monkeypatch, scripted)

        report = import_from_meta(_stub_db, tenant_id=1)
        # We actually called the /products edge:
        assert any("/123/products" in c for c in scripted.calls)
        # ...and NOT the legacy /items or /product_items:
        assert not any("/123/items" in c for c in scripted.calls)
        assert not any("/123/product_items" in c for c in scripted.calls)
        assert report.edge_used == "products"
