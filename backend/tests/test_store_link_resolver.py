"""
backend/tests/test_store_link_resolver.py
─────────────────────────────────────────
Regression suite for the store-URL resolver (May 2026 #31 — Tenant
33 production case).

Two production bugs collapsed into one symptom: customer asked
"رابط المتجر", got the awkward reply
"أبشر 🌷 تكفي لحظة وأجيب لك التفاصيل الكاملة بعد التأكد منه."

Root cause:
  * The store-link safety net fired (intent matched).
  * ``_lookup_tenant_store_url`` returned an empty string because
    it only checked ``TenantSettings.store_settings.store_url`` and
    ``Integration[provider='salla']``. Tenant 33 had neither
    populated.
  * The no-URL fallback was a FALSE PROMISE
    ("أرسل لك الرابط بعد التأكد منه") which then tripped the new
    asset-promise sanitizer, leaving the concatenated odd text.

Fix (this commit):
  1. ``_lookup_tenant_store_url`` now consults three sources in
     order: ``StoreKnowledgeSnapshot.store_profile.store_url`` →
     ``TenantSettings.store_settings.store_url`` → any
     ``Integration.config`` (Salla / Zid / Shopify / WooCommerce).
  2. The no-URL fallback no longer claims it will send a link —
     it asks a clarifying question instead.

These tests assert the source-of-truth chain and the
no-false-promise invariant.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ───────────────────────────────────────────────────────────────────


class _StubLoader:
    """Mimics ``core.store_knowledge.StoreKnowledgeLoader`` enough
    for the resolver's ``.store_profile()`` call."""
    def __init__(self, profile: Optional[Dict[str, Any]] = None) -> None:
        self._profile = profile or {}

    def store_profile(self) -> Dict[str, Any]:
        return dict(self._profile)


class _StubSettings:
    """Mimics ``TenantSettings`` with the two JSONB fields the
    resolver reads (``store_settings`` for the canonical store-tab
    URL, ``whatsapp_settings`` for the CTA-button URL slot added by
    May 2026 #35)."""
    def __init__(
        self,
        store_settings: Optional[Dict[str, Any]] = None,
        whatsapp_settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.store_settings = dict(store_settings or {})
        self.whatsapp_settings = dict(whatsapp_settings or {})


class _StubIntegration:
    """Mimics one ``Integration`` row."""
    def __init__(
        self, provider: str, config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.provider = provider
        self.config = dict(config or {})


class _StubQuery:
    """One-step ``.filter(...).first()`` chain for Integration queries."""
    def __init__(self, integrations: Dict[str, _StubIntegration]) -> None:
        # provider → integration row
        self._integrations = dict(integrations)
        self._pending_provider: Optional[str] = None

    def filter(self, *args: Any, **kwargs: Any) -> "_StubQuery":
        # We don't bother parsing the SQLAlchemy BinaryExpression. The
        # resolver always asks "tenant_id == X AND provider == Y" —
        # we peek at args[1]'s right side to find the provider.
        # Format: BinaryExpression has a ``right.value`` string.
        for expr in args:
            try:
                right = getattr(expr, "right", None)
                value = getattr(right, "value", None)
                if isinstance(value, str) and value in (
                    "salla", "zid", "shopify", "woocommerce",
                ):
                    self._pending_provider = value
                    break
            except Exception:
                continue
        return self

    def first(self) -> Optional[_StubIntegration]:
        provider = self._pending_provider
        self._pending_provider = None
        if not provider:
            return None
        return self._integrations.get(provider)


class _StubDB:
    """Tiny DB stub: only the queries the resolver makes."""
    def __init__(self, integrations: Optional[Dict[str, _StubIntegration]] = None) -> None:
        self._integrations = integrations or {}

    def query(self, _model: Any) -> _StubQuery:
        return _StubQuery(self._integrations)


def _install_resolver_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot_url: str = "",
    settings_url: str = "",
    whatsapp_button_url: str = "",
    integrations: Optional[Dict[str, Dict[str, Any]]] = None,
) -> _StubDB:
    """Inject the three source-of-truth modules via ``sys.modules``
    so the lazy imports inside
    :mod:`modules.ai.postprocess.safety_nets._lookup_tenant_store_url`
    pick up our stubs INSTEAD of the real modules.

    Done this way (rather than ``import core.tenant`` + ``setattr``)
    because the real ``core.tenant`` pulls in ``fastapi`` →
    ``starlette`` → stdlib ``secrets``, which collides with the
    ``backend/core/secrets.py`` module under pytest's collection
    path. Pre-populating ``sys.modules`` lets us short-circuit the
    whole chain without changing the resolver's production code.
    """
    import sys as _sys
    import types as _types

    integrations = integrations or {}

    # 1) Fake ``core.store_knowledge`` exposing ``StoreKnowledgeLoader``.
    sk_stub = _types.ModuleType("core.store_knowledge")
    def _fake_loader(_db: Any, _tid: int) -> _StubLoader:
        return _StubLoader({"store_url": snapshot_url} if snapshot_url else {})
    sk_stub.StoreKnowledgeLoader = _fake_loader  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "core.store_knowledge", sk_stub)

    # 2) Fake ``core.tenant`` exposing the four names the resolver uses
    # (DEFAULT_STORE / DEFAULT_WHATSAPP / get_or_create_settings /
    # merge_defaults). DEFAULT_WHATSAPP is read by the May 2026 #35
    # button-URL fallback layer.
    tenant_stub = _types.ModuleType("core.tenant")
    tenant_stub.DEFAULT_STORE = {"store_url": ""}  # type: ignore[attr-defined]
    tenant_stub.DEFAULT_WHATSAPP = {  # type: ignore[attr-defined]
        "store_button_url": "",
    }
    def _fake_settings(_db: Any, _tid: int) -> _StubSettings:
        return _StubSettings(
            store_settings=(
                {"store_url": settings_url} if settings_url else {}
            ),
            whatsapp_settings=(
                {"store_button_url": whatsapp_button_url}
                if whatsapp_button_url else {}
            ),
        )
    tenant_stub.get_or_create_settings = _fake_settings  # type: ignore[attr-defined]
    def _fake_merge_defaults(stored: Optional[Dict], defaults: Dict) -> Dict:
        out = dict(defaults or {})
        if stored:
            out.update({k: v for k, v in stored.items() if v is not None})
        return out
    tenant_stub.merge_defaults = _fake_merge_defaults  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "core.tenant", tenant_stub)

    # 3) Fake ``models`` exposing ``Integration`` with mockable columns.
    # The resolver calls ``db.query(Integration).filter(
    #     Integration.tenant_id == ..., Integration.provider == ...
    # )`` so we need ``Integration.tenant_id`` / ``.provider`` to be
    # descriptor-like objects that emit something ``_StubQuery.filter``
    # can introspect (we peek at ``.right.value`` to recover the
    # provider string).
    models_stub = _types.ModuleType("models")
    class _IntegrationColumn:  # noqa: D401
        def __init__(self, name: str) -> None:
            self.name = name
        def __eq__(self, other: Any) -> "_FakeBinaryExpr":  # type: ignore[override]
            return _FakeBinaryExpr(self.name, other)
    class _FakeBinaryExpr:  # noqa: D401
        def __init__(self, col_name: str, value: Any) -> None:
            self.col_name = col_name
            self.right = _types.SimpleNamespace(value=value)
    class _IntegrationStub:  # noqa: D401
        tenant_id = _IntegrationColumn("tenant_id")
        provider  = _IntegrationColumn("provider")
    models_stub.Integration = _IntegrationStub  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "models", models_stub)

    # 4) DB stub carrying the per-provider integration rows.
    integration_objs = {
        provider: _StubIntegration(provider, cfg)
        for provider, cfg in integrations.items()
    }
    return _StubDB(integration_objs)


