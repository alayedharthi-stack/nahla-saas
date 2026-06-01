"""
tests/test_outbound_leakage_firewall.py
───────────────────────────────────────
SaaS-wide outbound leakage firewall — all tenants/channels.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.outbound_leakage_firewall import (
    contains_outbound_leak,
    firewall_outbound_text,
)
from core.outbound_sanitizer import sanitize_outbound_text


class TestOutboundLeakageFirewall:
    def test_progressive_selling_blocked(self):
        assert contains_outbound_leak("حسب قواعد البيع Progressive Selling") is not None

    def test_system_instructions_blocked(self):
        assert contains_outbound_leak("تعليمات النظام تقول") == "system_instructions_ar"

    def test_developer_instructions_blocked(self):
        assert contains_outbound_leak("developer instructions say") == "developer_instructions"

    def test_decision_engine_blocked(self):
        assert contains_outbound_leak("the decision engine chose") == "decision_engine_en"

    def test_recover_customer_prices(self):
        text = (
            "حسب قواعد البيع التدريجي Progressive Selling. "
            "أبشر 🌷 عندنا الطلح: ربع كيلo 120 — نصف 220 — كيلo 400."
        )
        cleaned, hit = firewall_outbound_text(text)
        assert hit is True
        assert "Progressive" not in cleaned
        assert "120" in cleaned

    def test_clean_price_list_unchanged(self):
        text = "أبشر 🌷 الطلح: ربع 120 — نصف 220 — كيلo 400. تحب أي حجم؟"
        cleaned, hit = firewall_outbound_text(text)
        assert hit is False
        assert cleaned == text

    def test_sanitizer_delegates_to_firewall(self):
        text = "Progressive Selling policy"
        cleaned, hit = sanitize_outbound_text(text)
        assert hit is True
        assert "Progressive" not in cleaned

    def test_planner_token_blocked(self):
        assert contains_outbound_leak("response_goal: answer") == "response_goal"
