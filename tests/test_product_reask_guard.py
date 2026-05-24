"""
tests/test_product_reask_guard.py
─────────────────────────────────
Tenant 33 #47 — recurring regression: after the customer confirms
product + quantity + price and the bot asks for the customer's
location, the LLM occasionally loses the product context and
replies with "وصلني موقعك. قبل ما نكمل، اختر المنتج اللي تبغاه من
القائمة" — re-asking for a product that's already chosen.

This file pins the regression DOWN. The guard is implemented in
``modules.ai.postprocess.safety_nets.apply_product_reask_guard`` and
fires only when THREE independent signals align:

  1. The bot's reply contains a "re-ask product" phrase.
  2. The customer's inbound carries a location signal (Maps URL,
     geo coords, national short address code, or "موقعي" /
     "العنوان الوطني").
  3. The recent history (last 3 outbounds) carries an active-order
     marker (price + currency, quantity confirm, checkout cue).

All three together = the brain is contradicting recent context
and we MUST rewrite. Any one missing = stay out of the way; the
brain may legitimately be asking which product the customer wants.

The test name ``test_maps_link_after_confirmed_product_does_not_reask_product``
is intentionally descriptive — if you find this test deleted or
renamed, the underlying bug has resurfaced; restore the guard
BEFORE shipping the change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _enable_guard(monkeypatch):
    """The guard ships ON by default — pin the env in the test
    suite so a host with PRODUCT_REASK_GUARD_ENABLED=false cannot
    silently drop the regression coverage."""
    monkeypatch.setenv("PRODUCT_REASK_GUARD_ENABLED", "true")
    yield


# ════════════════════════════════════════════════════════════════════
# Canonical fixtures from the Tenant 33 #47 transcript
# ════════════════════════════════════════════════════════════════════

# Bot's "we just confirmed price+quantity" outbound — carries the
# active-order marker (price + currency token).
_BOT_CONFIRMED_PRICE = (
    "نص كيلو طلح بلدي = 193 ريال 🌷"
)

# Bot's "send me your location" outbound — carries the awaiting-
# delivery marker too, but for THIS test the active-order marker
# above is what the guard relies on.
_BOT_ASKED_LOCATION = (
    "أرسل لي موقعك على قوقل ماب أو الرمز الوطني المختصر "
    "عشان نجهز الشحنة 🌷"
)

# Customer's response — a Google Maps URL.
_CUSTOMER_MAPS_URL = (
    "https://maps.google.com/?q=24.7136,46.6753"
)

# The exact contradictory reply from the production transcript.
_BUGGY_BRAIN_REPLY = (
    "وصلني موقعك. قبل ما نكمل، اختر المنتج اللي تبغاه من القائمة "
    "وكميته."
)


def _confirmed_order_history():
    """History shape: customer asked → bot confirmed price → bot
    asked for location → customer sent location. The guard scans
    only the bot outbounds, so the inbound contents don't matter
    here as long as direction labels are right."""
    return [
        {"direction": "in",  "body": "أبي نص كيلو طلح بلدي"},
        {"direction": "out", "body": _BOT_CONFIRMED_PRICE},
        {"direction": "out", "body": _BOT_ASKED_LOCATION},
    ]


# ════════════════════════════════════════════════════════════════════
# Headline regression — DO NOT delete or rename this test.
# ════════════════════════════════════════════════════════════════════


def test_maps_link_after_confirmed_product_does_not_reask_product():
    """Headline regression for Tenant 33 #47.

    History:
      * Customer chose a product.
      * Bot confirmed quantity + price (active-order marker).
      * Bot asked for Google Maps / national address.
      * Customer sent a Google Maps link.

    Expected:
      * The contradictory "اختر المنتج" reply MUST be rewritten.
      * Resulting reply MUST NOT contain "اختر المنتج" / "أي منتج"
        / "حدد المنتج" / "من القائمة".
      * Resulting reply MUST acknowledge the location and continue
        the shipping flow (وصلني موقعك …).

    If this test ever fails, the recurring product-context-loss
    regression has resurfaced. Restore the guard BEFORE shipping.
    """
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )

    assert result.fired is True, (
        "Product re-ask guard MUST fire when the brain contradicts "
        "a freshly-confirmed product+price by asking for the "
        "product again right after the customer sent their location."
    )
    assert result.reason == "product_reask_after_location_in_active_order"
    assert result.has_maps_url is True

    # The rewritten reply must not contain ANY of the forbidden
    # product re-ask phrases.
    rewritten = result.new_reply
    assert rewritten, "Guard fired but new_reply is empty"
    for forbidden in (
        "اختر المنتج", "اختار المنتج", "حدد المنتج",
        "أي منتج", "اي منتج",
        "اسم المنتج",
        "من القائمة", "من القائمه",
    ):
        assert forbidden not in rewritten, (
            f"Rewritten reply still contains forbidden re-ask "
            f"phrase: {forbidden!r}\nReply: {rewritten!r}"
        )

    # And must acknowledge the location + continue shipping flow.
    assert "وصلني موقعك" in rewritten, (
        "Rewritten reply must acknowledge the customer's location."
    )


# ════════════════════════════════════════════════════════════════════
# Negative cases — DO NOT over-fire on legitimate flows.
# ════════════════════════════════════════════════════════════════════


def test_maps_link_in_general_chat_does_not_trigger_guard():
    """Negative regression: a customer who sends a Google Maps URL
    in a general chat (no product/price/quantity in recent
    outbounds) must NOT have the brain's reply rewritten — there's
    no contradiction to fix, and the brain may legitimately be
    asking the customer to pick a product."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    history = [
        {"direction": "in",  "body": "السلام عليكم"},
        {"direction": "out", "body": "وعليكم السلام 🌷 كيف نقدر نخدمك؟"},
    ]
    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text=_BUGGY_BRAIN_REPLY,
        history=history,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_active_order_context"


