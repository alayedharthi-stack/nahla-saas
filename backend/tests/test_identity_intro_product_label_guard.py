"""Regression: identity/intro inbounds must not become product labels or availability rewrites."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: E402
    is_conversational_non_product_inbound,
    is_non_product_label,
)
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
    should_block_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _label_from_inbound_availability_ask,
    apply_product_availability_truth_guard,
    build_operational_availability_conflict_reply,
)

IDENTITY_INTRO_MESSAGES = (
    "انا معلم في النحل وحبيت ادوم معاكم",
    "عندي خبره",
    "انا معلم نحل",
    "مربي نحل",
    "اشتغل بالنحل",
    "حاب اتعاون معكم",
    "حاب ادوم معكم",
    "ابحث عن عمل",
)

VALID_PRODUCT_MESSAGES = (
    "ابي عسل طلح",
    "عسل سدر",
    "هل عندكم طرود نحل",
)


class TestLayer1IdentityPhrases:
    @pytest.mark.parametrize("message", IDENTITY_INTRO_MESSAGES)
    def test_identity_phrases_are_non_product(self, message: str) -> None:
        assert is_conversational_non_product_inbound(message) is True
        assert is_non_product_label(message) is True

    @pytest.mark.parametrize("message", VALID_PRODUCT_MESSAGES)
    def test_product_phrases_still_allowed(self, message: str) -> None:
        assert is_conversational_non_product_inbound(message) is False


class TestLayer2LabelExtraction:
    @pytest.mark.parametrize("message", IDENTITY_INTRO_MESSAGES)
    def test_inbound_label_empty_for_identity(self, message: str) -> None:
        assert _label_from_inbound_availability_ask(message) == ""

    def test_incident_beekeeper_reply_has_no_inbound_sentence_as_label(self) -> None:
        inbound = "انا معلم في النحل وحبيت ادوم معاكم"
        ev = MagicMock()
        ev.entity.product_id = None
        ev.entity.family_key = "inbound:beekeeper"
        ctx = {"focus_product": {}, "catalog_skus": []}
        reply = build_operational_availability_conflict_reply(
            ev, availability_context=ctx, inbound_text=inbound,
        )
        assert inbound not in reply
        assert reply == "متوفر بعدة خيارات."


class TestLayer2AvailabilityRewriteExempt:
    @pytest.mark.parametrize("message", IDENTITY_INTRO_MESSAGES)
    def test_identity_inbound_exempt_from_rewrite(self, message: str) -> None:
        assert inbound_exempt_from_availability_rewrite(message) is True

    def test_incident_identity_reply_not_rewritten_in_enforce_mode(self) -> None:
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            inbound = "انا معلم في النحل وحبيت ادوم معاكم"
            bad_reply = f"متوفر {inbound} بعدة خيارات. وش الكمية تبغى؟"
            good_reply = (
                "بما أنك معلم في تربية النحل، هل تبي طرود نحل؟ "
                "أم منتجات العسل؟ أم نحل لتوسيع مناحلك؟"
            )
            ctx = {
                "catalog_skus": [],
                "focus_product": None,
                "recommended_product_ids": [],
                "kb_signals": [],
                "kb_links": [],
            }
            result = apply_product_availability_truth_guard(
                reply=bad_reply,
                availability_context=ctx,
                inbound_text=inbound,
                tenant_id=1,
            )
            assert result.replaced is False
            assert result.reply == bad_reply

            assert should_block_availability_rewrite(
                inbound_text=inbound,
                evidence_state="unknown",
                guard_action="rewrite_unknown",
            ) is True

            result2 = apply_product_availability_truth_guard(
                reply=good_reply,
                availability_context=ctx,
                inbound_text=inbound,
                tenant_id=1,
            )
            assert result2.replaced is False
            assert result2.reply == good_reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev
