"""
tests/test_outbound_search_leak_guard.py
────────────────────────────────────────
Locks the wire-layer search-leak guard (``core.outbound_sanitizer``,
May 2026 incident) on both of its edges:

1.  Every external-research FINGERPRINT still replaces the reply with
    ``SAFE_FALLBACK_TEXT`` and strips interactive buttons. Those
    fingerprints are evidence of a search dump; nothing here weakens
    them.
2.  The number of links a reply carries is not evidence of a leak
    when the platform recognises the links. September 2026, Tenant 1
    Commerce Runtime pilot: the model composed a correct four-product
    listing with four links on the merchant's own Salla store and the
    ``too_many_urls`` rule replaced the whole reply with the apology.
    Links on the merchant's own store domain, on a storefront platform
    Nahla integrates, and payment / tracking / product / map links by
    the CTA taxonomy never count. Three or more links the platform
    cannot place, with no fingerprint, still trip the rule.

Invariants are asserted as behaviour (marker name, replaced-or-not,
buttons kept-or-stripped, audit line present-or-absent) — never as an
exact Arabic sentence, except that a clean reply must pass through
byte-for-byte unchanged.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


SANITIZER_LOGGER = "nahla.security.outbound_sanitizer"


def _text_payload(body: str, to: str = "+966500000001") -> dict:
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}


def _interactive_payload(body: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": "+966500000001",
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body},
            "action": {"name": "cta_url",
                       "parameters": {"display_text": "عرض المنتج",
                                      "url": "https://shop.example-store.com/products/1"}},
        },
    }


# ── The real observed case and its generic siblings ──────────────────────────

# Turn 5 of the first real Tenant 1 conversation: four dresses, four links on
# the merchant's own Salla store. The wording is the model's; the shape is
# what matters.
OBSERVED_FOUR_PRODUCT_LISTING = (
    "يا تركي، عندنا فساتين حلوة متوفرة! 👗✨\n\n"
    "1️⃣ **فستان** - 289 ريال سعودي\n📦 متوفر (6 قطع)\n🔗 https://demostore.salla.sa/ar/p1\n\n"
    "2️⃣ **فستان صيفي** - 199 ريال سعودي\n📦 متوفر (3 قطع)\n🔗 https://demostore.salla.sa/ar/p2\n\n"
    "3️⃣ **فستان سهرة** - 450 ريال سعودي\n📦 متوفر (2 قطع)\n🔗 https://demostore.salla.sa/ar/p3\n\n"
    "4️⃣ **فستان كاجوال** - 150 ريال سعودي\n📦 متوفر (9 قطع)\n🔗 https://demostore.salla.sa/ar/p4\n\n"
    "أي واحد يعجبك؟ 😊"
)

# A generic merchant on a custom domain: shoes, a shirt and a perfume.
GENERIC_CUSTOM_DOMAIN_LISTING = (
    "أهلاً أحمد، هذي المنتجات المتوفرة في متجر تجريبي عام:\n\n"
    "• حذاء رياضي أبيض — 199 ريال\nhttps://shop.example-store.com/products/white-sneaker\n\n"
    "• قميص قطني أزرق — 120 ريال\nhttps://shop.example-store.com/products/blue-cotton-shirt\n\n"
    "• عطر ورد 100ml — 260 ريال\nhttps://shop.example-store.com/products/rose-perfume-100\n\n"
    "تحب تفاصيل أكثر عن أي واحد؟"
)

# Category pages on the merchant's own domain: no product path, so only the
# domain itself says these are the merchant's links.
GENERIC_CUSTOM_DOMAIN_CATEGORIES = (
    "أقسام المتجر:\n"
    "https://shop.example-store.com/category/shoes\n"
    "https://shop.example-store.com/category/shirts\n"
    "https://shop.example-store.com/category/perfumes"
)

# Operational links of three different kinds in one reply.
PAYMENT_TRACKING_MAP_REPLY = (
    "طلبك RRRD1234 جاهز.\n"
    "الدفع: https://pay.salla.sa/invoice/RRRD1234\n"
    "التتبع: https://www.smsaexpress.com/track/RRRD1234\n"
    "موقع الفرع: https://maps.app.goo.gl/abc123"
)

# Three links the platform cannot place — the residual dump shape.
UNRECOGNISED_LINKS_REPLY = (
    "حسب ما لقيت:\n"
    "https://example-blog-one.com/article\n"
    "https://another-site.example.org/page\n"
    "https://third-host.example.net/post"
)


# ── 1. Fingerprints are evidence; they still replace the reply ───────────────


class TestFingerprintsStillReplace:

    @pytest.mark.parametrize("marker, body", [
        ("duckduckgo_bridge", "المصدر: https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com"),
        ("duckduckgo_redirect", "انظر uddg=https%3A%2F%2Fexample.com&rut=abc"),
        ("bing_search", "https://www.bing.com/search?q=فاتورة"),
        ("google_search", "https://www.google.com/search?q=فاتورة+كهرباء"),
        ("wikipedia_citation", "حسب https://ar.wikipedia.org/wiki/كهرباء"),
        ("double_encoded_url", "https://x.com/?u=https%3A%2F%2Fy.com%2F%25D8%25A7"),
        ("sources_header", "الجواب هنا.\nالمصادر:\n- https://example.com/a"),
    ])
    def test_each_fingerprint_is_named(self, marker: str, body: str) -> None:
        from core.outbound_sanitizer import contains_leakage_markers
        assert contains_leakage_markers(body) == marker

    def test_a_fingerprinted_reply_is_replaced_and_its_buttons_stripped(self) -> None:
        from core.outbound_sanitizer import SAFE_FALLBACK_TEXT, sanitize_outbound_payload
        payload = _interactive_payload(
            "حسب البحث: https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com")
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=7)
        assert sanitised is True
        assert out["interactive"]["body"]["text"] == SAFE_FALLBACK_TEXT
        assert out["interactive"]["action"] == {"buttons": []}

    def test_a_fingerprint_wins_even_when_every_link_is_first_party(self) -> None:
        """Recognised links do not launder a search dump."""
        from core.outbound_sanitizer import contains_leakage_markers
        body = OBSERVED_FOUR_PRODUCT_LISTING + "\nالمصادر:\n- https://demostore.salla.sa/x"
        assert contains_leakage_markers(body) == "sources_header"


# ── 2. Recognised links are not a leak ───────────────────────────────────────


class TestRecognisedLinksAreNotALeak:

    def test_the_observed_four_product_listing_passes_untouched(self, caplog) -> None:
        """The real Tenant 1 case: four links on the merchant's Salla store."""
        from core.outbound_sanitizer import contains_leakage_markers, sanitize_outbound_payload
        assert contains_leakage_markers(OBSERVED_FOUR_PRODUCT_LISTING) is None
        payload = _text_payload(OBSERVED_FOUR_PRODUCT_LISTING)
        with caplog.at_level(logging.INFO, logger=SANITIZER_LOGGER):
            out, sanitised = sanitize_outbound_payload(payload, tenant_id=1)
        assert sanitised is False
        assert out["text"]["body"] == OBSERVED_FOUR_PRODUCT_LISTING
        audit = [r for r in caplog.records if "[OUTBOUND_URL_AUDIT]" in r.getMessage()]
        assert len(audit) == 1 and "url_count=4" in audit[0].getMessage()
        assert "demostore.salla.sa" in audit[0].getMessage()
        assert not [r for r in caplog.records if "[EXTERNAL_RESEARCH_BLOCKED]" in r.getMessage()]

    def test_a_generic_merchant_listing_on_a_custom_domain_passes(self) -> None:
        """Product paths are recognised on any host; the merchant is not Salla-shaped."""
        from core.outbound_sanitizer import contains_leakage_markers, sanitize_outbound_payload
        assert contains_leakage_markers(GENERIC_CUSTOM_DOMAIN_LISTING) is None
        out, sanitised = sanitize_outbound_payload(_text_payload(GENERIC_CUSTOM_DOMAIN_LISTING),
                                                   tenant_id=2)
        assert sanitised is False and out["text"]["body"] == GENERIC_CUSTOM_DOMAIN_LISTING

    def test_the_merchant_s_own_domain_makes_its_category_links_first_party(self) -> None:
        from core.outbound_sanitizer import contains_leakage_markers
        assert contains_leakage_markers(GENERIC_CUSTOM_DOMAIN_CATEGORIES,
                                        store_domain="shop.example-store.com") is None
        # Without knowing the merchant's domain, the same links cannot be placed.
        assert contains_leakage_markers(GENERIC_CUSTOM_DOMAIN_CATEGORIES) == "too_many_urls"

    def test_the_store_domain_is_read_from_tenant_state_only_when_needed(self, monkeypatch) -> None:
        """The wire layer resolves the merchant's store domain lazily, through
        the platform's one store-URL resolver, and only for a reply that
        would otherwise trip the rule."""
        from modules.ai.brain.commerce import store_url_resolver
        from core.outbound_sanitizer import sanitize_outbound_payload

        calls: list = []

        def fake_lookup(db, tenant_id):
            calls.append((db, tenant_id))
            return "https://shop.example-store.com/"

        monkeypatch.setattr(store_url_resolver, "lookup_tenant_store_url", fake_lookup)
        db = object()
        out, sanitised = sanitize_outbound_payload(_text_payload(GENERIC_CUSTOM_DOMAIN_CATEGORIES),
                                                   tenant_id=2, db=db, skip_handoff_scrub=True)
        assert sanitised is False
        assert out["text"]["body"] == GENERIC_CUSTOM_DOMAIN_CATEGORIES
        assert calls == [(db, 2)]

        # A reply with two links never pays for the lookup.
        calls.clear()
        two = "https://a.example.com/x\nhttps://b.example.com/y"
        sanitize_outbound_payload(_text_payload(two), tenant_id=2, db=db, skip_handoff_scrub=True)
        assert calls == []

    def test_an_unreadable_store_domain_leaves_the_rule_as_it_was(self, monkeypatch) -> None:
        from modules.ai.brain.commerce import store_url_resolver
        from core.outbound_sanitizer import SAFE_FALLBACK_TEXT, sanitize_outbound_payload

        def broken(db, tenant_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(store_url_resolver, "lookup_tenant_store_url", broken)
        out, sanitised = sanitize_outbound_payload(_text_payload(UNRECOGNISED_LINKS_REPLY),
                                                   tenant_id=2, db=object(), skip_handoff_scrub=True)
        assert sanitised is True and out["text"]["body"] == SAFE_FALLBACK_TEXT

    def test_storefront_platform_hosts_are_first_party_without_a_product_path(self) -> None:
        from core.outbound_sanitizer import contains_leakage_markers
        body = ("الأقسام:\nhttps://demostore.salla.sa/ar/category/a\n"
                "https://demostore.salla.sa/ar/category/b\nhttps://demostore.salla.sa/ar/category/c")
        assert contains_leakage_markers(body) is None
        zid = "https://brand.zid.store/a\nhttps://brand.zid.store/b\nhttps://brand.zid.store/c"
        assert contains_leakage_markers(zid) is None

    def test_payment_tracking_and_map_links_are_recognised(self) -> None:
        from core.outbound_sanitizer import contains_leakage_markers, sanitize_outbound_payload
        assert contains_leakage_markers(PAYMENT_TRACKING_MAP_REPLY) is None
        out, sanitised = sanitize_outbound_payload(_text_payload(PAYMENT_TRACKING_MAP_REPLY),
                                                   tenant_id=3)
        assert sanitised is False and out["text"]["body"] == PAYMENT_TRACKING_MAP_REPLY

    def test_a_clean_multi_link_interactive_reply_keeps_its_action(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload
        payload = _interactive_payload(GENERIC_CUSTOM_DOMAIN_LISTING)
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=2)
        assert sanitised is False
        assert out["interactive"]["action"]["name"] == "cta_url"

    def test_two_links_never_reach_the_audit(self, caplog) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload
        body = "الدفع: https://pay.salla.sa/x\nالتتبع: https://www.smsaexpress.com/track/1"
        with caplog.at_level(logging.INFO, logger=SANITIZER_LOGGER):
            sanitize_outbound_payload(_text_payload(body), tenant_id=3)
        assert not [r for r in caplog.records if "[OUTBOUND_URL_AUDIT]" in r.getMessage()]


# ── 3. The residual rule still holds for links nobody can place ──────────────


class TestUnrecognisedLinkFloodStillTrips:

    def test_three_unrecognised_links_are_still_a_dump(self) -> None:
        from core.outbound_sanitizer import SAFE_FALLBACK_TEXT, contains_leakage_markers, \
            sanitize_outbound_payload
        assert contains_leakage_markers(UNRECOGNISED_LINKS_REPLY) == "too_many_urls"
        out, sanitised = sanitize_outbound_payload(_text_payload(UNRECOGNISED_LINKS_REPLY),
                                                   tenant_id=4)
        assert sanitised is True and out["text"]["body"] == SAFE_FALLBACK_TEXT

    def test_recognised_links_do_not_raise_the_allowance_for_unrecognised_ones(self) -> None:
        """Four store links plus three unplaceable ones: the three still count."""
        from core.outbound_sanitizer import contains_leakage_markers
        assert contains_leakage_markers(
            OBSERVED_FOUR_PRODUCT_LISTING + "\n" + UNRECOGNISED_LINKS_REPLY) == "too_many_urls"

    def test_unrecognised_urls_is_the_pure_rule_behind_the_count(self) -> None:
        from core.outbound_sanitizer import unrecognised_urls
        urls = [
            "https://demostore.salla.sa/ar/p1",                 # storefront platform host
            "https://shop.example-store.com/products/x",        # product path
            "https://shop.example-store.com/category/shoes",    # merchant domain, when known
            "https://pay.salla.sa/invoice/1",                   # payment
            "https://maps.app.goo.gl/abc",                      # map
            "https://example-blog-one.com/article",             # nobody's
        ]
        assert unrecognised_urls(urls) == ["https://shop.example-store.com/category/shoes",
                                           "https://example-blog-one.com/article"]
        assert unrecognised_urls(urls, store_domain="shop.example-store.com") == [
            "https://example-blog-one.com/article"]

    def test_url_hosts_lists_each_host_once(self) -> None:
        from core.outbound_sanitizer import url_hosts
        assert url_hosts(OBSERVED_FOUR_PRODUCT_LISTING) == ["demostore.salla.sa"]
        assert url_hosts(PAYMENT_TRACKING_MAP_REPLY) == [
            "maps.app.goo.gl", "pay.salla.sa", "www.smsaexpress.com"]
        assert url_hosts("") == []
