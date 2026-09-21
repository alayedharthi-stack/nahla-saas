"""
tests/test_outbound_search_leak_guard.py
────────────────────────────────────────
Locks the wire-layer search-leak guard (``core.outbound_sanitizer``,
May 2026 incident) on both of its edges:

1.  Every external-research FINGERPRINT still replaces the reply with
    ``SAFE_FALLBACK_TEXT`` and strips interactive buttons. Those
    fingerprints are evidence of a search dump; nothing here weakens
    them.
2.  The number of links a reply carries is never a reason to rewrite
    it. September 2026, Tenant 1 Commerce Runtime pilot: the model
    composed a correct four-product listing with four links on the
    merchant's own Salla store and the retired ``too_many_urls`` rule
    replaced the whole reply with the apology. The owner retired the
    rule: a listing, a branch list, a "follow us" answer or a
    payment-plus-tracking reply passes unchanged, and a reply with many
    links is only logged (``[OUTBOUND_URL_AUDIT]``).

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

# Operational links of three different kinds in one reply.
PAYMENT_TRACKING_MAP_REPLY = (
    "طلبك RRRD1234 جاهز.\n"
    "الدفع: https://pay.salla.sa/invoice/RRRD1234\n"
    "التتبع: https://www.smsaexpress.com/track/RRRD1234\n"
    "موقع الفرع: https://maps.app.goo.gl/abc123"
)

# Three links on hosts the platform has no opinion about — a "follow us"
# answer from the merchant's own knowledge. The retired rule called this a
# search dump.
THIRD_PARTY_LINKS_REPLY = (
    "تابعنا على حساباتنا:\n"
    "https://www.instagram.com/generic.store\n"
    "https://www.tiktok.com/@generic.store\n"
    "https://www.snapchat.com/add/generic.store"
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
        """Store links do not launder a search dump."""
        from core.outbound_sanitizer import contains_leakage_markers
        body = OBSERVED_FOUR_PRODUCT_LISTING + "\nالمصادر:\n- https://demostore.salla.sa/x"
        assert contains_leakage_markers(body) == "sources_header"


# ── 2. A link count is never a reason to rewrite ─────────────────────────────


class TestLinkCountNeverRewrites:

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

    @pytest.mark.parametrize("body", [
        GENERIC_CUSTOM_DOMAIN_LISTING,
        PAYMENT_TRACKING_MAP_REPLY,
        THIRD_PARTY_LINKS_REPLY,
    ])
    def test_multi_link_replies_of_every_kind_pass_unchanged(self, body: str) -> None:
        from core.outbound_sanitizer import contains_leakage_markers, sanitize_outbound_payload
        assert contains_leakage_markers(body) is None
        out, sanitised = sanitize_outbound_payload(_text_payload(body), tenant_id=2)
        assert sanitised is False and out["text"]["body"] == body

    def test_many_links_nobody_can_place_are_logged_not_rewritten(self, caplog) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload
        body = "\n".join(f"https://host-{i}.example.org/page" for i in range(6))
        with caplog.at_level(logging.INFO, logger=SANITIZER_LOGGER):
            out, sanitised = sanitize_outbound_payload(_text_payload(body), tenant_id=4)
        assert sanitised is False and out["text"]["body"] == body
        audit = [r for r in caplog.records if "[OUTBOUND_URL_AUDIT]" in r.getMessage()]
        assert len(audit) == 1 and "url_count=6" in audit[0].getMessage()

    def test_the_marker_predicate_never_names_a_link_count(self) -> None:
        from core.outbound_sanitizer import contains_leakage_markers
        body = "\n".join(f"https://host-{i}.example.org/page" for i in range(40))
        assert contains_leakage_markers(body) is None

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

    def test_url_hosts_lists_each_host_once(self) -> None:
        from core.outbound_sanitizer import url_hosts
        assert url_hosts(OBSERVED_FOUR_PRODUCT_LISTING) == ["demostore.salla.sa"]
        assert url_hosts(PAYMENT_TRACKING_MAP_REPLY) == [
            "maps.app.goo.gl", "pay.salla.sa", "www.smsaexpress.com"]
        assert url_hosts("") == []
