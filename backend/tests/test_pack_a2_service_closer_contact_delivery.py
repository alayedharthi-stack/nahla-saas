"""Pack A2 — service_closer must not wipe grounded FAQ owner_contact delivery."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in (_backend, os.path.join(_backend, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import (
    contains_service_closer,
    strip_closer_segments,
)
from modules.ai.brain.compose.templates import faq_owner_contact
from modules.ai.brain.postprocess.service_closer_guard import apply_service_closer_guard


CONTACT_FAQ = faq_owner_contact(
    contact_phone="",
    contact_email="hello@demo.example",
    store_url="https://demo.example/store-a",
    social_links={
        "instagram": "https://instagram.com/demo_store",
        "twitter": "https://x.com/demo_store",
        "telegram": "DemoStoreTG",
    },
)


class TestOwnerContactSurvivesServiceCloser:
    def test_contact_delivery_header_not_treated_as_closer_only(self) -> None:
        # Bare marker must not fire solely because of "تتواصل معنا عبر".
        assert "تقدر تتواصل معنا عبر" in CONTACT_FAQ
        cleaned, stripped = strip_closer_segments(CONTACT_FAQ)
        assert "hello@demo.example" in cleaned
        assert "https://instagram.com/demo_store" in cleaned
        assert cleaned.strip()
        # Full contact block may still strip true trailing closers if present,
        # but must not empty the factual body.
        assert len(cleaned) > 40

    def test_apply_service_closer_keeps_grounded_contact(self) -> None:
        result = apply_service_closer_guard(
            CONTACT_FAQ,
            inbound_text="كيف أتواصل معكم؟",
            tenant_id=7,
        )
        assert result.reply.strip()
        assert "hello@demo.example" in result.reply
        assert "instagram.com/demo_store" in result.reply
        assert "الجوال:" not in result.reply

    def test_email_only_contact_survives(self) -> None:
        text = faq_owner_contact(contact_email="a@example.com")
        result = apply_service_closer_guard(text, inbound_text="وش إيميلكم؟", tenant_id=7)
        assert "a@example.com" in result.reply

    def test_social_only_contact_survives(self) -> None:
        text = faq_owner_contact(
            social_links={"instagram": "https://social.example/a"},
        )
        result = apply_service_closer_guard(
            text, inbound_text="عندكم حسابات تواصل؟", tenant_id=7
        )
        assert "social.example/a" in result.reply

    def test_true_cs_closer_still_stripped(self) -> None:
        raw = "الله يسعدك 🌷 إذا تحتاج أي مساعدة أو عندك استفسار، أنا هنا!"
        result = apply_service_closer_guard(raw, tenant_id=7)
        assert result.stripped is True
        assert "إذا تحتاج" not in result.reply
        assert "أنا هنا" not in result.reply
        assert "الله يسعدك" in result.reply

    def test_specific_contact_us_closer_still_detected(self) -> None:
        # Keep precise "contact us" CS closers detectable without bare
        # "تواصل معنا" / "للتواصل معنا" that wipe FAQ owner_contact delivery.
        assert contains_service_closer("يرجى التواصل معنا.")
        assert contains_service_closer("للتواصل معنا عند الحاجة.")
        assert not contains_service_closer("تقدر تتواصل معنا عبر:")
        assert not contains_service_closer("للتواصل معنا عبر:\nالبريد: a@example.com")

    def test_post_compose_chain_keeps_contact_nonempty(self) -> None:
        """Mimic pipeline order: service_closer then quality_guard."""
        from modules.ai.brain.postprocess.commerce_reply_quality_guard import (
            apply_commerce_reply_quality_guard,
        )

        after_sc = apply_service_closer_guard(
            CONTACT_FAQ,
            inbound_text="كيف أتواصل معكم؟",
            tenant_id=1,
        ).reply
        assert after_sc.strip()
        after_qg = apply_commerce_reply_quality_guard(
            reply=after_sc,
            inbound_text="كيف أتواصل معكم؟",
            intent_name="ask_owner_contact",
            locale="ar",
            tenant_id=1,
            decision_topic="owner_contact",
            chosen_path="rule",
        ).reply
        assert after_qg.strip()
        assert "hello@demo.example" in after_qg
        assert "instagram.com/demo_store" in after_qg

    def test_dual_tenant_values_isolated(self) -> None:
        a = faq_owner_contact(
            contact_email="a@example.com",
            social_links={"instagram": "https://social.example/a"},
        )
        b = faq_owner_contact(
            contact_email="b@example.com",
            social_links={"instagram": "https://social.example/b"},
        )
        out_a = apply_service_closer_guard(a, tenant_id=1).reply
        out_b = apply_service_closer_guard(b, tenant_id=2).reply
        assert "a@example.com" in out_a and "b@example.com" not in out_a
        assert "b@example.com" in out_b and "a@example.com" not in out_b
