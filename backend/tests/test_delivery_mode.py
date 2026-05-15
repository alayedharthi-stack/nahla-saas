"""
tests/test_delivery_mode.py
───────────────────────────
Locks the F-Delivery-Mode observability helpers. Pure helpers, pure
unit tests — no DB, no HTTP, no asyncio.

What's covered
──────────────
1. ``compute_final_delivery_mode`` returns the right verdict for
   every closed-enum outcome:
     * ``catalog`` (precedence over media)
     * ``image_cta``
     * ``media_only``
     * ``cta_only``
     * ``text_only``
     * ``failed`` (initial send failed AND empty-audit fallback)
2. Precedence rules — when multiple signals are present, the
   higher-tier mode wins.
3. ``customer_wants_product_or_image`` fires on both brain-action
   signals AND inbound-text keywords; stays conservative on
   ambiguous inputs.
4. ``is_acceptable_mode_for_product_intent`` — guard rule that the
   webhook uses to decide whether to emit
   ``[DELIVERY_GUARD_FAIL]``.

This suite is the SOURCE OF TRUTH for the verdict. If a future
template / responder change shifts what "useful content" means,
update these tests FIRST and then the helpers — never the other
way round.

Run:
    cd backend
    python -m pytest tests/test_delivery_mode.py -v
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from modules.observability import (
    DELIVERY_MODE_CATALOG,
    DELIVERY_MODE_CTA_ONLY,
    DELIVERY_MODE_FAILED,
    DELIVERY_MODE_IMAGE_CTA,
    DELIVERY_MODE_MEDIA_ONLY,
    DELIVERY_MODE_TEXT_ONLY,
    compute_final_delivery_mode,
    customer_wants_product_or_image,
    new_delivery_audit,
)
from modules.observability.delivery_mode import (
    is_acceptable_mode_for_product_intent,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. compute_final_delivery_mode — happy paths for every mode
# ─────────────────────────────────────────────────────────────────────────────

class TestModeComputation:
    def test_catalog_mode_when_any_catalog_card_landed(self):
        a = new_delivery_audit()
        a["text_sent"] = True
        a["catalog_card_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_CATALOG

    def test_catalog_beats_media_when_both_present(self):
        """Precedence — a catalog card is the richest experience
        and must win even if a legacy media also landed (rare but
        possible when an attachment list mixes both)."""
        a = new_delivery_audit()
        a["text_sent"] = True
        a["catalog_card_sent_count"] = 1
        a["legacy_media_sent_count"] = 2
        a["cta_url_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_CATALOG

    def test_image_cta_when_media_and_cta_url_both_landed(self):
        a = new_delivery_audit()
        a["text_sent"] = True
        a["legacy_media_sent_count"] = 1
        a["cta_url_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_IMAGE_CTA

    def test_media_only_when_image_lands_without_cta(self):
        a = new_delivery_audit()
        a["text_sent"] = True
        a["legacy_media_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_MEDIA_ONLY

    def test_cta_only_when_link_lands_without_image(self):
        a = new_delivery_audit()
        a["text_sent"] = True
        a["cta_url_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_CTA_ONLY

    def test_text_only_for_plain_text_reply(self):
        a = new_delivery_audit()
        a["text_sent"] = True
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_TEXT_ONLY

    def test_text_only_when_interactive_buttons_used_but_no_rich_content(self):
        """Quick-reply buttons attached to a text body still classify
        as ``text_only`` — the customer received a question, not a
        product card or image."""
        a = new_delivery_audit()
        a["interactive_buttons_sent"] = True
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_TEXT_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# 2. failure paths
# ─────────────────────────────────────────────────────────────────────────────

class TestFailurePaths:
    def test_failed_when_first_send_failed_flag_set(self):
        a = new_delivery_audit()
        a["first_send_failed"] = True
        a["text_sent"] = False
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_FAILED

    def test_failed_overrides_partial_attachments(self):
        """If the initial reply send failed, downstream attachments
        couldn't have run successfully either. The mode is
        ``failed`` regardless of stale counters."""
        a = new_delivery_audit()
        a["first_send_failed"] = True
        # Hypothetical: an inconsistent caller left counters
        # populated. We must still return ``failed``.
        a["catalog_card_sent_count"] = 1
        a["legacy_media_sent_count"] = 1
        a["cta_url_sent_count"] = 1
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_FAILED

    def test_empty_audit_classifies_as_failed(self):
        """Defensive — a caller that forgets to populate the audit
        gets the safest classification (``failed``) instead of a
        false-positive ``text_only``."""
        a = new_delivery_audit()
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_FAILED

    def test_none_input_classifies_as_failed(self):
        assert compute_final_delivery_mode(None) == DELIVERY_MODE_FAILED  # type: ignore[arg-type]

    def test_partial_dict_with_extra_keys_is_accepted(self):
        """Forward-compatibility — extra keys (e.g. counters added
        by a future webhook revision) must not break classification."""
        a = new_delivery_audit()
        a["text_sent"] = True
        a["future_counter_xyz"] = 42  # type: ignore[typeddict-unknown-key]
        assert compute_final_delivery_mode(a) == DELIVERY_MODE_TEXT_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# 3. customer_wants_product_or_image — brain-action signal
# ─────────────────────────────────────────────────────────────────────────────

class TestIntentBrainSignal:
    @pytest.mark.parametrize("action", [
        "search_products",
        "recommend_addon",
        "propose_draft_order",
    ])
    def test_known_product_actions_fire(self, action):
        assert customer_wants_product_or_image(
            inbound_text="", brain_action=action,
        ) is True

    @pytest.mark.parametrize("action", [
        "greet",
        "social_reply",
        "faq_reply",
        "platform_reply",
        "out_of_scope",
        "clarify",
        "narrow",
        "handoff",
        "llm_reply",
        "",
        None,
    ])
    def test_non_product_actions_do_not_fire_on_brain_alone(self, action):
        assert customer_wants_product_or_image(
            inbound_text="", brain_action=action or "",
        ) is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. customer_wants_product_or_image — inbound-text signal
# ─────────────────────────────────────────────────────────────────────────────

class TestIntentInboundText:
    """High-confidence Arabic phrasings must fire. Generic chitchat
    must NOT — false positives erode trust in the alarm."""

    @pytest.mark.parametrize("msg", [
        # The exact production regression text.
        "أبغى أشوف صورة لعسل السمر",
        # Variants the merchant might type.
        "أبي أشوف عسل سمر",
        "ودي أشوف منتجاتك",
        "خلني أشوف الأنواع",
        "وريني الكتالوج",
        "أرني صورة المنتج",
        "اعرض علي الكتالوج",
        "ابعث صورة لعسل السدر",
        "أرسل صورة المنتج",
        "كتالوج المتجر لو سمحت",
        "أبي منتج للمناعة",
        "ودي صورة",
    ])
    def test_high_confidence_phrases_fire(self, msg):
        assert customer_wants_product_or_image(
            inbound_text=msg, brain_action="",
        ) is True

    @pytest.mark.parametrize("msg", [
        # Bare social — must NOT fire.
        "السلام عليكم",
        "وعليكم السلام",
        "هلا",
        "كيفك",
        "شكرا",
        "تسلم",
        # Generic question — no product / image keywords.
        "وين فرعكم؟",
        "في توصيل؟",
        # Ambiguous solo "صورة" — too noisy on its own.
        "صورة",
        # Empty / numeric noise.
        "",
        "123",
        "🌹",
    ])
    def test_ambiguous_or_chitchat_does_not_fire(self, msg):
        assert customer_wants_product_or_image(
            inbound_text=msg, brain_action="",
        ) is False

    def test_either_signal_alone_is_enough(self):
        """Brain action alone fires."""
        assert customer_wants_product_or_image(
            inbound_text="مرحبا", brain_action="search_products",
        ) is True

    def test_normalisation_does_not_drop_valid_match(self):
        """Extra whitespace inside the phrase must still match —
        the function collapses whitespace before checking."""
        assert customer_wants_product_or_image(
            inbound_text="أبي    أشوف   عسل", brain_action="",
        ) is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Guard rule — product intent + bad mode → alarm
# ─────────────────────────────────────────────────────────────────────────────

class TestGuardRule:
    """The webhook fires ``[DELIVERY_GUARD_FAIL]`` when the customer
    asked for product content AND the final mode is unacceptable.
    This test class pins the truth table."""

    def test_text_only_for_product_intent_is_NOT_acceptable(self):
        """The exact production regression — must alarm."""
        assert is_acceptable_mode_for_product_intent(
            DELIVERY_MODE_TEXT_ONLY
        ) is False

    def test_cta_only_for_product_intent_is_NOT_acceptable(self):
        """A bare URL with no image isn't "seeing" anything — alarm."""
        assert is_acceptable_mode_for_product_intent(
            DELIVERY_MODE_CTA_ONLY
        ) is False

    def test_failed_for_product_intent_is_NOT_acceptable(self):
        assert is_acceptable_mode_for_product_intent(
            DELIVERY_MODE_FAILED
        ) is False

    @pytest.mark.parametrize("mode", [
        DELIVERY_MODE_CATALOG,
        DELIVERY_MODE_IMAGE_CTA,
        DELIVERY_MODE_MEDIA_ONLY,
    ])
    def test_rich_modes_for_product_intent_are_acceptable(self, mode):
        assert is_acceptable_mode_for_product_intent(mode) is True

    def test_unknown_mode_string_is_not_acceptable(self):
        """Defensive — a future mode we haven't taught the guard
        about should err on the side of alerting, not silence."""
        assert is_acceptable_mode_for_product_intent("future_mode_xyz") is False


# ─────────────────────────────────────────────────────────────────────────────
# 6. End-to-end — pin the production regression
# ─────────────────────────────────────────────────────────────────────────────

class TestProductionRegressionScenario:
    """Reproduce the exact production case where the bot replied
    "أبشر خالد 🍯" to "أبغى أشوف صورة لعسل السمر" with no rich
    content. The classifier must say "wanted product" AND the
    mode must be ``text_only`` AND the guard must say "alarm"."""

    def test_full_chain_alerts_on_text_only_for_product_intent(self):
        # Stage 1: the customer asked to see a product image.
        wants = customer_wants_product_or_image(
            inbound_text="أبغى أشوف صورة لعسل السمر",
            brain_action="llm_reply",  # bot fell through to chitchat
        )
        assert wants is True

        # Stage 2: the bot sent only a plain text body — no
        # catalog, no media, no CTA.
        a = new_delivery_audit()
        a["text_sent"] = True
        mode = compute_final_delivery_mode(a)
        assert mode == DELIVERY_MODE_TEXT_ONLY

        # Stage 3: the guard fires.
        assert is_acceptable_mode_for_product_intent(mode) is False
