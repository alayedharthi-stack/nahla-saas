"""
backend/tests/test_maps_link_resolver.py
─────────────────────────────────────────
Regression suite for the maps-URL resolver chain (May 2026 #36 —
platform-level location safety net).

Production bug we are pinning down:
  Customer asks "وين موقعكم؟" / "ابي رابط الموقع" and the bot
  replies with the e-commerce ``store_url`` instead of a Google
  Maps link, even though the merchant filled
  ``TenantSettings.store_settings.google_maps_location`` (or
  pasted a maps URL into the ``branches`` KB section) months ago.

Root cause:
  * No dedicated ``ask_location`` intent — phrasings collapsed
    onto ``ask_store_info`` which routed to the e-commerce
    ``faq_store_info`` template.
  * No maps URL resolver and no maps safety net.

Fix (this commit):
  1. New :func:`_lookup_tenant_maps_url` mirrors
     :func:`_lookup_tenant_store_url` with a snapshot →
     store_settings → KB-section chain. The KB layer scans
     ``branches`` / ``store_story`` / ``custom`` sections for
     the first URL whose host matches a known maps host.
  2. New :func:`apply_location_safety_net` injects the resolved
     maps URL into outbound replies for location intents and
     never invents a URL when none is configured.

These tests assert the source-of-truth ordering and the no-false-
promise invariant. They use the SAME test-stubs strategy as
``test_store_link_resolver.py`` so behaviour stays in lockstep.
"""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubLoader:
    def __init__(self, profile: Optional[Dict[str, Any]] = None) -> None:
        self._profile = profile or {}

    def store_profile(self) -> Dict[str, Any]:
        return dict(self._profile)