def test_no_location_in_inbound_does_not_trigger_guard():
    """Negative: bot is asking for product, customer's inbound is
    NOT a location → brain may legitimately be asking. The guard
    must stay out of the way."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg="ايش عندكم؟",
        reply_text="اختر المنتج من القائمة 🌷",
        history=_confirmed_order_history(),
    )
    assert result.fired is False
    assert result.skipped_reason == "inbound_not_location"


def test_brain_reply_not_a_product_reask_does_not_trigger_guard():
    """Negative: bot replied with a perfectly-fine order-continuation
    line. The guard must not rewrite a healthy reply."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text="وصلني موقعك 🌷 جاري تجهيز الطلب.",
        history=_confirmed_order_history(),
    )
    assert result.fired is False
    assert result.skipped_reason == "reply_not_product_reask"


def test_empty_reply_does_not_trigger_guard():
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text="",
        history=_confirmed_order_history(),
    )
    assert result.fired is False
    assert result.skipped_reason == "empty_reply"


def test_kill_switch_disables_guard(monkeypatch):
    """Ops can flip PRODUCT_REASK_GUARD_ENABLED=false to silence
    the guard without a redeploy."""
    monkeypatch.setenv("PRODUCT_REASK_GUARD_ENABLED", "false")
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is False
    assert result.skipped_reason == "flag_disabled"


# ════════════════════════════════════════════════════════════════════
# ACK-shape coverage — adapts to the data the customer sent
# ════════════════════════════════════════════════════════════════════


