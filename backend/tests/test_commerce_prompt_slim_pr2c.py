"""PR2C — commerce prompt slim payload tests."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict

import pytest

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.compose.prompt_state_serializer import (
    is_commerce_prompt_slim_enabled,
    serialize_commerce_brain_state,
    should_apply_commerce_prompt_slim,
    slim_ai_settings_for_commerce_prompt,
)
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY
from modules.ai.brain.types import (
    BrainReplyState,
    INTENT_ASK_PRODUCT,
    INTENT_SOLUTION_SEEKING_COMMERCE,
)
from modules.ai.orchestrator.llm_cost_audit import build_brain_compose_audit_extra

_LARGE_KB = "حقائق المتجر التفصيلية.\n" * 2500
_LARGE_MANUAL = "نص معرفة يدوي طويل.\n" * 2000
_LARGE_RESOLVER = "بروتوكول طويل.\n" * 800
_CUSTOMER_MSG = "هل عندكم عسل طلح؟"


def _heavy_commerce_state(**overrides) -> BrainReplyState:
    base = dict(
        store_name="متجر العسل",
        intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
        need_based_advice_mode=True,
        need_category="general_attribute",
        primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        stage="discovery",
        response_goal="answer product availability",
        selected_product={"id": 11, "title": "عسل طلح", "price": 120, "available": True},
        known_facts={
            "checkout_preparation": {
                "order_status": "none",
                "awaiting_payment_receipt": False,
            },
            "availability": {"in_stock": True, "product_title": "عسل طلح"},
        },
        recent_turns=["مرحبا", "عندكم عسل؟", "طلح تحديداً"],
        merchant_context={
            "tenant_id": 7,
            "tenant_id_alt_should_not_matter": 33,
            "structured_facts_block": _LARGE_KB,
            "structured_behavior_block": "قواعد سلوك.\n" * 400,
            "products": [
                {"id": i, "title": f"منتج {i}", "price": i * 10, "description": "x" * 200}
                for i in range(1, 9)
            ],
            "resolver_overlay": _LARGE_RESOLVER + "\nأدوات الوسائط المتوفرة في هذا المتجر:\n[MEDIA_KEY:pay]",
            "ai_settings": {
                "reply_tone": "friendly",
                "default_language": "ar",
                "reply_length": "medium",
                "manual_knowledge_base": _LARGE_MANUAL,
                "owner_instructions": "تعليمات طويلة.\n" * 300,
            },
            "brain_profile": {"autopilot_enabled": True, "orderable": True, "tenant_id": 7},
            "payment_enabled": True,
            "shipping_enabled": True,
        },
    )
    base.update(overrides)
    return BrainReplyState(**base)


@pytest.fixture
def slim_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_MAX_CHARS", "25000")
    monkeypatch.setenv("NAHLA_COMMERCE_KB_MAX_CHARS", "3500")
    monkeypatch.setenv("NAHLA_MAX_KB_PROMPT_CHARS", "12000")


class TestCommercePromptSlimFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", raising=False)
        assert is_commerce_prompt_slim_enabled() is False
        assert not should_apply_commerce_prompt_slim(
            _heavy_commerce_state(),
        )

    def test_enabled_for_solution_seeking(self, slim_enabled: None) -> None:
        assert should_apply_commerce_prompt_slim(_heavy_commerce_state())


class TestPromptSizeReduction:
    def test_slim_reduces_prompt_size_significantly(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        heavy = build_brain_reply_prompt(state)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", raising=False)
        try:
            legacy = build_brain_reply_prompt(state)
        finally:
            monkeypatch.undo()

        assert len(slim := heavy) < len(legacy) * 0.6
        assert len(slim) < 25_000

    def test_solution_seeking_meets_target_threshold(self, slim_enabled: None) -> None:
        prompt = build_brain_reply_prompt(_heavy_commerce_state())
        assert len(prompt) < 25_000
        assert len(prompt) // 4 < 7_000


class TestPayloadContents:
    def test_does_not_send_full_ai_settings_in_json(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        state_dict = asdict(state)
        state_dict.pop("tenant_overlay", None)
        slim = serialize_commerce_brain_state(
            state_dict,
            state,
            kb_in_prompt_block=True,
        )
        mc = slim.get("merchant_context") or {}
        assert "ai_settings" not in mc
        dumped = json.dumps(slim, ensure_ascii=False)
        assert "manual_knowledge_base" not in dumped
        assert "owner_instructions" not in dumped

    def test_essential_product_and_availability_facts_remain(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        slim = serialize_commerce_brain_state(
            asdict(state),
            state,
            kb_in_prompt_block=True,
        )
        assert slim.get("selected_product", {}).get("title") == "عسل طلح"
        facts = slim.get("known_facts") or {}
        assert facts.get("availability", {}).get("in_stock") is True
        products = (slim.get("merchant_context") or {}).get("products") or []
        assert products and products[0].get("title")

    def test_payment_fulfillment_flags_remain(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        slim = serialize_commerce_brain_state(
            asdict(state),
            state,
            kb_in_prompt_block=True,
        )
        mc = slim.get("merchant_context") or {}
        assert mc.get("payment_enabled") is True
        assert mc.get("shipping_enabled") is True

    def test_recent_turns_capped_to_two(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        slim = serialize_commerce_brain_state(
            asdict(state),
            state,
            kb_in_prompt_block=True,
        )
        assert len(slim.get("recent_turns") or []) <= 2

    def test_high_priority_uses_slim_settings_only(self, slim_enabled: None) -> None:
        settings = (_heavy_commerce_state().merchant_context or {})["ai_settings"]
        slim = slim_ai_settings_for_commerce_prompt(settings)
        assert "manual_knowledge_base" not in slim
        assert slim.get("reply_tone") == "friendly"
        prompt = build_brain_reply_prompt(_heavy_commerce_state())
        assert "نص معرفة يدوي طويل" not in prompt


class TestAuditSafety:
    def test_no_customer_content_in_contributors_log(
        self,
        slim_enabled: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="nahla.ai.commerce_prompt_slim")
        build_brain_reply_prompt(_heavy_commerce_state())
        joined = "\n".join(r.message for r in caplog.records)
        assert "[COMMERCE_PROMPT_CONTRIBUTORS]" in joined
        assert _CUSTOMER_MSG not in joined

    def test_slim_applied_audit_log(self, slim_enabled: None, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="nahla.ai.commerce_prompt_slim")
        build_brain_reply_prompt(_heavy_commerce_state())
        joined = "\n".join(r.message for r in caplog.records)
        assert "[COMMERCE_PROMPT_SLIM_APPLIED]" in joined
        assert "removed_ai_settings" in joined
        assert _CUSTOMER_MSG not in joined

    def test_audit_extra_marks_slim_enabled(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state()
        prompt = build_brain_reply_prompt(state)
        extra = build_brain_compose_audit_extra(
            reply_state=state,
            prompt=prompt,
            history_messages=[{"role": "user", "content": _CUSTOMER_MSG}],
            tenant_id=7,
            conversation_id=1,
            turn_id=1,
        )
        assert extra.get("commerce_prompt_slim") is True
        assert _CUSTOMER_MSG not in json.dumps(extra, ensure_ascii=False)


class TestNoTenantSpecialCase:
    def test_same_slim_shape_for_tenant_7_and_33(self, slim_enabled: None) -> None:
        s7 = serialize_commerce_brain_state(
            asdict(_heavy_commerce_state(merchant_context={
                **_heavy_commerce_state().merchant_context,
                "tenant_id": 7,
            })),
            _heavy_commerce_state(merchant_context={
                **_heavy_commerce_state().merchant_context,
                "tenant_id": 7,
            }),
            kb_in_prompt_block=True,
        )
        s33 = serialize_commerce_brain_state(
            asdict(_heavy_commerce_state(merchant_context={
                **_heavy_commerce_state().merchant_context,
                "tenant_id": 33,
            })),
            _heavy_commerce_state(merchant_context={
                **_heavy_commerce_state().merchant_context,
                "tenant_id": 33,
            }),
            kb_in_prompt_block=True,
        )
        assert set((s7.get("merchant_context") or {}).keys()) == set(
            (s33.get("merchant_context") or {}).keys()
        )

    def test_ask_product_intent_also_slims(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state(
            intent_name=INTENT_ASK_PRODUCT,
            need_based_advice_mode=False,
        )
        prompt = build_brain_reply_prompt(state)
        assert len(prompt) < 25_000


class TestNeedBasedDiscoveryPath:
    def test_discovery_without_selected_product_meets_target(self, slim_enabled: None) -> None:
        state = _heavy_commerce_state(selected_product=None)
        prompt = build_brain_reply_prompt(state)
        assert len(prompt) < 25_000
        assert len(prompt) // 4 < 7_000
        assert "manual_knowledge_base" not in prompt

    def test_commerce_lite_applies_for_need_based_when_flag_on(self, slim_enabled: None) -> None:
        from modules.ai.brain.compose.prompt_payload_slim import should_apply_commerce_lite

        state = _heavy_commerce_state()
        assert should_apply_commerce_lite(state) is True
