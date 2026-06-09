"""
tests/test_store_link_safety_net.py
───────────────────────────────────
Coverage for the store-link safety net introduced May 2026 after
the production complaint where the bot replied "هذا متجرنا 🌷"
without including the actual store URL.

The safety net lives in ``modules.ai.postprocess.safety_nets``
(``apply_store_link_safety_net``) and runs in the webhook after the
other post-LLM nets. It is intentionally narrow: it ONLY fires when
the customer's message is clearly a store-link request AND the
reply does not already contain a URL.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _patch_store_url(monkeypatch, url):
    """Patch the tenant store-url resolver so the safety net sees
    the URL we want without hitting a real DB."""
    from modules.ai.postprocess import safety_nets
    monkeypatch.setattr(
        safety_nets, "_lookup_tenant_store_url",
        lambda db, tenant_id: url,
    )


# ──────────────────────────────────────────────────────────────────
# 1. Intent detection (pure)
# ──────────────────────────────────────────────────────────────────


class TestStoreLinkIntent:
    @pytest.mark.parametrize("msg", [
        "رابط المتجر",
        "وين رابط المتجر",
        "ارسل الرابط",
        "أرسل الرابط",
        "ابعث اللينك",
        "أبي رابط المتجر؟",
        "المتجر الإلكتروني",
        "store link",
        "send the link",
        "your website",
    ])
    def test_recognises_store_link_request(self, msg):
        from modules.ai.postprocess.safety_nets import (
            _looks_like_store_link_request,
        )
        assert _looks_like_store_link_request(msg) is True, msg

    @pytest.mark.parametrize("msg", [
        "",
        "السلام عليكم",
        "أبي عسل سدر",
        "في توصيل لجدة؟",
        "كم سعر العسل؟",
        "متى يفتح المتجر؟",   # mentions "متجر" but no link request
    ])
    def test_ignores_unrelated_messages(self, msg):
        from modules.ai.postprocess.safety_nets import (
            _looks_like_store_link_request,
        )
        assert _looks_like_store_link_request(msg) is False, msg

    @pytest.mark.parametrize("msg", [
        "موقعكم",
        "رابط الموقع",
    ])
    def test_location_phrases_not_store_link_intent(self, msg):
        """May 2026 #36: bare موقع / رابط الموقع → location safety net."""
        from modules.ai.postprocess.safety_nets import (
            _looks_like_location_request,
            _looks_like_store_link_request,
        )
        assert _looks_like_store_link_request(msg) is False, msg
        assert _looks_like_location_request(msg) is True, msg


# ──────────────────────────────────────────────────────────────────
# 2. URL-already-present short-circuit
# ──────────────────────────────────────────────────────────────────


class TestUrlAlreadyInReply:
    def test_skips_when_reply_already_has_https_url(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://aaied.store")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="تفضل رابط متجرنا 🌷\nhttps://aaied.store",
        )
        assert res.fired is False
        assert res.skipped_reason == "url_already_in_reply"

    def test_skips_when_reply_has_bare_domain(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://aaied.store")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="موقعنا: aaied.store 🌷",
        )
        assert res.fired is False
        assert res.skipped_reason == "url_already_in_reply"


# ──────────────────────────────────────────────────────────────────
# 3. URL injection — the production bug fix
# ──────────────────────────────────────────────────────────────────