class _StubSettings:
    def __init__(
        self,
        store_settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.store_settings = dict(store_settings or {})


class _StubKBSection:
    """Mimics ``MerchantKnowledgeSection`` for the KB-fallback layer."""

    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        is_active: bool = True,
        priority: int = 100,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.is_active = is_active
        self.priority = priority
        self.updated_at = section_id  # monotonic stand-in


class _KBQuery:
    """Captures filter chain, returns the curated section list."""

    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_KBQuery":
        # The resolver always filters to the maps fallback kinds; we
        # honour that on the input list to keep ordering deterministic.
        for expr in args:
            kinds = getattr(expr, "_kinds", None)
            if kinds:
                self._sections = [
                    s for s in self._sections if s.kind in kinds
                ]
        return self

    def order_by(self, *_: Any) -> "_KBQuery":
        return self

    def limit(self, _n: int) -> "_KBQuery":
        return self

    def all(self) -> List[_StubKBSection]:
        return list(self._sections)


class _StubDB:
    def __init__(self, sections: Optional[List[_StubKBSection]] = None) -> None:
        self._sections = list(sections or [])

    def query(self, _model: Any) -> _KBQuery:
        return _KBQuery(self._sections)


def _install_resolver_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot_maps_url: str = "",
    settings_maps_url: str = "",
    kb_sections: Optional[List[_StubKBSection]] = None,
) -> _StubDB:
    """Inject ``core.store_knowledge`` / ``core.tenant`` / ``models``
    stubs so the lazy imports inside
    :func:`modules.ai.postprocess.safety_nets._lookup_tenant_maps_url`
    pick up our doubles instead of the real chain (which would pull
    fastapi → starlette → stdlib ``secrets`` → collide with
    ``backend/core/secrets.py`` under pytest's path).
    """
    # 1) core.store_knowledge with our profile.
    sk_stub = _types.ModuleType("core.store_knowledge")

    def _fake_loader(_db: Any, _tid: int) -> _StubLoader:
        return _StubLoader(
            {"maps_url": snapshot_maps_url} if snapshot_maps_url else {}
        )

    sk_stub.StoreKnowledgeLoader = _fake_loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.store_knowledge", sk_stub)

    # 2) core.tenant exposing DEFAULT_STORE / merge / settings getter.
    tenant_stub = _types.ModuleType("core.tenant")
    tenant_stub.DEFAULT_STORE = {  # type: ignore[attr-defined]
        "google_maps_location": "",
    }

    def _fake_settings(_db: Any, _tid: int) -> _StubSettings:
        return _StubSettings(
            store_settings=(
                {"google_maps_location": settings_maps_url}
                if settings_maps_url else {}
            ),
        )

    tenant_stub.get_or_create_settings = _fake_settings  # type: ignore[attr-defined]

    def _fake_merge_defaults(stored: Optional[Dict], defaults: Dict) -> Dict:
        out = dict(defaults or {})
        if stored:
            out.update({k: v for k, v in stored.items() if v is not None})
        return out

    tenant_stub.merge_defaults = _fake_merge_defaults  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.tenant", tenant_stub)

    # 3) models exposing MerchantKnowledgeSection.
    models_stub = _types.ModuleType("models")

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> _types.SimpleNamespace:  # type: ignore[override]
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def is_(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def in_(self, values: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(
                col_name=self.name, _kinds=tuple(values),
            )

        def asc(self) -> "_Col":
            return self

        def desc(self) -> "_Col":
            return self

    class _MksStub:
        tenant_id = _Col("tenant_id")
        kind = _Col("kind")
        is_active = _Col("is_active")
        deleted_at = _Col("deleted_at")
        priority = _Col("priority")
        updated_at = _Col("updated_at")

    models_stub.MerchantKnowledgeSection = _MksStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    return _StubDB(kb_sections or [])


# ── _lookup_tenant_maps_url — source-of-truth chain ─────────────────────────


def test_resolver_snapshot_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Source #1: ``StoreKnowledgeSnapshot.store_profile.maps_url``."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/snapshot",
        settings_maps_url="https://maps.app.goo.gl/settings",
        kb_sections=[
            _StubKBSection(
                section_id=1,
                kind="branches",
                body="Visit us at https://maps.app.goo.gl/kb",
            )
        ],
    )
    url, source = _lookup_tenant_maps_url(db, 33)
    assert url == "https://maps.app.goo.gl/snapshot"
    assert source == "snapshot"


def test_resolver_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Source #2: ``store_settings.google_maps_location`` when
    snapshot is empty."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="",
        settings_maps_url="https://maps.app.goo.gl/settings",
        kb_sections=[
            _StubKBSection(
                section_id=1,
                kind="branches",
                body="https://maps.app.goo.gl/kb",
            )
        ],
    )
    url, source = _lookup_tenant_maps_url(db, 33)
    assert url == "https://maps.app.goo.gl/settings"
    assert source == "store_settings"


