"""
tests/test_clear_intent_and_delivery_nets.py
────────────────────────────────────────────
Coverage for the two May-2026 safety nets that fix the production
embarrassments captured in:

    1. Screenshot of "هل يوجد عروض على العسل" →
       bot replied "عذراً، تأخّر الرد قليلاً. هل يمكنك إعادة سؤالك؟"
       → handled by ``apply_clear_intent_fallback_net``.

    2. Screenshot of an April-2026 turn where the bot asked the
       customer for the shipping address, the customer typed the
       full address (name, phone, city, district, building number)
       and the bot replied "أعتذر، هذا خارج تخصصي…" →
       handled by ``apply_delivery_info_context_net``.

Both nets are pure text post-processors. They never mutate order
state, never invent prices, never delete LLM markers. They simply
REWRITE the outbound text when the LLM produced a self-deprecating
fallback for a clear customer question.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# Both nets are gated by feature flags (default ON). For the test
# suite we keep the kill switches explicitly ON so we never depend
# on the host's environment defaults.
@pytest.fixture(autouse=True)
def _enable_safety_nets(monkeypatch):
    monkeypatch.setenv("CLEAR_INTENT_FALLBACK_NET_ENABLED", "true")
    monkeypatch.setenv("DELIVERY_INFO_CONTEXT_NET_ENABLED", "true")
    yield


# ════════════════════════════════════════════════════════════════════
# Part 1 — Clear-intent fallback safety net
# ════════════════════════════════════════════════════════════════════


# The canonical LLM-timeout copy from
# backend/modules/ai/brain/compose/responder.py — the literal line
# the production bot shipped in the user's screenshot.
PRODUCTION_TIMEOUT_REPLY = (
    "عذراً، تأخّر الرد قليلاً. هل يمكنك إعادة سؤالك؟ "
    "أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
)


class TestClearIntentFires:
    """The screenshotted scenario: clear question + generic fallback."""

    def test_screenshot_scenario_offers_on_honey(self):
        """The literal screenshot scenario — honey merchant. Asserts
        the GENERIC offers reply (no category name) is shipped."""
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="سلام عليكم هل يوجد عروض على العسل",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"
        assert result.reason == "reply_asked_to_repeat_clear_question"
        assert "تأخر"      not in result.new_reply
        assert "إعادة سؤالك" not in result.new_reply
        # Cross-merchant invariant: the reply must NOT hard-code a
        # product class. It's the same line for every merchant.
        assert "عسل" not in result.new_reply

    @pytest.mark.parametrize(
        "msg,expected_intent",
        [
            # Honey merchant
            ("بكم سعر عسل السدر؟", "price"),
            ("ابغى اعرف انواع العسل المتوفرة", "product"),
            ("ابي اشتري عسل طلح",  "order"),
            ("ودي اطلب كيلو عسل",  "order"),
            ("هل عندكم خصومات اليوم؟", "offers"),
            # Perfume merchant
            ("ايش عندكم من العطور؟", "product"),
            ("بكم سعر هذا العطر؟",   "price"),
            ("ابي اطلب عطر عود",     "order"),
            # Electronics merchant
            ("هل عندكم عروض على الجوالات اليوم؟", "offers"),
            ("اشتري لي شاحن ايفون", "order"),
            ("كم سعر السماعات؟",     "price"),
            # Clothing merchant
            ("وش المتوفر من العبايات؟", "product"),
            ("ابي اطلب فستان مقاس L",   "order"),
            # Generic, category-free phrasing
            ("ايش المنتجات المتوفره؟",   "product"),
            ("ابغى اشوف الكتالوج",       "product"),
            ("اعرضي لي المنتجات",        "product"),
            ("هل عندكم تخفيضات على المنتجات؟", "offers"),
            # Cross-merchant infrastructure intents
            ("كم رسوم الشحن للرياض؟", "shipping"),
            ("ايش وسائل الدفع المتاحه؟", "payment"),
            ("ابعث لي رابط المتجر", "store_link"),
            # English (multilingual merchants)
            ("How much is shipping to Riyadh?", "shipping"),
            ("Do you have any sale today?", "offers"),
            ("Show me your products", "product"),
        ],
    )
    def test_clear_intents_trigger_rewrite(self, msg, expected_intent):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg=msg,
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == expected_intent
        assert result.new_reply
        # The new reply must NOT be the generic "please repeat" copy.
        assert "إعادة سؤالك" not in result.new_reply
        assert "أعد سؤالك"   not in result.new_reply
        assert "لم أفهم"      not in result.new_reply

    @pytest.mark.parametrize(
        "reply",
        [
            "عذراً، لم أفهم. ممكن توضح أكثر؟",
            "ما فهمت قصدك، ممكن تعيد؟",
            "Could you repeat your question please?",
            "I didn't understand. Please rephrase.",
        ],
    )
    def test_other_fallback_phrasings_also_trigger(self, reply):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="هل عندكم عروض على العسل؟",
            reply_text=reply,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"


class TestClearIntentCrossMerchant:
    """The new reply templates must NOT hard-code any product
    class — they have to work for every merchant."""

    @pytest.mark.parametrize("intent_key", [
        "offers", "price", "product", "store_link",
        "shipping", "payment", "order",
    ])
    def test_reply_template_is_category_neutral(self, intent_key):
        from modules.ai.postprocess import safety_nets
        reply = safety_nets._CLEAR_INTENT_REPLIES[intent_key]
        # Honey vocabulary must NOT leak into the canonical replies.
        for honey_word in (
            "عسل", "العسل", "طلح", "السدر", "السمر", "الزهر",
            "نحل", "النحل",
        ):
            assert honey_word not in reply, (
                f"Reply for {intent_key!r} hard-codes {honey_word!r} — "
                "the safety net runs for ALL merchants; replies must "
                "stay category-neutral."
            )

    def test_perfume_merchant_offers_question_uses_generic_reply(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض على العطور؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"
        assert "عطور" not in result.new_reply  # generic reply, no category
        assert "عسل"  not in result.new_reply  # never honey-flavoured

    def test_electronics_merchant_product_browse(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="ايش عندكم من سماعات بلوتوث؟",
            reply_text="عذراً، لم أفهم. ممكن توضح أكثر؟",
        )
        assert result.fired is True
        assert result.customer_intent == "product"
        assert "سماعات" not in result.new_reply  # generic


class TestClearIntentSkips:
    """Stays out of the way for genuinely ambiguous inputs and for
    non-fallback replies."""

    def test_truly_ambiguous_input_keeps_repeat_request(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="???",  # no recognisable intent
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is False
        assert result.skipped_reason == "no_clear_intent"

    def test_helpful_reply_left_alone(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        # Reply is already informative — net must not touch it.
        good_reply = "نعم 🌷 عندنا عرض على عسل السدر، السعر 120 ريال."
        result = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض على العسل؟",
            reply_text=good_reply,
        )
        assert result.fired is False
        assert result.skipped_reason == "reply_not_generic_fallback"

    def test_empty_reply_skipped(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="بكم العسل؟", reply_text="",
        )
        assert result.fired is False
        assert result.skipped_reason == "empty_reply"

    def test_kill_switch_disables_net(self, monkeypatch):
        monkeypatch.setenv("CLEAR_INTENT_FALLBACK_NET_ENABLED", "false")
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض على العسل؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is False
        assert result.skipped_reason == "flag_disabled"


class TestClearIntentNormalisation:
    """Arabic diacritics and English text are both handled."""

    def test_diacritics_in_customer_msg_still_match(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        # Fully voweled "هل عَنْدَكُمْ عُرُوضٌ عَلَى العَسَلِ؟"
        result = apply_clear_intent_fallback_net(
            customer_msg="هل عَنْدَكُمْ عُرُوضٌ عَلَى العَسَلِ؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"

    def test_english_clear_intent_message_triggers(self):
        from modules.ai.postprocess.safety_nets import (
            apply_clear_intent_fallback_net,
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="Do you have any discount on honey today?",
            reply_text="I'm sorry, could you repeat your question?",
        )
        assert result.fired is True
        assert result.customer_intent in {"offers", "product"}


# ════════════════════════════════════════════════════════════════════
# Part 2 — Delivery-info context-aware safety net
# ════════════════════════════════════════════════════════════════════


# The full screenshot scenario — bot asked for the shipping address
# / city, customer responded with name + phone + city + district +
# building number.
SCREENSHOT_BOT_ASK = (
    "وصل الإيصال يا الغالي 🌷 99 ريال عبر الراجحي، وسيتم متابعة "
    "الطلب وتجهيزه بإذن الله.\n\n"
    "ممكن ترسل لي عنوان الشحن أو المدينة عشان نرتب لك التوصيل؟"
)

SCREENSHOT_CUSTOMER_REPLY = (
    "خالد محيل صالح الحربي\n"
    "0552375813\n"
    "المدينة المنورة\n"
    "الحمراء حي الصناعية قرط بن ربيعة\n"
    "رقم المبنى 4365"
)

SCREENSHOT_DISMISSIVE_BOT_REPLY = (
    "أعتذر، هذا خارج تخصصي. لو تحب أساعدك في شي يخص العسل أو الطلب، "
    "أنا جاهزة 🌷"
)


def _history_with_outbound(text: str):
    """Build a minimal history list with one inbound followed by
    one outbound message ending with ``text``."""
    return [
        {"direction": "in",  "body": "إيصال تحويل بنكي"},
        {"direction": "out", "body": text},
    ]


class TestDeliveryInfoContextFires:
    def test_screenshot_scenario_full_address(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is True
        assert result.reason == "bot_was_awaiting_delivery_info"
        # Saudi phone must be picked up by our scan.
        assert result.has_phone is True
        # Either the deterministic ordering extractor caught the
        # city or the address-line — at minimum one of them.
        slot_keys = set(result.extracted_slots.keys())
        assert any(
            k in slot_keys
            for k in ("city", "customer_name", "customer_first_name", "phone")
        )
        # Confirmation copy — never the dismissive one.
        assert "خارج تخصصي" not in result.new_reply
        assert "وصلتني" in result.new_reply

    def test_partial_data_phone_only_nudges_for_missing(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg="0552375813",
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(
                "ممكن ترسل لي اسمك ورقم جوالك عشان نرتب التوصيل؟"
            ),
        )
        assert result.fired is True
        assert result.has_phone is True
        # Nudges for what's still missing (name / city / address).
        assert "وصلتني" in result.new_reply

    def test_repeat_fallback_with_address_data_also_rewritten(self):
        """Dismissive markers include 'إعادة سؤالك' — make sure
        the delivery-info net catches that variant too when the
        bot was awaiting delivery data."""
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text="عذراً، تأخّر الرد قليلاً. هل يمكنك إعادة سؤالك؟",
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is True


class TestDeliveryInfoContextSkips:
    def test_skips_when_bot_not_awaiting_delivery(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(
                "كيف نقدر نخدمك؟"  # No delivery-info markers
            ),
        )
        assert result.fired is False
        assert result.skipped_reason == "bot_not_awaiting_delivery"

    def test_skips_when_reply_is_already_helpful(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text="تمام 🌷 جاري ترتيب الطلب على الحمراء بالمدينة المنورة.",
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is False
        assert result.skipped_reason == "reply_not_dismissive"

    def test_skips_when_customer_msg_has_no_delivery_signal(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg="شكراً، تسلمين",
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is False
        assert result.skipped_reason == "no_delivery_signals_in_msg"

    def test_skips_when_history_is_empty(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=[],
        )
        assert result.fired is False
        assert result.skipped_reason == "bot_not_awaiting_delivery"

    def test_kill_switch_disables_net(self, monkeypatch):
        monkeypatch.setenv("DELIVERY_INFO_CONTEXT_NET_ENABLED", "false")
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is False
        assert result.skipped_reason == "flag_disabled"


class TestDeliveryInfoAcksHistoryShape:
    """The bot-awaiting detector must scan the last few outbound
    turns (the dispatcher sometimes shippes a follow-up card AFTER
    the question)."""

    def test_address_question_two_outbounds_ago_still_counts(self):
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        history = [
            {"direction": "in",  "body": "تم التحويل"},
            {"direction": "out", "body": SCREENSHOT_BOT_ASK},
            {"direction": "out", "body": "أرسل لي عنوانك في رسالة واحدة."},
            {"direction": "in",  "body": "..."},
        ]
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=history,
        )
        assert result.fired is True

    def test_handles_alternate_direction_key_spellings(self):
        """History rows may use 'inbound'/'outbound' OR 'in'/'out'."""
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        history = [
            {"direction": "inbound",  "body": "..."},
            {"direction": "outbound", "body": SCREENSHOT_BOT_ASK},
        ]
        result = apply_delivery_info_context_net(
            customer_msg=SCREENSHOT_CUSTOMER_REPLY,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=history,
        )
        assert result.fired is True


# ════════════════════════════════════════════════════════════════════
# Part 3 — Composition: the two nets cooperate, never contradict
# ════════════════════════════════════════════════════════════════════


class TestNetsCooperate:
    """Both nets may match the same turn — but the wiring in the
    webhook runs them in order (delivery first, then clear-intent).
    Either way, neither must produce a dismissive reply."""

    def test_delivery_response_with_offers_keyword_takes_delivery(self):
        """Customer pastes their address AND mentions 'عروض' inside
        it. The delivery context net should still fire because the
        bot was clearly waiting for the address."""
        from modules.ai.postprocess.safety_nets import (
            apply_delivery_info_context_net,
        )
        msg = (
            "خالد الحربي\n0552375813\nالرياض\n"
            "ولو في عروض على عسل الطلح اعطني خبر"
        )
        result = apply_delivery_info_context_net(
            customer_msg=msg,
            reply_text=SCREENSHOT_DISMISSIVE_BOT_REPLY,
            history=_history_with_outbound(SCREENSHOT_BOT_ASK),
        )
        assert result.fired is True
        # The acknowledgement must NOT include the dismissive copy.
        assert "خارج تخصصي" not in result.new_reply
