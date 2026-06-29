"""Hard dedup must not silence repeated commerce/availability inquiries."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    prior_outbound_was_unhelpful_availability_rewrite,
    prior_outbound_was_wrong_social_only_reply,
    should_bypass_hard_dedup_repeat_availability,
    should_restore_brain_reply_after_dedup_silence,
)
from routers.whatsapp_webhook import _max_outbound_overlap  # noqa: E402

_MSG = "صباح الخير\nفيه عندك طرود نحل؟"
_UNCERTAINTY = "ما نقدر نؤكد التوفر بدقة لهذا المنتج"
_SOCIAL_ONLY = "صباح النور! 👋 🌿"


class TestDedupCommerceInquirySilence:
    def test_unhelpful_availability_pattern_matches_nuakid_variant(self) -> None:
        assert prior_outbound_was_unhelpful_availability_rewrite(_UNCERTAINTY)

    def test_greeting_plus_availability_is_commerce_inquiry_for_bypass(self) -> None:
        assert should_bypass_hard_dedup_repeat_availability(_MSG, _UNCERTAINTY)
        assert should_bypass_hard_dedup_repeat_availability(_MSG, _SOCIAL_ONLY)

    def test_wrong_social_only_prior_detected(self) -> None:
        assert prior_outbound_was_wrong_social_only_reply(_SOCIAL_ONLY)
        assert not prior_outbound_was_wrong_social_only_reply(_UNCERTAINTY)

    def test_restore_brain_candidate_after_dedup_silence(self) -> None:
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound=_MSG,
            candidate_reply=_UNCERTAINTY,
            previous_outbound=_SOCIAL_ONLY,
        )

    def test_pure_greeting_does_not_restore(self) -> None:
        assert not should_restore_brain_reply_after_dedup_silence(
            current_inbound="صباح الخير",
            candidate_reply="صباح النور! 👋",
            previous_outbound=_SOCIAL_ONLY,
        )

    @pytest.mark.parametrize(
        "history",
        [
            [
                {"direction": "outbound", "body": _SOCIAL_ONLY},
                {"direction": "inbound", "body": _MSG},
                {"direction": "outbound", "body": _UNCERTAINTY},
            ],
        ],
    )
    def test_identical_uncertainty_reply_triggers_hard_overlap(self, history: list) -> None:
        overlap = _max_outbound_overlap(_UNCERTAINTY, history)
        assert overlap >= 0.85