def test_resolver_falls_back_to_kb_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source #3 (May 2026 #36): when snapshot and store_settings are
    both empty, the resolver scans free-form KB sections under
    ``branches`` / ``store_story`` / ``custom`` for the first URL
    whose host hints at a maps service.

    Why this matters: many merchants typed "موقعنا على الخرايط:
    https://maps.app.goo.gl/..." into the branches KB bucket during
    onboarding and never touched the structured field. Without
    this layer the AI treats those tenants as having "no maps URL"
    even though it's right there in the KB. Platform-wide; not
    Tenant-33-specific.
    """
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="",
        settings_maps_url="",
        kb_sections=[
            _StubKBSection(
                section_id=1,
                kind="branches",
                body=(
                    "فرعنا الرئيسي في الرياض - حي الورود.\n"
                    "موقعنا على الخرايط: https://maps.app.goo.gl/abc123"
                ),
            ),
        ],
    )
    url, source = _lookup_tenant_maps_url(db, 33)
    assert url == "https://maps.app.goo.gl/abc123"
    assert source == "kb:branches"


def test_resolver_kb_skips_non_maps_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a KB section with a Salla product link MUST NOT
    be promoted to the maps slot. The resolver requires the URL host
    to match :data:`_MAPS_HOST_HINTS`."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="",
        settings_maps_url="",
        kb_sections=[
            _StubKBSection(
                section_id=1,
                kind="branches",
                body="نحن في الرياض. تفضل: https://salla.sa/store/abc",
            ),
        ],
    )
    url, source = _lookup_tenant_maps_url(db, 33)
    assert url == ""
    assert source == "none"


def test_resolver_kb_recognises_diverse_maps_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host-hint allowlist covers Google, Apple, Waze, and the
    common short-link variants. Pick one of each shape and confirm
    each is recognised."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    cases = [
        "https://maps.google.com/?cid=12345",
        "https://www.google.com/maps/place/Riyadh",
        "https://goo.gl/maps/abcdef",
        "https://maps.apple.com/?ll=24.7,46.7",
        "https://waze.com/ul?ll=24.7,46.7",
    ]
    for expected in cases:
        db = _install_resolver_stubs(
            monkeypatch,
            snapshot_maps_url="",
            settings_maps_url="",
            kb_sections=[
                _StubKBSection(
                    section_id=1,
                    kind="branches",
                    body=f"عنواننا: {expected}",
                ),
            ],
        )
        url, source = _lookup_tenant_maps_url(db, 33)
        assert url == expected, f"failed to recognise {expected}"
        assert source == "kb:branches"


def test_resolver_returns_empty_when_all_sources_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No source has a maps URL → return ("", "none"). The caller
    then takes the honest no-URL fallback path instead of inventing
    one (or, worse, swapping in the e-commerce ``store_url``)."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="",
        settings_maps_url="",
        kb_sections=[],
    )
    url, source = _lookup_tenant_maps_url(db, 33)
    assert url == ""
    assert source == "none"


def test_resolver_handles_db_none() -> None:
    """Defensive: never crash when the caller forgot to pass a DB
    handle. We see this rarely in production from one-off tasks."""
    from modules.ai.postprocess.safety_nets import _lookup_tenant_maps_url

    url, source = _lookup_tenant_maps_url(None, 33)
    assert url == ""
    assert source == "none"


# ── apply_location_safety_net — behaviour matrix ────────────────────────────


def test_safety_net_injects_maps_url_for_arabic_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: customer asks "وين موقعكم؟", LLM shipped a stub,
    safety net appends the canonical maps URL line."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/abc",
        settings_maps_url="",
        kb_sections=[],
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="وين موقعكم؟",
        reply_text="هذا موقعنا 🌷",
    )
    assert result.fired is True
    assert result.rewrote_reply is True
    assert result.maps_url == "https://maps.app.goo.gl/abc"
    assert result.source == "snapshot"
    assert "https://maps.app.goo.gl/abc" in result.new_reply


def test_safety_net_appends_when_reply_has_other_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM already wrote a longer body answering something
    else, we APPEND the maps URL on a new line instead of replacing
    the whole reply — same behaviour as the store-link net."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/xyz",
        settings_maps_url="",
        kb_sections=[],
    )
    long_reply = (
        "أبشر 🌷 سعر الكيلو ٢٢٠ ريال، ومتوفر شحن مبرد للخليج."
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="ابي رابط الموقع",
        reply_text=long_reply,
    )
    assert result.fired is True
    assert long_reply.rstrip() in result.new_reply
    assert "https://maps.app.goo.gl/xyz" in result.new_reply


def test_safety_net_no_op_when_intent_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic greeting must NOT trigger the maps net."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/xyz",
        settings_maps_url="",
        kb_sections=[],
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="السلام عليكم",
        reply_text="حياك الله 🌷",
    )
    assert result.fired is False
    assert result.skipped_reason == "no_location_intent"


def test_safety_net_no_op_when_maps_url_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM somehow already shipped a Google Maps URL, we
    must NOT re-append the same one — that would look spammy."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/xyz",
        settings_maps_url="",
        kb_sections=[],
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="وين موقعكم",
        reply_text="موقعنا 📍\nhttps://maps.app.goo.gl/xyz",
    )
    assert result.fired is False
    assert result.skipped_reason == "maps_url_already_in_reply"


def test_safety_net_fires_when_only_store_url_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical regression check for the production bug: a reply
    that contains the e-commerce ``store_url`` must NOT block the
    maps safety net. We require a *maps host hint* in the existing
    URL before skipping — the whole point of this net is to fix
    "asked for location → got store URL" replies."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/xyz",
        settings_maps_url="",
        kb_sections=[],
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="ابي رابط الموقع",
        reply_text="هذا متجرنا 🌷\nhttps://aledstore.com",
    )
    assert result.fired is True, (
        "safety net should still fire even when the LLM shipped "
        "the e-commerce store URL — that URL is the bug, not the fix."
    )
    assert result.maps_url == "https://maps.app.goo.gl/xyz"
    assert "https://maps.app.goo.gl/xyz" in result.new_reply


def test_safety_net_no_url_fallback_is_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no maps URL is configured anywhere, the fallback message
    must NOT contain a link/barcode/phone/location promise — otherwise
    the wire-layer ``maybe_scrub_unkept_asset_promise`` will rewrite
    it again, producing the exact awkward concatenation the
    store-link safety net learned to avoid."""
    from core.outbound_sanitizer import contains_promised_asset
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="",
        settings_maps_url="",
        kb_sections=[],
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="ابي رابط اللوكيشن",
        reply_text="",
    )
    assert result.fired is True
    assert result.rewrote_reply is True
    assert result.maps_url == ""
    assert result.source == "none"
    assert contains_promised_asset(result.new_reply) is None, (
        f"no-URL maps fallback still contains a promise: "
        f"{result.new_reply!r}"
    )


