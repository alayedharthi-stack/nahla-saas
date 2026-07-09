"""Class 9 PR1 — outbound quality metadata stamping."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_quality_events import (  # noqa: E402
    build_outbound_quality_metadata,
    merge_quality_metadata_into_extra_metadata,
)
from modules.ai.brain.pipeline import _build_quality_observability  # noqa: E402
from modules.ai.brain.types import Decision, Intent  # noqa: E402


class TestBuildOutboundQualityMetadata:
    def test_flattens_brain_quality_observability(self) -> None:
        brain_result = {
            "quality_observability": {
                "chosen_path": "track_order_status",
                "decision_action": "track_order",
                "intent": "track_order",
                "surface": "",
                "source": "templates",
                "question_kind": "",
                "price_source": "",
                "catalog_product_ids": [],
                "pre_guard_body_preview": "حالة الطلب",
                "post_guard_body_preview": "حالة الطلب النهائية",
                "guards_triggered": [],
                "final_turn_violations": [],
            }
        }
        meta = build_outbound_quality_metadata(brain_result)
        assert meta["chosen_path"] == "track_order_status"
        assert meta["decision_action"] == "track_order"
        assert meta["intent"] == "track_order"

    def test_merges_outbound_text_policy_previews(self) -> None:
        brain_result = {"chosen_path": "catalog_product_answer", "decision_action": "llm_reply"}
        policy = {
            "text_source": "llm",
            "pre_postprocess_body_preview": "before guards",
            "postprocess_body_preview": "after guards",
        }
        meta = build_outbound_quality_metadata(
            brain_result,
            outbound_text_policy=policy,
            inbound_text="كم سعر جاكيت؟",
        )
        assert meta["pre_guard_body_preview"] == "before guards"
        assert meta["post_guard_body_preview"] == "after guards"
        assert meta["outbound_text_policy"]["text_source"] == "llm"


class TestMergeQualityMetadataIntoExtra:
    def test_stamps_quality_observability_block(self) -> None:
        merged = merge_quality_metadata_into_extra_metadata(
            {"phone": "966500000001"},
            {
                "chosen_path": "catalog_product_answer",
                "decision_action": "llm_reply",
                "intent": "ask_price",
                "surface": "catalog_product_answer",
                "source": "catalog_deterministic_fallback",
                "question_kind": "price",
                "price_source": "catalog",
                "outbound_text_policy": {"text_source": "deterministic"},
            },
        )
        assert merged["chosen_path"] == "catalog_product_answer"
        assert merged["quality_observability"]["surface"] == "catalog_product_answer"
        assert merged["quality_observability"]["question_kind"] == "price"
        assert merged["question_kind"] == "price"


class TestPipelineQualityObservabilityBuilder:
    def test_builds_guard_and_violation_fields(self) -> None:
        decision = Decision(action="track_order", args={"topic": "shipment_status"})
        intent = Intent(name="track_order", confidence=0.95, raw_message="وين طلبي؟")
        result_data = {
            "persona_compose": {},
            "final_turn_violations_post_compose": ["catalog_promise_without_catalog_action"],
            "catalog_product_ids": [1],
            "price_source": "catalog",
            "question_kind": "price",
        }
        obs = _build_quality_observability(
            chosen_path="track_order_status",
            decision=decision,
            intent=intent,
            result_data=result_data,
            reply="حالة الطلب: قيد الإكمال",
            pre_guard_body="حالة الطلب",
            guards_triggered=["product_claim_grounding_guard"],
        )
        assert obs["chosen_path"] == "track_order_status"
        assert obs["topic"] == "shipment_status"
        assert obs["guards_triggered"] == ["product_claim_grounding_guard"]
        assert obs["final_turn_violations"] == ["catalog_promise_without_catalog_action"]
        assert obs["post_guard_body_preview"].startswith("حالة الطلب")