# ── _lookup_tenant_store_url — source-of-truth chain ───────────────────────


def test_resolver_snapshot_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Source-of-truth #1: ``StoreKnowledgeSnapshot.store_profile.store_url``."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="https://snapshot.example.sa",
        settings_url="https://settings.example.sa",
        integrations={"salla": {"store_url": "https://salla.example.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://snapshot.example.sa"


def test_resolver_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Source #2: settings.store_settings used when snapshot is empty."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="https://settings.example.sa",
        integrations={"salla": {"store_url": "https://salla.example.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://settings.example.sa"


def test_resolver_falls_back_to_whatsapp_button_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source #3 (May 2026 #35): when snapshot and store_settings.store_url
    are both empty, the resolver consults
    ``whatsapp_settings.store_button_url`` BEFORE falling through to
    integration configs.

    Why this matters platform-wide: the dashboard exposes two URL
    slots — one in the "Store" tab (``store_settings.store_url``) and
    one in the "WhatsApp" tab as the CTA-button URL. Many merchants —
    especially Nahla-native shops without a Salla/Zid integration —
    fill ONLY the WhatsApp tab because that's the surface they
    interact with most. Before this fix the AI could not deliver
    "ابي رابط المتجر" for those tenants even though the URL was
    sitting one column over. This is NOT tenant-33-specific; any
    tenant with this configuration shape benefits.
    """
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        whatsapp_button_url="https://merchant.example.sa/ar",
        integrations={},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://merchant.example.sa/ar"


def test_resolver_prefers_store_settings_over_whatsapp_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order invariant: when BOTH ``store_settings.store_url`` AND
    ``whatsapp_settings.store_button_url`` are set, the canonical
    "Store" field wins. The button URL is a fallback, not an override.
    Tenants who already had a working setup must not see their
    resolved URL change just because the new layer was added."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="https://store-tab.example.sa",
        whatsapp_button_url="https://wa-tab.example.sa",
        integrations={},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://store-tab.example.sa"


def test_resolver_prefers_whatsapp_button_over_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order invariant (continued): when settings are empty but BOTH
    a button URL and a Salla integration domain exist, the manually-
    typed button URL wins. Manual entry beats auto-discovered
    integration metadata across the board."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        whatsapp_button_url="https://wa-tab.example.sa",
        integrations={"salla": {"store_url": "https://salla.example.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://wa-tab.example.sa"


def test_resolver_falls_back_to_salla_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source #4a: Salla integration config when prior sources empty."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        integrations={"salla": {"store_url": "https://salla.example.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://salla.example.sa"


def test_resolver_falls_back_to_zid_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source #4b: Zid integration — the original Salla-only
    resolver silently failed for Zid tenants."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        integrations={"zid": {"storefront_url": "https://zid.example.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://zid.example.sa"


def test_resolver_promotes_bare_domain_to_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration configs sometimes store a bare domain
    (``mystore.salla.sa`` with no scheme). The resolver must promote
    those to https:// before returning."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        integrations={"salla": {"domain": "mystore.salla.sa"}},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://mystore.salla.sa"


def test_resolver_returns_empty_when_all_sources_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tenant 33 production case: no source has a URL → return
    "" (the caller will then take the honest no-URL fallback path)."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="",
        settings_url="",
        integrations={},
    )
    assert _lookup_tenant_store_url(db, 33) == ""


def test_resolver_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing slashes break WhatsApp link previews on some clients
    — normalise them out before returning."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_store_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="https://example.sa/",
        settings_url="",
        integrations={},
    )
    assert _lookup_tenant_store_url(db, 33) == "https://example.sa"


# ── apply_store_link_safety_net — no-URL fallback must be honest ───────────


def test_no_url_fallback_does_not_promise_to_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old fallback ("أرسل لك الرابط بعد التأكد منه") was a
    false promise. The new fallback must not contain ANY of the
    asset-promise sanitizer's link-promise patterns — otherwise we
    re-create the production bug where the sanitizer scrubbed the
    safety net's own output."""
    from core.outbound_sanitizer import contains_promised_asset
    from modules.ai.postprocess.safety_nets import (
        apply_store_link_safety_net,
    )

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="", settings_url="", integrations={},
    )
    result = apply_store_link_safety_net(
        db, tenant_id=33,
        customer_msg="رابط المتجر",
        reply_text="",
    )
    assert result.fired is True
    assert result.rewrote_reply is True
    assert result.store_url == ""

    # CRITICAL: the new fallback message must NOT itself contain a
    # link/barcode/phone/location promise — otherwise the wire-layer
    # sanitizer will rewrite it again, producing the awkward
    # concatenated text the merchant flagged.
    assert contains_promised_asset(result.new_reply) is None, (
        f"no-URL fallback still contains a promise: {result.new_reply!r}"
    )

    # And the specific banned phrase from the bug report.
    assert "أرسل لك الرابط" not in result.new_reply
    assert "بعد التأكد منه" not in result.new_reply


def test_url_injected_when_snapshot_has_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: snapshot carries the URL → safety net injects it
    into the reply verbatim."""
    from modules.ai.postprocess.safety_nets import (
        apply_store_link_safety_net,
    )

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="https://mystore.example.sa",
        settings_url="", integrations={},
    )
    result = apply_store_link_safety_net(
        db, tenant_id=33,
        customer_msg="رابط المتجر",
        reply_text="",
    )
    assert result.fired is True
    assert result.rewrote_reply is True
    assert result.store_url == "https://mystore.example.sa"
    assert "https://mystore.example.sa" in result.new_reply


def test_safety_net_no_op_when_intent_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import (
        apply_store_link_safety_net,
    )

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="https://mystore.example.sa",
        settings_url="", integrations={},
    )
    result = apply_store_link_safety_net(
        db, tenant_id=33,
        customer_msg="السلام عليكم",  # greeting, no link request
        reply_text="حياك الله 🌷",
    )
    assert result.fired is False
    assert result.rewrote_reply is False


def test_safety_net_no_op_when_reply_already_has_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import (
        apply_store_link_safety_net,
    )

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_url="https://mystore.example.sa",
        settings_url="", integrations={},
    )
    result = apply_store_link_safety_net(
        db, tenant_id=33,
        customer_msg="رابط المتجر",
        reply_text="تفضل: https://mystore.example.sa 🌷",
    )
    assert result.fired is False
    assert result.skipped_reason == "url_already_in_reply"


# ── Sanitizer wording check (May 2026 #31 follow-up) ───────────────────────


def test_asset_promise_replacement_uses_clean_arabic() -> None:
    """The user flagged the original "تكفي لحظة وأجيب لك التفاصيل
    الكاملة 🌷" copy as awkward. The new copy must be short, natural,
    and not contain "تكفي لحظة" anywhere."""
    from core.outbound_sanitizer import _PROMISE_REPLACEMENTS, ASSET_LINK

    replacement = _PROMISE_REPLACEMENTS[ASSET_LINK]
    assert "تكفي لحظة" not in replacement
    assert "🌷" in replacement
    assert len(replacement) <= 60, (
        f"link replacement too long ({len(replacement)} chars): "
        f"{replacement!r}"
    )
