"""
tests/test_final_dispatch_guard.py
──────────────────────────────────
Final outbound dispatch guard — product cards must not leak on handoff,
social, fulfillment, or weak-intent turns.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_HANDOFF,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from services.final_dispatch_guard import (
    should_allow_product_attachment_dispatch,
    suppress_product_attachments,
)


_HANDOFF_REPLY = (
    "تمام 🌷 وصلت رسالتك، وسأخبر فريق المتجر ليتواصل معك في أقرب وقت ممكن."
)
_STALE_CARD = {
    "kind": "product_card",
    "id": 42,
    "title": "كريم سم النحل بلس",
}


class TestFinalDispatchGuard:
    def test_handoff_with_stale_markers_blocks_product_card(self):
        d = should_allow_product_attachment_dispatch(
            brain_action=ACTION_HANDOFF,
            intent_name="general",
            inbound_message="حولني لموظف",
            reply_text=f"{_HANDOFF_REPLY} [PRODUCT:كريم سم النحل]",
            brain_handoff=True,
        )
        assert d.allow is False
        assert d.reason == "handoff"

        cleared, reply = suppress_product_attachments(
            product_attachments=[dict(_STALE_CARD)],
            reply_text=f"{_HANDOFF_REPLY} [PRODUCT:كريم سم النحل]",
            decision=d,
            had_stale_candidates=True,
        )
        assert cleared == []
        assert "[PRODUCT:" not in reply.upper()

    def test_social_reply_with_previous_candidates_blocks(self):
        d = should_allow_product_attachment_dispatch(
            brain_action=ACTION_SOCIAL_REPLY,
            intent_name="social",
            inbound_message="كل عام وأنتم بخير",
            reply_text="كل عام وأنتم بخير 🌹",
        )
        assert d.allow is False
        assert d.reason == "social"

        cleared, _ = suppress_product_attachments(
            product_attachments=[dict(_STALE_CARD)],
            reply_text="كل عام وأنتم بخير 🌹",
            decision=d,
            had_stale_candidates=True,
        )
        assert cleared == []

    def test_active_order_fulfillment_blocks_marker_leakage(self):
        d = should_allow_product_attachment_dispatch(
            brain_action="llm_reply",
            intent_name="general",
            inbound_message="https://maps.app.goo.gl/abc — أبغى الطلبية تجي هنا",
            reply_text="تمام وصل الموقع",
            fulfillment_discovery_blocked=True,
            brain_state={
                "stage": "ordering",
                "order_prep": {"product_id": "ext-1", "order_status": "awaiting_address"},
                "current_product_focus": {"title": "عسل سدر", "id": 101},
            },
            active_order_state={"product_id": "ext-1"},
        )
        assert d.allow is False
        assert d.reason == "fulfillment_lock"

        cleared, _ = suppress_product_attachments(
            product_attachments=[dict(_STALE_CARD)],
            reply_text="[PRODUCT:عسل سدر]",
            decision=d,
            had_stale_candidates=True,
        )
        assert cleared == []

    def test_unknown_clarify_with_top_products_blocked(self):
        d = should_allow_product_attachment_dispatch(
            brain_action=ACTION_CLARIFY,
            intent_name="unknown",
            inbound_message="؟",
            reply_text="وش تقصد بالضبط؟",
        )
        assert d.allow is False
        assert d.reason == "clarify"

    def test_explicit_product_ask_allows_card(self):
        d = should_allow_product_attachment_dispatch(
            brain_action=ACTION_SEARCH_PRODUCTS,
            intent_name="ask_product",
            intent_confidence=0.92,
            inbound_message="أبغى عسل سدر",
            reply_text="عندنا عسل سدر ممتاز",
        )
        assert d.allow is True
        assert d.reason in {"commerce_action", "positive_commerce_intent", "visual_product_intent"}

    def test_explicit_browse_allows_limited_cards(self):
        d = should_allow_product_attachment_dispatch(
            brain_action=ACTION_SEARCH_PRODUCTS,
            intent_name="general",
            inbound_message="وش عندكم؟",
            reply_text="تفضل أبرز منتجاتنا",
        )
        assert d.allow is True
        assert d.reason in {"commerce_action", "explicit_browse"}

    def test_team_notification_reply_blocks_even_without_handoff_action(self):
        d = should_allow_product_attachment_dispatch(
            brain_action="llm_reply",
            intent_name="general",
            inbound_message="عندي استفسار",
            reply_text=_HANDOFF_REPLY,
            brain_handoff=False,
        )
        assert d.allow is False
        assert d.reason == "handoff"