class TestUrlInjection:
    def test_replaces_bare_generic_intro_with_canonical_reply(self, monkeypatch):
        """The exact production bug from the screenshot — customer
        sends "رابط المتجر", LLM replies "هذا متجرنا 🌷". The
        safety net must replace it with the full canonical reply
        that includes the actual store URL."""
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://aaied.store")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="هذا متجرنا 🌷",
        )
        assert res.fired is True
        assert res.rewrote_reply is True
        # Required: the actual store URL must appear in the new reply.
        assert "https://aaied.store" in res.new_reply
        # Required: must NOT remain only the generic "هذا متجرنا".
        assert res.new_reply.strip() != "هذا متجرنا 🌷"
        # Required: canonical opener.
        assert "تفضل رابط متجرنا" in res.new_reply

    def test_appends_url_when_reply_has_other_content(self, monkeypatch):
        """If the LLM gave a longer contextual reply (answered
        another question first) we APPEND the link rather than
        wiping the content."""
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://aaied.store")
        long_reply = (
            "ياهلا 🌷 عندنا حاليًا عرض سمر الحجاز إنتاج 1446 بأسعار "
            "تصفية ممتازة. الرابط موجود لكن أرسله لك الحين."
        )
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="ارسل الرابط",
            reply_text=long_reply,
        )
        assert res.fired is True
        # The new reply must keep the original content AND add the link.
        assert "سمر الحجاز" in res.new_reply
        assert "https://aaied.store" in res.new_reply

    def test_handles_url_without_scheme_from_settings(self, monkeypatch):
        """If the URL configured in TenantSettings is a bare domain,
        the integration lookup promotes it to https:// — verify the
        injected URL is well-formed."""
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://example.sa")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="هذا متجرنا 🌷",
        )
        assert res.fired is True
        assert "https://example.sa" in res.new_reply


# ──────────────────────────────────────────────────────────────────
# 4. No-URL-on-file fallback
# ──────────────────────────────────────────────────────────────────


class TestNoUrlConfigured:
    def test_returns_polite_fallback_when_no_url_on_file(self, monkeypatch):
        """When the tenant has no store_url, the safety net must
        NOT hallucinate a URL AND must NOT make a false promise to
        send one later. The previous fallback ("أرسل لك الرابط بعد
        التأكد منه") was itself a broken promise — Tenant 33
        production case (May 2026 #31). The new fallback asks a
        clarifying question instead."""
        from core.outbound_sanitizer import contains_promised_asset
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="هذا متجرنا 🌷",
        )
        assert res.fired is True
        assert res.rewrote_reply is True

        # Must NOT contain a fake URL.
        assert "http" not in res.new_reply
        assert ".com" not in res.new_reply
        assert ".sa" not in res.new_reply

        # Must NOT contain the old broken-promise phrasing.
        assert "أرسل لك الرابط" not in res.new_reply
        assert "بعد التأكد منه" not in res.new_reply

        # Critical invariant: the fallback must NOT itself contain
        # any link/barcode/phone/location promise — otherwise the
        # wire-layer ``maybe_scrub_unkept_asset_promise`` will
        # rewrite it AGAIN, reproducing the production bug where
        # the customer saw two stitched-together neutral phrases.
        assert contains_promised_asset(res.new_reply) is None, (
            f"no-URL fallback still contains a promise: {res.new_reply!r}"
        )


# ──────────────────────────────────────────────────────────────────
# 5. Feature flag kill switch
# ──────────────────────────────────────────────────────────────────


class TestFeatureFlag:
    def test_flag_disabled_skips(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        monkeypatch.setenv("STORE_LINK_SAFETY_NET_ENABLED", "off")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="هذا متجرنا 🌷",
        )
        assert res.fired is False
        assert res.skipped_reason == "flag_disabled"


# ──────────────────────────────────────────────────────────────────
# 6. Unrelated turns must remain untouched
# ──────────────────────────────────────────────────────────────────


class TestUnrelatedTurns:
    def test_skips_when_customer_not_asking_for_link(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import (
            apply_store_link_safety_net,
        )
        _patch_store_url(monkeypatch, "https://aaied.store")
        res = apply_store_link_safety_net(
            MagicMock(), tenant_id=11,
            customer_msg="أبي عسل السدر",
            reply_text="عندنا سدر بسعر 358 ر.س 🌷",
        )
        assert res.fired is False
        assert res.skipped_reason == "no_store_link_intent"
