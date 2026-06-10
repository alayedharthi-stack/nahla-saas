"""
tests/test_presentation_mode.py
───────────────────────────────
Phase 0 + Phase 1 — PresentationMode resolver and shadow stamping.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.presentation_mode import (  # noqa: E402
    PresentationMode,
    apply_presentation_mode_shadow,
    is_presentation_mode_shadow_enabled,
    is_price_with_card_enabled,
    resolve_presentation_mode,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

_FOCUS = {
    "id": 1,
    "title": "عسل السمر 1447",
    "price": 280,
    "external_id": "ext-1",
}


def _ctx(
    message: str,
    *,
    with_focus: bool = False,
    intent_name: str | None = None,
) -> BrainContext:
    intent = rules.match(message)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.5,
            raw_message=message,
        )
    state = MerchantConversationState(greeted=True, stage="discovery")
    if with_focus:
        state.current_product_focus = dict(_FOCUS)
    return BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, product_count=10),
    )


def _decision(action: str, **args) -> Decision:
    return Decision(action=action, args=dict(args), reason="test", confidence=0.9)


class TestPresentationModeResolver:
    @pytest.mark.parametrize(
        "message",
        [
            "عسل السمر بكم الكيلو",
            "بكم الطلح",
            "كم سعر السمر",
        ],
    )
    def test_resolved_product_price_defaults_price_only(self, message: str) -> None:
        ctx = _ctx(message)
        dec = _decision(ACTION_SEARCH_PRODUCTS, query="سمر")
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.PRICE_ONLY
        assert result.evidence.get("rule") in {
            "product_price_ask_resolved",
            "price_search_shadow_default",
        }

    def test_bare_price_ask_is_discovery_list(self) -> None:
        ctx = _ctx("بكم")
        dec = _decision(ACTION_SEARCH_PRODUCTS, query="")
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.DISCOVERY_LIST

    def test_visual_request_is_visual(self) -> None:
        ctx = _ctx("ورني عسل السمر")
        dec = _decision(
            ACTION_SEARCH_PRODUCTS,
            query="سمر",
            after_search="product_visual",
        )
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.VISUAL

    def test_focus_unit_price_is_price_only(self) -> None:
        ctx = _ctx("بكم الكيلو", with_focus=True)
        dec = _decision(ACTION_LLM_REPLY, topic="price", product=dict(_FOCUS))
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.PRICE_ONLY
        assert result.evidence.get("rule") == "focus_backed_price_turn"

    def test_category_discovery_is_discovery_list(self) -> None:
        ctx = _ctx("وش عندكم من عسل")
        dec = _decision(ACTION_LLM_REPLY, topic="category_discovery")
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.DISCOVERY_LIST

    def test_price_with_card_disabled_by_default(self) -> None:
        assert is_price_with_card_enabled() is False

    def test_price_only_forbids_product_card(self) -> None:
        ctx = _ctx("بكم السمر")
        dec = _decision(ACTION_SEARCH_PRODUCTS, query="سمر")
        result = resolve_presentation_mode(ctx, decision=dec)
        assert result.mode == PresentationMode.PRICE_ONLY
        assert "product_card" in result.forbidden_attachments
        assert "none" in result.allowed_attachments


class TestPresentationModeShadow:
    def test_shadow_stamps_decision_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_PRESENTATION_MODE_SHADOW", "true")
        ctx = _ctx("عسل السمر بكم الكيلو")
        dec = _decision(ACTION_SEARCH_PRODUCTS, query="سمر")
        out = apply_presentation_mode_shadow(ctx, dec)
        assert out.action == ACTION_SEARCH_PRODUCTS
        assert out.args.get("presentation_mode") == PresentationMode.PRICE_ONLY.value
        assert out.args.get("presentation_shadow") is True
        assert "presentation_evidence" in out.args

    def test_shadow_off_leaves_decision_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_PRESENTATION_MODE_SHADOW", "false")
        ctx = _ctx("عسل السمر بكم الكيلو")
        dec = _decision(ACTION_SEARCH_PRODUCTS, query="سمر")
        out = apply_presentation_mode_shadow(ctx, dec)
        assert out.args == dec.args

    def test_shadow_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NAHLA_PRESENTATION_MODE_SHADOW", raising=False)
        assert is_presentation_mode_shadow_enabled() is True