# ── Trigger phrasing — disjoint from store-link triggers ────────────────────


def test_trigger_sets_are_disjoint() -> None:
    """The store-link and location trigger sets MUST be disjoint —
    otherwise both nets fire on the same inbound and we'd ship two
    URLs for one question."""
    from modules.ai.postprocess.safety_nets import (
        _LOCATION_LINK_TRIGGERS_PHRASE,
        _STORE_LINK_TRIGGERS_PHRASE,
    )

    overlap = _LOCATION_LINK_TRIGGERS_PHRASE & _STORE_LINK_TRIGGERS_PHRASE
    assert not overlap, (
        f"location and store-link triggers overlap: {sorted(overlap)!r}"
    )


def test_classic_location_phrasings_match() -> None:
    """The exact phrasings reported in the Tenant 33 production
    feedback (May 2026 #36) MUST classify as location intent."""
    from modules.ai.postprocess.safety_nets import _looks_like_location_request

    for msg in (
        "وين موقعكم",
        "وين موقعكم؟",
        "موقع المتجر",
        "موقع المعرض",
        "ابي رابط الموقع",
        "أبي رابط الموقع",
        "ابغى اللوكيشن",
        "وين فرعكم",
        "أين فرعكم؟",
        "google maps please",
        "your address?",
    ):
        assert _looks_like_location_request(msg), (
            f"location intent should match: {msg!r}"
        )


def test_store_link_phrasings_dont_match_location() -> None:
    """An online-store ask MUST NOT trip the location net."""
    from modules.ai.postprocess.safety_nets import _looks_like_location_request

    for msg in (
        "ابي رابط المتجر",
        "أبي رابط متجركم",
        "store link please",
    ):
        assert not _looks_like_location_request(msg), (
            f"location net should not match: {msg!r}"
        )


# ── Redundant prose REPLACE behaviour (May 2026 #38) ─────────────────────────


