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


def test_no_live_customer_coupon_action_from_decision_engine() -> None:
    """Action exists for canary ownership, but decide() still never emits it."""
    assert decision_actions.ACTION_CUSTOMER_COUPON_REQUEST == "customer_coupon_request"
    assert decision_actions.ACTION_CUSTOMER_COUPON_REQUEST in decision_actions.ALL_ACTIONS
    assert decision_actions.ACTION_SUGGEST_COUPON == "suggest_coupon"
    source = inspect.getsource(DefaultDecisionEngine.decide)
    assert "ACTION_CUSTOMER_COUPON_REQUEST" not in source
    assert "customer_coupon_request" not in source
    assert "issue_customer_coupon" not in source


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
    assert "maybe_own_customer_coupon_request_turn" in pipeline
    assert "maybe_run_coupon_capability_probe_for_turn" in pipeline
    assert "if tenant_id == 1" not in pipeline
    assert "tenant_id == 1" not in pipeline


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
