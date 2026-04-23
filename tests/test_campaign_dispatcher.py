"""
tests/test_campaign_dispatcher.py
─────────────────────────────────
Unit tests for _build_send_payload, validate_template_payload,
and button parameter handling.
"""
from __future__ import annotations

import os
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, "..", "backend"))
_DB = os.path.abspath(os.path.join(_BACKEND, "..", "database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
from unittest.mock import MagicMock
from services.campaign_dispatcher import (
    _build_send_payload,
    _button_needs_param,
    _extract_param_count,
    validate_template_payload,
)


def _make_template(components, name="test_tpl", language="ar"):
    tpl = MagicMock()
    tpl.name = name
    tpl.language = language
    tpl.components = components
    return tpl


# ── _extract_param_count ────────────────────────────────────────────────────

class TestExtractParamCount:
    def test_empty(self):
        assert _extract_param_count("") == 0
        assert _extract_param_count(None) == 0

    def test_one_param(self):
        assert _extract_param_count("hello {{1}}") == 1

    def test_three_params(self):
        assert _extract_param_count("{{1}} and {{2}} and {{3}}") == 3

    def test_non_sequential(self):
        assert _extract_param_count("{{1}} and {{5}}") == 5


# ── _button_needs_param ─────────────────────────────────────────────────────

class TestButtonNeedsParam:
    def test_copy_code(self):
        assert _button_needs_param({"type": "COPY_CODE"}) is True

    def test_url_with_var(self):
        assert _button_needs_param({"type": "URL", "url": "https://x.com/{{1}}"}) is True

    def test_url_with_example_but_static(self):
        assert _button_needs_param({"type": "URL", "url": "https://x.com/", "example": ["https://x.com/shop"]}) is False

    def test_url_static(self):
        assert _button_needs_param({"type": "URL", "url": "https://x.com/"}) is False

    def test_quick_reply(self):
        assert _button_needs_param({"type": "QUICK_REPLY"}) is False

    def test_otp(self):
        assert _button_needs_param({"type": "OTP"}) is True


# ── _build_send_payload — BODY only ─────────────────────────────────────────

class TestBuildPayloadBodyOnly:
    def test_body_two_params(self):
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا {{1}} من {{2}}"},
            {"type": "FOOTER", "text": "نحلة"},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 1
        assert comps[0]["type"] == "body"
        assert len(comps[0]["parameters"]) == 2
        assert comps[0]["parameters"][0]["text"] == "أحمد"
        assert comps[0]["parameters"][1]["text"] == "المتجر"

    def test_body_no_params(self):
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا بك"},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 0


# ── _build_send_payload — BODY + BUTTONS ────────────────────────────────────

class TestBuildPayloadWithButtons:
    def test_copy_code_button(self):
        tpl = _make_template([
            {"type": "BODY", "text": "عرض خاص {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["VIP30"]},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
            coupon_code="DISCOUNT20",
        )
        comps = result["template"]["components"]
        assert len(comps) == 2  # body + button
        btn_comp = [c for c in comps if c["type"] == "button"][0]
        assert btn_comp["sub_type"] == "copy_code"
        assert btn_comp["index"] == "0"
        assert btn_comp["parameters"][0]["coupon_code"] == "DISCOUNT20"

    def test_url_button_with_var(self):
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "URL", "text": "تسوق", "url": "https://shop.com/{{1}}"},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
            cart_url="cart/123",
        )
        comps = result["template"]["components"]
        btn_comp = [c for c in comps if c["type"] == "button"][0]
        assert btn_comp["sub_type"] == "url"
        assert btn_comp["parameters"][0]["text"] == "cart/123"

    def test_static_url_with_example_no_param(self):
        """Static URL button (no {{1}}) should NOT get a parameter even
        if it has an 'example' field — example is for Meta review only."""
        tpl = _make_template([
            {"type": "BODY", "text": "عرض {{1}} من {{2}}"},
            {"type": "FOOTER", "text": "نحلة"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["VIP30"]},
                {"type": "URL", "text": "تسوق", "url": "https://x.com/", "example": ["https://x.com/shop"]},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
            coupon_code="CODE50",
        )
        comps = result["template"]["components"]
        assert len(comps) == 2  # body + copy_code (NO url param for static)
        types = {c.get("sub_type") or c["type"] for c in comps}
        assert "copy_code" in types
        assert "body" in types

    def test_static_url_button_no_param(self):
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "URL", "text": "موقعنا", "url": "https://shop.com/"},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 1  # body only, no button param needed

    def test_quick_reply_no_param(self):
        tpl = _make_template([
            {"type": "BODY", "text": "هل تريد مساعدة {{1}}؟"},
            {"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": "نعم"},
                {"type": "QUICK_REPLY", "text": "لا"},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 1  # body only


# ── validate_template_payload ───────────────────────────────────────────────

class TestValidatePayload:
    def test_body_only_ok(self):
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا {{1}}"},
        ])
        assert validate_template_payload(tpl, coupon_code="") == []

    def test_copy_code_no_coupon_warns(self):
        tpl = _make_template([
            {"type": "BODY", "text": "عرض {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["VIP"]},
            ]},
        ])
        issues = validate_template_payload(tpl, coupon_code="")
        assert len(issues) == 1
        assert "كود خصم" in issues[0]

    def test_copy_code_with_coupon_ok(self):
        tpl = _make_template([
            {"type": "BODY", "text": "عرض {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["VIP"]},
            ]},
        ])
        issues = validate_template_payload(tpl, coupon_code="SAVE20")
        assert len(issues) == 0

    def test_otp_unsupported(self):
        tpl = _make_template([
            {"type": "BODY", "text": "كود {{1}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "OTP"},
            ]},
        ])
        issues = validate_template_payload(tpl, coupon_code="")
        assert len(issues) == 1
        assert "غير مدعوم" in issues[0]


