"""Phase 2A/2B safety regressions — existing owners stay on their current path."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.brain.decision import actions as decision_actions
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import slot_extractor
from modules.ai.brain.intent.coupon_capability_probe import COUPON_CAPABILITY_PROBE_SYSTEM
from services.coupon_generator import _segment_to_level
from services.customer_request_coupon_service import (
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
)


def test_no_live_customer_coupon_action() -> None:
    names = [name for name in dir(decision_actions) if name.startswith("ACTION_")]
    assert "ACTION_CUSTOMER_COUPON_REQUEST" not in names
    assert decision_actions.ACTION_SUGGEST_COUPON == "suggest_coupon"


def test_hesitation_coupon_action_still_only_hesitation_product() -> None:
    source = inspect.getsource(DefaultDecisionEngine.decide)
    assert "ACTION_SUGGEST_COUPON" in source
    assert "INTENT_HESITATION" in source
    assert "customer_coupon_request" not in source
    assert "issue_customer_coupon" not in source


def test_pipeline_does_not_call_issuance() -> None:
    pipeline = (REPO_ROOT / "backend/modules/ai/brain/pipeline.py").read_text(encoding="utf-8")
    assert "issue_customer_coupon" not in pipeline
    assert "ACTION_CUSTOMER_COUPON_REQUEST" not in pipeline
    assert "maybe_run_shadow_coupon_capability_probe" in pipeline


def test_slot_extractor_and_persona_not_repurposed() -> None:
    assert COUPON_CAPABILITY_PROBE_SYSTEM != slot_extractor._SYSTEM
    assert "customer_coupon_request" not in slot_extractor._SYSTEM
    from modules.ai.brain.intent.slot_extractor import LAYER2_INTENT_HINT_VOCABULARY
    assert "customer_coupon_request" not in LAYER2_INTENT_HINT_VOCABULARY
    layer2 = REPO_ROOT / "backend/modules/ai/brain/truth_surface/layer2"
    vocab_hits = []
    for path in layer2.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "customer_coupon_request" in text:
            vocab_hits.append(str(path))
    assert vocab_hits == []


def test_crm_segment_owner_unchanged() -> None:
    assert _segment_to_level("new") == "bronze"
    assert _segment_to_level("active") == "silver"
    assert _segment_to_level("vip") == "gold"


def test_live_switches_off() -> None:
    assert CUSTOMER_COUPON_LIVE_ROUTING is False
    assert CUSTOMER_COUPON_LIVE_ISSUANCE is False
