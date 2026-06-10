"""P1-E regression: product media response quality."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_media import (  # noqa: E402
    PRODUCT_MEDIA_TOPIC,
    build_product_media_decision_args,
    compose_product_media_response_goal,
    detect_product_media_turn,
    has_active_order_evidence,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.pipeline import _compose_base_response_goal  # noqa: E402
from modules.ai.brain.postprocess.product_media_reply_guard import (  # noqa: E402
    apply_product_media_reply_guard,
    strip_product_media_violations,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)

_HONEY_VIDEO = (
    "[فيديو من العميل]\n"
    "النص الظاهر/الوصف من الفيديو: فيديو يظهر مرحلة تصفية العسل "
    "من الخلايا إلى العبوات.\n"
    "استنتاج خفيف من النص المتاح: نحل_أو_عسل"
)

_PRODUCT_DETAIL_TEXT = (
    "هذا العسل تاريخ الفرز 20 ذي الحجة صيفي مع الضرم"
)


class TestProductMediaDetection:
    def test_honey_video_with_vision_matches(self) -> None:
        v = detect_product_media_turn(
            _HONEY_VIDEO,
            inbound_metadata={
                "source_type": "video",
                "topic_hints": ["نحل_أو_عسل"],
                "frame_vision_status": "ok",
                "frame_vision_text": "تصفية العسل",
            },
            intent_name="general",
        )
        assert v.matched
        assert v.has_vision_evidence

    def test_hint_only_when_vision_failed(self) -> None:
        v = detect_product_media_turn(
            "[فيديو من العميل]\nاستنتاج خفيف من النص المتاح: نحل_أو_عسل",
            inbound_metadata={
                "source_type": "video",
                "topic_hints": ["نحل_أو_عسل"],
                "frame_vision_status": "skipped",
            },
            intent_name="general",
        )
        assert v.matched
        assert v.has_hint_only
        assert not v.has_vision_evidence

    def test_product_detail_text_matches(self) -> None:
        v = detect_product_media_turn(
            _PRODUCT_DETAIL_TEXT,
            intent_name="general",
        )
        assert v.matched

    def test_explicit_price_intent_excluded(self) -> None:
        v = detect_product_media_turn(
            _HONEY_VIDEO,
            inbound_metadata={"topic_hints": ["نحل_أو_عسل"]},
            intent_name="ask_price",
        )
        assert not v.matched

    def test_social_only_excluded(self) -> None:
        v = detect_product_media_turn(
            "كل عام وأنتم بخير",
            inbound_metadata={"topic_hints": ["دعاء_أو_تهنئة"]},
            intent_name="general",
        )
        assert not v.matched


class TestResponseGoal:
    def test_goal_forbids_shipment_without_order_evidence(self) -> None:
        goal = compose_product_media_response_goal(
            has_vision_evidence=True,
            has_hint_only=False,
            active_order_evidence=False,
            vision_preview="تصفية العسل",
        )
        assert "active_order_evidence=false" in goal
        assert "product_media" in goal
        assert "caption" in goal.lower() or "publish" in goal.lower()

    def test_goal_allows_shipment_with_order_evidence(self) -> None:
        goal = compose_product_media_response_goal(
            has_vision_evidence=True,
            has_hint_only=False,
            active_order_evidence=True,
        )
        assert "active_order_evidence=true" in goal

    def test_goal_forbids_repeated_yebdo_shape(self) -> None:
        goal = compose_product_media_response_goal(
            has_vision_evidence=True,
            has_hint_only=False,
            active_order_evidence=False,
        )
        assert "يبدو أنك تعرض" in goal

    def test_goal_forbids_thanks_only(self) -> None:
        goal = compose_product_media_response_goal(
            has_vision_evidence=False,
            has_hint_only=True,
            active_order_evidence=False,
        )
        assert "شكرًا على المعلومات" in goal

    def test_pipeline_uses_decision_goal(self) -> None:
        args = build_product_media_decision_args(
            detect_product_media_turn(
                _HONEY_VIDEO,
                inbound_metadata={"topic_hints": ["نحل_أو_عسل"]},
            ),
        )
        decision = Decision(action=ACTION_LLM_REPLY, args=args, reason="test")
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert goal.startswith("product_media")
        assert "واضح أنها مرحلة" not in goal


class TestDecisionEngineRoute:
    def test_general_honey_video_routes_product_media(self) -> None:
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message=_HONEY_VIDEO,
            intent=Intent(name="general", confidence=0.5, slots={}),
            state=MerchantConversationState(),
            facts=CommerceFacts(),
            profile={
                "inbound_metadata": {
                    "source_type": "video",
                    "topic_hints": ["نحل_أو_عسل"],
                    "frame_vision_status": "ok",
                    "frame_vision_text": "تصفية العسل",
                },
            },
        )
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_LLM_REPLY
        assert (d.args or {}).get("topic") == PRODUCT_MEDIA_TOPIC


class TestProductMediaGuard:
    def test_strips_video_uncertainty_with_hints(self) -> None:
        bad = (
            "آسفة، لم أتمكن من مشاهدة الفيديو، لكن يبدو أنه عسل.\n"
            "أقدر أساعدك بوصف للفيديو."
        )
        cleaned, stripped = strip_product_media_violations(
            bad,
            has_content_signal=True,
            allow_order_shipment=False,
        )
        assert stripped
        assert "لم أتمكن" not in cleaned
        assert "وصف" in cleaned

    def test_strips_shipment_without_order(self) -> None:
        bad = "جميل! إذا لديك استفسار حول طلبك أو الشحنة خبرني."
        cleaned, stripped = strip_product_media_violations(
            bad,
            has_content_signal=True,
            allow_order_shipment=False,
        )
        assert stripped
        assert "الشحنة" not in cleaned

    def test_keeps_shipment_with_order_evidence(self) -> None:
        bad = "طلبك تحت التجهيز — والفيديو يوضح التصفية."
        cleaned, stripped = strip_product_media_violations(
            bad,
            has_content_signal=True,
            allow_order_shipment=True,
        )
        assert not stripped
        assert cleaned == bad

    def test_guard_integration(self) -> None:
        r = apply_product_media_reply_guard(
            "لم أتمكن من مشاهدة الفيديو لكن يبدو عسل.",
            inbound_text=_HONEY_VIDEO,
            inbound_metadata={"topic_hints": ["نحل_أو_عسل"], "source_type": "video"},
        )
        assert r.stripped
        assert "لم أتمكن" not in r.reply


class TestActiveOrderEvidence:
    def test_structured_bundle(self) -> None:
        assert has_active_order_evidence({
            "active_order_context": {
                "order_id": "42",
                "order_status": "pending_review",
            },
        })

    def test_empty_bundle(self) -> None:
        assert not has_active_order_evidence({})


class TestNormalizerFraming:
    def test_no_vision_failure_in_brain_text(self) -> None:
        import inspect

        from modules.ai.media import normalizer as norm  # noqa: PLC0415

        src = inspect.getsource(norm._process_video)
        assert "تعذّر استخراج وصف بصري" not in src
        assert "حافظ على ربط المحادثة بالطلب أو الشحنة" not in src
