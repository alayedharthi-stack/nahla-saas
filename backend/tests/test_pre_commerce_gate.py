"""
tests/test_pre_commerce_gate.py
───────────────────────────────
Pre-commerce gate — social turns must skip catalog preload.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.intent.non_commerce_classifier import (
    NC_EID_GREETING,
    NonCommerceMatch,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut
from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_SOCIAL, Intent


def _intent(name: str, conf: float = 0.95, **slots):
    return Intent(
        name=name,
        confidence=conf,
        slots=dict(slots),
        raw_message="test",
        extraction_method="rules",
    )


class TestPreCommerceGate:
    def test_social_intent_shortcuts(self):
        intent = _intent(INTENT_SOCIAL, social_category="thanks")
        assert should_pre_commerce_shortcut(intent, None) is True

    def test_non_commerce_match_shortcuts(self):
        intent = _intent(INTENT_SOCIAL, social_category=NC_EID_GREETING)
        nc = NonCommerceMatch(
            category=NC_EID_GREETING,
            confidence=0.95,
            source="ocr",
        )
        assert should_pre_commerce_shortcut(intent, nc) is True

    def test_commerce_intent_does_not_shortcut(self):
        intent = _intent(INTENT_ASK_PRODUCT, query="عسل")
        assert should_pre_commerce_shortcut(intent, None) is False

    def test_low_confidence_social_no_shortcut(self):
        intent = _intent(INTENT_SOCIAL, conf=0.5, social_category="thanks")
        assert should_pre_commerce_shortcut(intent, None, min_confidence=0.82) is False

    def test_block_commerce_slot_shortcuts(self):
        intent = _intent(
            INTENT_SOCIAL,
            block_commerce_escalation=True,
            social_category=NC_EID_GREETING,
        )
        assert should_pre_commerce_shortcut(intent, None) is True