def test_ack_when_location_only_asks_for_remaining_data():
    """Customer sent only a location → ACK must mention the
    remaining fields (name + phone) so the merchant has a path
    forward without re-asking the product."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is True
    assert "الاسم" in result.new_reply
    assert "الجوال" in result.new_reply


def test_ack_when_full_data_does_not_ask_for_more():
    """Customer sent location + name + phone → ACK confirms data
    is complete and signals the next step (payment/confirmation)
    without asking for additional fields."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    msg = (
        "خالد الحربي\n0552375813\n"
        "https://maps.google.com/?q=24.7136,46.6753"
    )
    result = apply_product_reask_guard(
        customer_msg=msg,
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is True
    assert "بيانات الشحن اكتملت" in result.new_reply
    # Should NOT ask for name/phone again — those arrived.
    assert "نحتاج الاسم" not in result.new_reply


# ════════════════════════════════════════════════════════════════════
# Location-signal variants — Maps URL is one of many shapes
# ════════════════════════════════════════════════════════════════════


def test_short_address_code_inbound_triggers_guard():
    """Saudi national short address (4 letters + 4 digits) is a
    valid location signal even without a Google Maps URL."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg="RAKB1234",
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is True
    assert result.has_short_address is True


def test_explicit_address_keyword_inbound_triggers_guard():
    """Customer who types "العنوان الوطني" + value also counts."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg="العنوان الوطني RAKB1234",
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is True


def test_maps_app_short_link_inbound_triggers_guard():
    """The newer ``https://maps.app.goo.gl/...`` short-link shape
    must also count as a location signal."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg="https://maps.app.goo.gl/AbCd1234",
        reply_text=_BUGGY_BRAIN_REPLY,
        history=_confirmed_order_history(),
    )
    assert result.fired is True


# ════════════════════════════════════════════════════════════════════
# Re-ask phrasing variants — the brain has many ways to misbehave
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "buggy_reply",
    [
        "وصلني موقعك. اختر المنتج اللي تبغاه من القائمة.",
        "وصلني موقعك 🌷 حدد المنتج وكميته.",
        "تمام، أي منتج تريد بالضبط؟",
        "ممكن تخبرني وش المنتج اللي تبغ؟",
        "اسم المنتج لو سمحت 🌷",
        "اختار من المنتجات اللي تبغاها.",
    ],
)
def test_various_product_reask_phrasings_all_caught(buggy_reply):
    """Pin a small grid of brain mis-phrasings — all must be
    caught by the guard. If a future brain prompt change introduces
    a new phrasing the guard misses, ADD IT to the marker list
    rather than to this test parameter — the guard is the single
    source of truth."""
    from modules.ai.postprocess.safety_nets import apply_product_reask_guard

    result = apply_product_reask_guard(
        customer_msg=_CUSTOMER_MAPS_URL,
        reply_text=buggy_reply,
        history=_confirmed_order_history(),
    )
    assert result.fired is True, (
        f"Guard missed product re-ask phrasing: {buggy_reply!r}"
    )


# ════════════════════════════════════════════════════════════════════
# Helper-level pins (defensive — caught by integration tests above
# but useful for fast feedback during refactors)
# ════════════════════════════════════════════════════════════════════


def test_helper_detects_maps_url_as_location():
    from modules.ai.postprocess.safety_nets import _customer_inbound_has_location

    assert _customer_inbound_has_location(_CUSTOMER_MAPS_URL) is True


def test_helper_detects_short_address_via_keyword():
    from modules.ai.postprocess.safety_nets import _customer_inbound_has_location

    assert _customer_inbound_has_location("العنوان الوطني RAKB1234") is True


def test_helper_does_not_flag_plain_chat():
    from modules.ai.postprocess.safety_nets import _customer_inbound_has_location

    assert _customer_inbound_has_location("شكراً، تسلمين") is False


def test_helper_detects_product_reask_phrase():
    from modules.ai.postprocess.safety_nets import _reply_looks_like_product_reask

    assert _reply_looks_like_product_reask(_BUGGY_BRAIN_REPLY) is True


def test_helper_does_not_flag_healthy_order_reply():
    from modules.ai.postprocess.safety_nets import _reply_looks_like_product_reask

    assert _reply_looks_like_product_reask(
        "وصلني موقعك 🌷 جاري تجهيز الطلب."
    ) is False
