"""Tests for marketing emoji policy — metadata-driven outbound emoji polish."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.postprocess.marketing_emoji_policy import (  # noqa: E402
    MarketingEmojiContext,
    PURPOSE_CATALOG_BROWSE,
    PURPOSE_CONFIRMED_SUCCESS,
    PURPOSE_GREETING,
    PURPOSE_ORDER_OR_CART,
    PURPOSE_RECEIPT_REVIEW,
    PURPOSE_SHIPMENT_TRACKING,
    apply_marketing_emoji_policy,
    build_marketing_emoji_context,
    resolve_marketing_emoji_style_mode,
    resolve_message_purpose,
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _count(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def _assert_protected_span_intact(reply: str, span: str) -> None:
    assert span in reply
    start = reply.index(span)
    end = start + len(span)
    assert not _EMOJI_RE.search(reply[start:end])


def _assert_append_only_first_line(result_reply: str, original: str) -> None:
    orig_lines = original.split("\n", 1)
    new_lines = result_reply.split("\n", 1)
    orig_first = orig_lines[0].rstrip()
    new_first = new_lines[0]
    assert new_first.startswith(orig_first)
    tail = new_first[len(orig_first):].lstrip()
    if tail:
        assert _EMOJI_RE.sub("", tail).strip() == ""
    if len(orig_lines) > 1:
        assert new_lines[1] == orig_lines[1]


def _checkout_ctx(**kwargs) -> MarketingEmojiContext:
    base = {
        "policy_enabled": True,
        "audit_only": False,
        "locale": "ar",
        "style_mode": "light",
        "reply_instruction_path": "order_slot_prompt",
        "decision_action": "propose_draft_order",
        "inbound_text": "مكة",
    }
    base.update(kwargs)
    return MarketingEmojiContext(**base)


def _ctx(**kwargs) -> MarketingEmojiContext:
    base = {
        "policy_enabled": True,
        "audit_only": False,
        "locale": "ar",
        "style_mode": "light",
        "inbound_text": "مرحبا",
    }
    base.update(kwargs)
    return MarketingEmojiContext(**base)


class TestStyleModeResolution:
    def test_explicit_marketing_emoji_mode(self) -> None:
        assert resolve_marketing_emoji_style_mode({"marketing_emoji_mode": "formal"}) == "formal"

    def test_reply_tone_fallback(self) -> None:
        assert resolve_marketing_emoji_style_mode({"reply_tone": "رسمي"}) == "formal"
        assert resolve_marketing_emoji_style_mode({"reply_tone": "marketing"}) == "marketing"


class TestPurposeResolution:
    def test_payment_evidence_soft_ack(self) -> None:
        ctx = _ctx(reply_instruction_path="payment_evidence_soft_ack")
        assert resolve_message_purpose(ctx, "وصل الإيصال") == PURPOSE_RECEIPT_REVIEW

    def test_catalog_navigate_action(self) -> None:
        ctx = _ctx(decision_action="catalog_navigate")
        assert resolve_message_purpose(ctx, "اختر من الكتالوج") == PURPOSE_CATALOG_BROWSE

    def test_start_order_action(self) -> None:
        ctx = _ctx(decision_action="start_order")
        assert resolve_message_purpose(ctx, "خلنا نبدأ الطلب") == PURPOSE_ORDER_OR_CART

    def test_track_order_with_shipment_evidence(self) -> None:
        ctx = _ctx(
            decision_action="track_order",
            shipment_evidence_ok=True,
        )
        assert resolve_message_purpose(ctx, "تتبع الشحنة") == PURPOSE_SHIPMENT_TRACKING


class TestEmojiLimits:
    def test_short_reply_adds_at_most_one(self) -> None:
        reply = "مرحبا، كيف أقدر أساعدك؟"
        ctx = _ctx(decision_action="greet")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert _count(result.reply) <= 1

    def test_long_reply_respects_cap(self) -> None:
        reply = (
            "هذه قائمة منتجاتنا المتنوعة.\n"
            "تقدر تختار من الأقسام الموجودة.\n"
            "وإذا احتجت مساعدة أنا هنا.\n"
            "الأسعار واضحة في الكتالوج.\n"
            "وش تفضل اليوم؟"
        )
        ctx = _ctx(
            decision_action="catalog_navigate",
            style_mode="marketing",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert _count(result.reply) <= 3


class TestOperationalSafety:
    def test_no_checkmark_on_payment_pending(self) -> None:
        reply = "بانتظار إيصال التحويل"
        ctx = _ctx(
            payment_evidence_status="needs_confirmation",
            awaiting_payment_receipt=True,
            style_mode="marketing",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "✅" not in result.reply

    def test_no_truck_without_shipment_evidence(self) -> None:
        reply = "طلبك قيد المراجعة"
        ctx = _ctx(
            decision_action="track_order",
            shipment_evidence_ok=False,
            style_mode="marketing",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "🚚" not in result.reply

    def test_checkmark_only_when_confirmed(self) -> None:
        reply = "تم استلام الدفع بنجاح"
        ctx = _ctx(
            payment_evidence_status="confirmed",
            payment_receipt_received=True,
            style_mode="marketing",
        )
        purpose = resolve_message_purpose(ctx, reply)
        assert purpose == PURPOSE_CONFIRMED_SUCCESS
        result = apply_marketing_emoji_policy(reply, ctx)
        if result.changed:
            assert "✅" in result.reply

    def test_no_fire_without_offer_evidence(self) -> None:
        reply = "تصفح منتجاتنا الجديدة"
        ctx = _ctx(
            decision_action="catalog_navigate",
            has_offer_evidence=False,
            style_mode="marketing",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "🔥" not in result.reply


class TestProtectedSpans:
    def test_url_preserved(self) -> None:
        url = "https://store.example.com/catalog"
        reply = f"تصفح الكتالوج من هنا {url}"
        ctx = _ctx(decision_action="catalog_navigate")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert url in result.reply

    def test_price_preserved(self) -> None:
        reply = "سعر المنتج 120 ر.س"
        ctx = _ctx(decision_action="start_order")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "120 ر.س" in result.reply

    def test_iban_preserved(self) -> None:
        iban = "SA03 8000 0000 6080 1016 7519"
        reply = f"حول على الآيبان {iban}"
        ctx = _ctx(
            reply_instruction_path="payment_method_ack",
            payment_evidence_status="awaiting_payment",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "SA03" in result.reply


class TestCheckoutProtectedSpansAppendOnly:
    """Checkout replies: protected tokens stay intact; emoji append-only at line end."""

    def test_short_address_code_append_only(self) -> None:
        code = "MDQA5061"
        reply = f"تمام، رمز عنوانك {code} محفوظ عندنا"
        result = apply_marketing_emoji_policy(reply, _checkout_ctx())
        _assert_protected_span_intact(result.reply, code)
        _assert_append_only_first_line(result.reply, reply)

    def test_google_maps_url_append_only(self) -> None:
        url = "https://maps.app.goo.gl/abc123xyz"
        reply = f"تمام، شاركنا موقعك عبر {url}"
        result = apply_marketing_emoji_policy(reply, _checkout_ctx())
        _assert_protected_span_intact(result.reply, url)
        _assert_append_only_first_line(result.reply, reply)

    def test_price_append_only_within_light_cap(self) -> None:
        price = "319 ر.س"
        reply = f"تمام، إجمالي طلبك {price} قبل الشحن"
        result = apply_marketing_emoji_policy(reply, _checkout_ctx())
        _assert_protected_span_intact(result.reply, price)
        _assert_append_only_first_line(result.reply, reply)
        assert _count(result.reply) <= 1


class TestFeatureAndStyleGates:
    def test_policy_disabled(self) -> None:
        reply = "أهلاً وسهلاً"
        ctx = _ctx(decision_action="greet", policy_enabled=False)
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.reply == reply
        assert result.blocked_reason == "policy_disabled"

    def test_style_none(self) -> None:
        reply = "أهلاً وسهلاً"
        ctx = _ctx(decision_action="greet", style_mode="none")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.reply == reply
        assert result.blocked_reason == "style_none"

    def test_audit_only_logs_without_mutating(self) -> None:
        reply = "أهلاً وسهلاً"
        ctx = _ctx(decision_action="greet", audit_only=True)
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.reply == reply
        assert result.blocked_reason == "audit_only"
        assert result.selected_emojis


class TestPurposeBuckets:
    def test_greeting_adds_emoji(self) -> None:
        reply = "أهلاً وسهلاً"
        ctx = _ctx(decision_action="greet")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.purpose == PURPOSE_GREETING
        if result.changed:
            assert _count(result.reply) >= 1

    def test_catalog_browse(self) -> None:
        reply = "اختر من الكتالوج"
        ctx = _ctx(decision_action="catalog_navigate")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.purpose == PURPOSE_CATALOG_BROWSE

    def test_pure_greeting_with_stale_checkout_stage_gets_no_cart_emoji(self) -> None:
        reply = "وعليكم السلام"
        ctx = _ctx(
            inbound_text="السلام عليكم",
            decision_action="llm_reply",
            stage="checkout",
        )
        result = apply_marketing_emoji_policy(reply, ctx)
        assert "🛒" not in result.reply

    def test_address_request(self) -> None:
        reply = "شاركنا موقعك"
        ctx = _ctx(reply_instruction_path="map_image_ack")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.purpose == "address_request"

    def test_support_handoff(self) -> None:
        reply = "بحولك للدعم"
        ctx = _ctx(decision_action="handoff_to_human")
        result = apply_marketing_emoji_policy(reply, ctx)
        assert result.purpose == "support"


class TestButtonsBoundary:
    """Policy only touches body text — button payloads are separate."""

    def test_body_change_does_not_imply_button_mutation(self) -> None:
        reply = "اختر المنتج"
        buttons = [
            {"type": "reply", "reply": {"id": "prod_123", "title": "عسل سدر"}},
            {"type": "reply", "reply": {"id": "prod_456", "title": "قهوة"}},
        ]
        ctx = _ctx(decision_action="catalog_navigate")
        result = apply_marketing_emoji_policy(reply, ctx)
        # Buttons remain untouched — policy has no access to them.
        assert buttons[0]["reply"]["id"] == "prod_123"
        assert buttons[1]["reply"]["title"] == "قهوة"
        if result.changed:
            assert result.reply != reply


class TestBuildContext:
    def test_build_from_metadata(self) -> None:
        ctx = build_marketing_emoji_context(
            tenant_id=33,
            decision_action="catalog_navigate",
            decision_args={"discovery_output_kind": "offer"},
            reply_text="عرض خاص",
            policy_enabled=True,
        )
        assert ctx.has_offer_evidence
        assert ctx.style_mode in {"light", "marketing", "formal", "none"}

    def test_config_flag_importable_and_enabled_by_default(self) -> None:
        from core.config import (  # noqa: PLC0415
            MARKETING_EMOJI_POLICY_AUDIT_ONLY,
            MARKETING_EMOJI_POLICY_ENABLED,
        )

        assert MARKETING_EMOJI_POLICY_ENABLED is True
        assert MARKETING_EMOJI_POLICY_AUDIT_ONLY is False