# ── HEADER params ───────────────────────────────────────────────────────────

class TestBuildPayloadHeader:
    def test_header_with_params(self):
        tpl = _make_template([
            {"type": "HEADER", "format": "TEXT", "text": "عرض {{1}}"},
            {"type": "BODY", "text": "مرحبا {{1}} من {{2}}"},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 2
        header_comp = [c for c in comps if c["type"] == "header"][0]
        assert len(header_comp["parameters"]) == 1

    def test_static_header_no_params(self):
        tpl = _make_template([
            {"type": "HEADER", "format": "TEXT", "text": "مرحبا بك"},
            {"type": "BODY", "text": "عرض {{1}}"},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
        )
        comps = result["template"]["components"]
        assert len(comps) == 1  # body only


# ── Full VIP template (matches production) ──────────────────────────────────

class TestVipExclusiveTemplate:
    """Reproduce the exact production template that was failing."""

    def test_vip_exclusive_full(self):
        """Exact reproduction of the production vip_exclusive template.
        URL is static (no {{1}}) so only BODY + COPY_CODE components."""
        tpl = _make_template([
            {"type": "BODY", "text": (
                "أنت من عملائنا المميزين يا {{1}} 👑\n\n"
                "شكراً لولائك لمتجر {{2}}!\n\n"
                "هذا كود خاص جداً — مخصص لك وحدك — انسخه واستمتع بخصمك الحصري:"
            ), "example": {"body_text": [["وليد", "متجر الساعات"]]}},
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["VIP30"]},
                {"type": "URL", "text": "تسوق الآن",
                 "url": "https://example.com/",
                 "example": ["https://example.com/"]},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966555096501",
            customer_name="تركي الحارثي", store_name="المتجر",
            coupon_code="VIP30TURKI",
        )
        comps = result["template"]["components"]
        comp_types = [(c.get("sub_type") or c["type"]) for c in comps]
        assert "body" in comp_types, f"Missing body in {comp_types}"
        assert "copy_code" in comp_types, f"Missing copy_code in {comp_types}"
        assert len(comps) == 2  # body + copy_code; static URL gets no param

        body = [c for c in comps if c["type"] == "body"][0]
        assert body["parameters"][0]["text"] == "تركي الحارثي"
        assert body["parameters"][1]["text"] == "المتجر"

        cc = [c for c in comps if c.get("sub_type") == "copy_code"][0]
        assert cc["parameters"][0]["coupon_code"] == "VIP30TURKI"
        assert cc["index"] == "0"

    def test_dynamic_url_with_cart(self):
        """Template with URL containing {{1}} should get the cart_url."""
        tpl = _make_template([
            {"type": "BODY", "text": "مرحبا {{1}} سلتك تنتظرك"},
            {"type": "BUTTONS", "buttons": [
                {"type": "URL", "text": "أكمل الطلب",
                 "url": "https://shop.com/cart/{{1}}",
                 "example": ["https://shop.com/cart/abc"]},
            ]},
        ])
        result = _build_send_payload(
            template=tpl, to_phone="966500000000",
            customer_name="أحمد", store_name="المتجر",
            cart_url="cart/xyz123",
        )
        comps = result["template"]["components"]
        assert len(comps) == 2  # body + url
        url_btn = [c for c in comps if c.get("sub_type") == "url"][0]
        assert url_btn["parameters"][0]["text"] == "cart/xyz123"