def test_bare_intro_detector_flags_ask_for_branch_prose() -> None:
    """The exact prose the LLM emitted before May 2026 #38 — a
    multi-line "we'll send the location, tell us the branch" stub
    — MUST be classified as bare-intro-shaped so the safety net
    REPLACES it instead of appending a second message containing
    the maps URL. Without this, customers saw both messages on
    the wire ("أخبرنا بالفرع" + "موقعنا 📍 ..."), contradicting
    each other."""
    from modules.ai.postprocess.safety_nets import _looks_like_bare_location_intro

    awkward_prose_replies = (
        "حياك الله 🌷 لنبعث لك موقعنا على الخرايط\nعطنا اسم الفرع أو المدينة وأبشر.",
        "أخبرنا بنوع الاستفسار أو الفرع لنبعث لك موقعنا على الخرايط",
        "أعطنا اسم الفرع أو المدينة 🌷",
        "خبرنا بالفرع لنرسل لك الموقع",
        "أخبرنا أي فرع تبحث عنه",
    )
    for reply in awkward_prose_replies:
        assert _looks_like_bare_location_intro(reply), (
            f"redundant prose should be flagged for replacement: {reply!r}"
        )


def test_substantive_reply_without_redundant_prose_is_not_bare() -> None:
    """A real product/service answer (no "send branch" prose,
    no maps URL) must NOT be flagged as bare-intro — we'd
    overwrite the merchant's actual reply with our canonical
    location line."""
    from modules.ai.postprocess.safety_nets import _looks_like_bare_location_intro

    real_replies = (
        "متوفر شحن مبرد للخليج، السعر ٢٢٠ ريال للكيلو 🌷",
        "نوفر العسل الجبلي والسدر بأسعار مختلفة حسب الكمية المطلوبة.",
    )
    for reply in real_replies:
        assert not _looks_like_bare_location_intro(reply), (
            f"substantive reply must NOT be flagged: {reply!r}"
        )


def test_safety_net_replaces_redundant_prose_when_injecting_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the LLM ships the awkward "ask for branch"
    fallback (because facts.maps_url was empty), the safety net
    resolves a maps URL, and the FINAL reply must be the clean
    canonical "موقعنا 📍\n<url>" — NOT the awkward prose plus
    the URL on a new line."""
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    db = _install_resolver_stubs(
        monkeypatch,
        snapshot_maps_url="https://maps.app.goo.gl/aledqr",
        settings_maps_url="",
        kb_sections=[],
    )
    awkward = (
        "حياك الله 🌷 لنبعث لك موقعنا على الخرايط\n"
        "عطنا اسم الفرع أو المدينة وأبشر."
    )
    result = apply_location_safety_net(
        db, tenant_id=33,
        customer_msg="وين موقعكم",
        reply_text=awkward,
    )
    assert result.fired is True
    assert result.rewrote_reply is True
    # Customer must NOT see both the redundant prose AND the URL.
    assert "لنبعث لك" not in result.new_reply
    assert "عطنا اسم الفرع" not in result.new_reply
    assert "https://maps.app.goo.gl/aledqr" in result.new_reply


def test_build_location_reply_includes_branch_details() -> None:
    from modules.ai.postprocess.safety_nets import _build_location_reply

    text = _build_location_reply(
        "https://maps.app.goo.gl/branch",
        store_name="معرض آل عايد",
        branch_name="معرض آل عايد للعسل البلدي",
        city="الطائف",
        district="الحلقة الغربية",
        address="شارع العدل الواعظ، حي الحلقة الغربية",
        has_branch_details=True,
    )
    assert "📍 معرض آل عايد للعسل البلدي" in text
    assert "الفرع:" in text
    assert "الطائف" in text
    assert "العنوان:" in text
    assert "اضغط الزر لفتح الموقع في خرائط Google." in text
    assert "https://maps.app.goo.gl/branch" in text


def test_build_location_reply_maps_only_fallback() -> None:
    from modules.ai.postprocess.safety_nets import _build_location_reply

    text = _build_location_reply(
        "https://maps.app.goo.gl/only",
        has_branch_details=False,
    )
    assert "📍 موقعنا على الخريطة" in text
    assert "اضغط الزر لفتح الموقع." in text
    assert "https://maps.app.goo.gl/only" in text
