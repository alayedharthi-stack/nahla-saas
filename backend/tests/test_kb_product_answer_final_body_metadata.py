"""Regression: KB product answer body/metadata must survive post-compose guards."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.catalog_body_policy import TECHNICAL_CATALOG_BODY  # noqa: E402
from modules.ai.brain.persona.integration import (  # noqa: E402
    merge_persona_compose_into_extra_metadata,
)
from modules.ai.brain.postprocess.catalog_browse_silent_recovery import (  # noqa: E402
    try_catalog_browse_silent_recovery,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)

SMOKE_MESSAGE = "ما هي مميزات عسل السدر القيضي؟"
KB_REPLY = (
    "عسل السدر القيضي يتميز بندرة الإنتاج وطعم غني "
    "وموثوق من قاعدة المعرفة."
)


class TestKbProductAnswerMetadataMerge:
    def test_merge_preserves_kb_grounding_fields(self) -> None:
        event = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {
                "surface": "kb_product_answer",
                "source": "persona_llm",
                "guard_passed": True,
                "model": "gpt-4o-mini",
                "facts_hash": "abc123",
            },
            "knowledge_source": "tenant_knowledge_base",
            "kb_section_ids": [213, 222],
            "question_kind": "features",
        }
        merged = merge_persona_compose_into_extra_metadata({"is_ai": True}, event)
        assert merged["knowledge_source"] == "tenant_knowledge_base"
        assert merged["kb_section_ids"] == [213, 222]
        assert merged["question_kind"] == "features"
        assert merged["persona_compose"]["surface"] == "kb_product_answer"


class TestKbProductAnswerGuardBypass:
    def test_availability_guard_does_not_rewrite_kb_answer(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
        inactive_ctx = {
            "catalog_skus": [
                {"id": 1, "title": "عسل السدر القيضي", "can_checkout": False},
            ]
        }
        result = apply_product_availability_truth_guard(
            reply=KB_REPLY,
            availability_context=inactive_ctx,
            inbound_text=SMOKE_MESSAGE,
            chosen_path="fact_bound_persona_compose",
            decision_topic="product_knowledge_facts",
        )
        assert result.reply == KB_REPLY
        assert result.action == "allowed_product_knowledge_facts"
        assert not result.replaced

    def test_catalog_silent_recovery_skips_product_knowledge_question(self) -> None:
        reply = try_catalog_browse_silent_recovery(
            inbound_text=SMOKE_MESSAGE,
            tenant_id=33,
            db=None,
        )
        assert reply is None

    def test_catalog_silent_recovery_still_applies_to_browse(self) -> None:
        reply = try_catalog_browse_silent_recovery(
            inbound_text="وش عندكم منتجات؟",
            tenant_id=33,
            db=None,
        )
        assert reply == TECHNICAL_CATALOG_BODY


class TestKbProductAnswerVariants:
    @pytest.mark.parametrize(
        "message",
        [
            "ما هي مميزات عسل السدر القيضي؟",
            "وش مميزات عسل السدر القيضي؟",
            "ما هي خصائص عسل السدر القيضي؟",
        ],
    )
    def test_availability_guard_skips_feature_question_variants(
        self, message: str, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
        result = apply_product_availability_truth_guard(
            reply=KB_REPLY,
            inbound_text=message,
            chosen_path="llm_reply",
        )
        assert result.reply == KB_REPLY
        assert result.action == "allowed_product_knowledge_facts"
