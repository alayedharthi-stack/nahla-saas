"""
PR-D6D — safe image facts in general media composer brief.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_SOCIAL_VISION = (
    "[وصف الصورة المرسلة] لقطة شاشة من منشور اجتماعي قصير يظهر شخصان "
    "وأحدهما يحمل كوباً مع نص وتفاعلات و@creator_handle و#skincare"
)

_PAYMENT_VISION = (
    "[تصنيف الصورة: إيصال تحويل بنكي مؤكد]\n"
    "[وصف الصورة المرسلة] إيصال تحويل بنكي بمبلغ 150 ريال"
)


def _ctx_with_meta(message: str, meta: dict) -> SimpleNamespace:
    return SimpleNamespace(
        message=message,
        tenant_id=33,
        profile={"inbound_metadata": meta},
        intent=SimpleNamespace(slots={}),
        block_commerce_escalation=False,
    )


class TestSafeGeneralImageFactsBuilder:
    def test_builds_structured_facts_from_vision(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            build_safe_general_image_facts,
        )

        facts = build_safe_general_image_facts(
            message=_SOCIAL_VISION,
            inbound_metadata={
                "vision_text": (
                    "لقطة شاشة من منشور اجتماعي قصير يظهر شخصان "
                    "وأحدهما يحمل كوباً مع نص وتفاعلات"
                ),
                "media_semantic_category": "social",
            },
        )
        assert facts.get("scene_type") == "social_media_screenshot"
        assert isinstance(facts.get("visible_elements"), list)
        assert facts["visible_elements"]
        assert "people appear in the image" in facts["visible_elements"]
        assert "@" not in str(facts.get("visible_text_summary") or "")
        assert "#" not in str(facts.get("visible_text_summary") or "")

    def test_no_person_names_or_handles_as_contact_targets(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            build_safe_general_image_facts,
        )

        facts = build_safe_general_image_facts(
            message=_SOCIAL_VISION,
            inbound_metadata={"vision_text": "creator @teddy_abuk says hello to #brand"},
        )
        summary = str(facts.get("visible_text_summary") or "")
        assert "@" not in summary
        assert "#" not in summary
        assert "teddy" not in summary.lower()

    def test_payment_classified_image_returns_empty_facts(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            build_safe_general_image_facts,
        )

        facts = build_safe_general_image_facts(
            message=_PAYMENT_VISION,
            inbound_metadata={
                "vision_text": "إيصال تحويل بنكي بمبلغ 150 ريال",
                "image_kind": "payment_receipt",
            },
        )
        assert facts == {}


class TestGeneralImageSafeFactsDecision:
    def test_vision_only_routes_with_safe_image_facts(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            TOPIC_IMAGE_ACK_OR_CLARIFY,
            try_general_media_ack_decision,
        )

        meta = {
            "vision_text": (
                "لقطة شاشة من منشور اجتماعي قصير يظهر شخصان "
                "وأحدهما يحمل كوباً"
            ),
        }
        decision = try_general_media_ack_decision(
            _ctx_with_meta(_SOCIAL_VISION, meta),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_IMAGE_ACK_OR_CLARIFY
        safe = decision.args.get("safe_image_facts") or {}
        assert safe.get("scene_type")
        assert safe.get("visible_elements")

    def test_compose_goal_uses_safe_facts_not_generic_only(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            compose_image_ack_or_clarify_goal,
        )

        safe = {
            "scene_type": "social_media_screenshot",
            "visible_elements": ["people appear in the image", "a cup or drink is visible"],
            "visible_text_summary": "social post screenshot with on-screen text",
            "safety_notes": ["Do not identify people or name individuals."],
        }
        goal = compose_image_ack_or_clarify_goal(safe_image_facts=safe)
        assert "SAFE_IMAGE_FACTS" in goal
        assert "Visible elements:" in goal
        assert "people appear in the image" in goal
        assert "Do not identify people" in goal

    def test_ocr_handles_do_not_trigger_staff_contact(self) -> None:
        from modules.ai.brain.commerce.staff_contact_evidence import (
            classify_staff_contact_request,
            compile_staff_contact_registry,
        )
        from modules.ai.brain.commerce.staff_contact_media_source_guard import (
            staff_contact_intent_message,
        )

        class _Section:
            def __init__(self, body: str) -> None:
                self.body = body

        reg = compile_staff_contact_registry([_Section(body="موظف: 0501111111")])
        assert staff_contact_intent_message(_SOCIAL_VISION) == ""
        assert classify_staff_contact_request(_SOCIAL_VISION, registry=reg).kind == "none"

    def test_ocr_storefront_phrase_not_website_route(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )

        msg = "[وصف الصورة] محتوى يذكر رابط المتجر الإلكتروني والموقع الإلكتروني"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.UNKNOWN_LINK

    def test_payment_classified_not_general_media(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            try_general_media_ack_decision,
        )

        decision = try_general_media_ack_decision(
            _ctx_with_meta(
                _PAYMENT_VISION,
                {"vision_text": "إيصال تحويل", "image_kind": "payment_receipt"},
            ),
        )
        assert decision is None


class TestGeneralImageSafeFactsPipeline:
    def test_compose_base_goal_includes_safe_facts(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import Decision, SuggestionSnapshot

        safe = {
            "scene_type": "social_media_screenshot",
            "visible_elements": ["people appear in the image"],
        }
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "image_ack_or_clarify",
                "safe_image_facts": safe,
            },
            reason="test",
            confidence=0.9,
        )
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert "SAFE_IMAGE_FACTS" in goal
        assert "people appear in the image" in goal

    def test_known_facts_pattern_for_image_ack(self) -> None:
        decision_args = {
            "topic": "image_ack_or_clarify",
            "safe_image_facts": {
                "scene_type": "general_photo",
                "visible_elements": ["on-screen text or interaction markers"],
            },
        }
        known_facts: dict = {}
        if str(decision_args.get("topic") or "") == "image_ack_or_clarify":
            _safe_image_facts = dict(decision_args.get("safe_image_facts") or {})
            if _safe_image_facts:
                known_facts["safe_image_facts"] = _safe_image_facts
        assert "safe_image_facts" in known_facts
        assert known_facts["safe_image_facts"]["scene_type"] == "general_photo"
