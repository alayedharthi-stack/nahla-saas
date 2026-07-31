"""
tests/test_slot_extractor.py
─────────────────────────────
Unit tests for slot_extractor — focuses on the P0 fix:
  • Complex multi-field messages no longer cause JSON truncation / empty slots
  • _repair_json() salvages partially-truncated JSON
  • Deterministic extraction still works without an API key
  • Compact output merging strips empty values
"""
from __future__ import annotations

import json
import sys
import os
import asyncio
from typing import Any, Dict
from unittest.mock import patch

import pytest

# ── path bootstrap ────────────────────────────────────────────────────────────
_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.intent.slot_extractor import (
    _extract_deterministic_slots,
    _repair_json,
    extract_slots,
)

_OPENAI_PROVIDER_CALL = (
    "modules.ai.orchestrator.providers.openai_compatible_provider."
    "OpenAICompatibleProvider.call"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    # Fresh loop per call — get_event_loop() fails on Python 3.12+ after
    # prior tests (e.g. test_salla_pages_sync) close the default loop.
    return asyncio.run(coro)


def _mock_openai_slot_response(json_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a dict mimicking OpenAICompatibleProvider.call for a slot payload."""
    return {
        "reply_text": json.dumps(json_payload, ensure_ascii=False),
        "model": "gpt-5.6-luna",
        "provider": "openai_compatible",
    }


# ── _repair_json ──────────────────────────────────────────────────────────────

class TestRepairJson:
    def test_clean_json_unchanged(self):
        raw = '{"city": "الرياض", "intent_hint": "start_order"}'
        result = _repair_json(raw)
        assert result == {"city": "الرياض", "intent_hint": "start_order"}

    def test_truncated_mid_string_value(self):
        # Simulate max_tokens cut mid-value: "الري" is incomplete
        raw = '{"customer_name": "محمد", "city": "الري'
        result = _repair_json(raw)
        # Should at minimum recover customer_name
        assert result is not None
        assert result.get("customer_name") == "محمد"

    def test_truncated_after_last_comma(self):
        raw = '{"city": "الرياض", "district": "النخيل", "str'
        result = _repair_json(raw)
        assert result is not None
        assert result.get("city") == "الرياض"

    def test_completely_invalid_returns_none(self):
        result = _repair_json("not json at all")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _repair_json("")
        assert result is None

    def test_closing_brace_only_needed(self):
        raw = '{"intent_hint": "general"'
        result = _repair_json(raw)
        assert result is not None
        assert result.get("intent_hint") == "general"


# ── _extract_deterministic_slots ──────────────────────────────────────────────

class TestDeterministicSlots:
    def test_extracts_short_address_code(self):
        slots = _extract_deterministic_slots("رمزي RIYD2342")
        assert slots.get("short_address_code") == "RIYD2342"

    def test_extracts_email(self):
        slots = _extract_deterministic_slots("بريدي هو test@example.com")
        assert slots.get("customer_email") == "test@example.com"

    def test_extracts_google_maps_url(self):
        url = "https://maps.app.goo.gl/abc123"
        slots = _extract_deterministic_slots(f"موقعي {url}")
        assert slots.get("google_maps_url") == url

    def test_empty_message(self):
        slots = _extract_deterministic_slots("")
        assert slots == {}

    def test_no_signals(self):
        slots = _extract_deterministic_slots("شكراً جزيلاً")
        assert slots == {}


# ── extract_slots — integration mocks ────────────────────────────────────────

class TestExtractSlots:
    """Tests that mock OpenAI-compatible provider to verify the full pipeline."""

    def test_no_api_key_returns_deterministic(self):
        """Without an API key, only deterministic slots come back."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            result = _run(extract_slots("اسمي محمد RIYD1234", []))
        assert result.get("short_address_code") == "RIYD1234"
        # LLM-only fields should be absent
        assert "customer_name" not in result

    def test_multi_field_message_all_fields_extracted(self):
        """Core P0 regression test: complex single message with many fields."""
        llm_payload = {
            "customer_name": "محمد أحمد",
            "customer_first_name": "محمد",
            "customer_last_name": "أحمد",
            "city": "الرياض",
            "district": "النخيل",
            "short_address_code": "RIAD1234",
            "intent_hint": "start_order",
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                return_value=_mock_openai_slot_response(llm_payload),
            ):
                result = _run(
                    extract_slots(
                        "اسمي محمد أحمد، من الرياض، حي النخيل، رمزي RIAD1234",
                        [],
                    )
                )

        assert result.get("customer_name") == "محمد أحمد"
        assert result.get("city") == "الرياض"
        assert result.get("district") == "النخيل"
        assert result.get("short_address_code") == "RIAD1234"
        assert result.get("intent_hint") == "start_order"

    def test_compact_output_no_empty_fields_in_result(self):
        """LLM compact output should not pollute result with empty strings."""
        llm_payload = {
            "city": "جدة",
            "intent_hint": "start_order",
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                return_value=_mock_openai_slot_response(llm_payload),
            ):
                result = _run(extract_slots("أنا من جدة", []))

        assert result.get("city") == "جدة"
        # Empty fields must be absent from result entirely (compact output)
        assert "customer_name" not in result
        assert "product_query" not in result

    def test_deterministic_wins_over_llm_for_address_code(self):
        """Deterministic regex override: if LLM and regex both find address code, regex wins."""
        llm_payload = {
            "short_address_code": "WRONG123",  # LLM got it wrong
            "city": "مكة",
            "intent_hint": "start_order",
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                return_value=_mock_openai_slot_response(llm_payload),
            ):
                result = _run(extract_slots("رمزي MKAH5678 مكة", []))

        # Deterministic extractor found MKAH5678 — it should override LLM's WRONG123
        assert result.get("short_address_code") == "MKAH5678"

    def test_truncated_json_recovered_via_repair(self):
        """If LLM returns truncated JSON, _repair_json recovers partial data."""
        truncated_raw = '{"customer_name": "سارة", "city": "الد'

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                return_value={"reply_text": truncated_raw},
            ):
                result = _run(extract_slots("اسمي سارة من الد...", []))

        # customer_name was fully present before the cut
        assert result.get("customer_name") == "سارة"

    def test_timeout_falls_back_to_deterministic(self):
        """A TimeoutError returns deterministic slots, not an exception."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(_OPENAI_PROVIDER_CALL, side_effect=TimeoutError("timeout")):
                result = _run(extract_slots("ABCD1234 ارسل", []))

        # Deterministic should still give us the address code
        assert result.get("short_address_code") == "ABCD1234"

    def test_api_exception_falls_back_gracefully(self):
        """Any API error returns deterministic slots without raising."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                side_effect=RuntimeError("network error"),
            ):
                result = _run(extract_slots("مرحباً", []))

        # No exception, empty dict or partial is fine
        assert isinstance(result, dict)

    def test_order_id_extracted(self):
        """order_id slot is captured when customer mentions an order number."""
        llm_payload = {
            "order_id": "12345",
            "intent_hint": "track_order",
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                _OPENAI_PROVIDER_CALL,
                return_value=_mock_openai_slot_response(llm_payload),
            ):
                result = _run(extract_slots("ما حال طلبي رقم 12345", []))

        assert result.get("order_id") == "12345"
        assert result.get("intent_hint") == "track_order"
