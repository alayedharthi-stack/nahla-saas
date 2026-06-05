#!/usr/bin/env python3
"""
Staging Phase A — clarification replay and evidence report.

Run with CONTEXTUAL_CLARIFY_ENABLED=true (staging parity):

  cd backend
  set CONTEXTUAL_CLARIFY_ENABLED=true   # Windows
  python ../scripts/staging_phase_a_clarification_replay.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Staging parity — must match Railway staging env.
os.environ.setdefault("CONTEXTUAL_CLARIFY_ENABLED", "true")
os.environ.setdefault("CLARIFICATION_SHADOW_ENABLED", "true")

from modules.ai.brain.clarification.flags import (  # noqa: E402
    is_clarification_shadow_enabled,
    is_contextual_clarify_enabled,
)
from modules.ai.brain.clarification.router import (  # noqa: E402
    record_clarification_shadow,
    try_contextual_clarification_fallback,
)
from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    intelligent_need_clarification,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.pipeline import _compose_response_goal  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    clarify_instead_of_top_products,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)
from modules.ai.brain.intent import rules  # noqa: E402

LEGACY_GENERAL = intelligent_need_clarification("general_attribute")
LEGACY_MARKER = "تقصد حاجة أو مواصفة"

REPLAY_MESSAGES = [
    "بكم القسط؟",
    "تقسيط بكم والسعر الإجمالي كم؟",
    "أبي شيء مناسب للوالد.",
    "أبغى الأفضل.",
    "الله يسعدك وين ما تروح",
]


def _ctx(message: str) -> BrainContext:
    intent = rules.match(message)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=message)
    return BrainContext(
        tenant_id=0,
        customer_phone="staging-replay",
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            store_name="Staging Store",
            has_products=True,
            product_count=5,
            in_stock_count=5,
            orderable=True,
            snapshot_fresh=True,
        ),
    )


def _legacy_hit(decision: Decision) -> bool:
    q = str((decision.args or {}).get("question") or "")
    return LEGACY_GENERAL in q or LEGACY_MARKER in q


def _shadow_line(message: str, trigger: str) -> dict:
    ctx = _ctx(message)
    spec = record_clarification_shadow(
        ctx,
        trigger=trigger,
        legacy_action="clarify",
        legacy_reason="staging_replay",
    )
    return {
        "message": message,
        "trigger": trigger,
        "class": spec.ambiguity_class,
        "recovery_mode": spec.recovery_mode,
        "compose_topic": spec.compose_topic,
        "would_action": "llm_reply" if spec.is_generative else "clarify",
    }


def _prompt_salesperson_check(message: str) -> dict:
    dec = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "contextual_clarify",
            "ambiguity_class": "missing_product_ref",
            "clarification_evidence": {"intent_name": "ask_price"},
        },
        reason="staging_replay",
        confidence=0.84,
    )
    goal = _compose_response_goal(dec, SuggestionSnapshot())
    state = BrainReplyState(
        store_name="Staging Store",
        tone="warm",
        stage="discovery",
        response_goal=goal,
        contextual_clarify_mode=True,
        ambiguity_class="missing_product_ref",
        clarification_evidence={"intent_name": "ask_price"},
        intent_name="ask_price",
    )
    prompt = build_brain_reply_prompt(state)
    return {
        "message": message,
        "has_salesperson_block": "SALESPERSON BEHAVIOR" in prompt,
        "has_contextual_block": "contextual_clarify" in prompt,
    }


def main() -> int:
    print("=== Phase A Staging Replay Report ===")
    print(f"CONTEXTUAL_CLARIFY_ENABLED={is_contextual_clarify_enabled()}")
    print(f"CLARIFICATION_SHADOW_ENABLED={is_clarification_shadow_enabled()}")
    print()

    engine = DefaultDecisionEngine()
    rows = []

    for msg in REPLAY_MESSAGES:
        ctx = _ctx(msg)
        engine_dec = engine.decide(ctx)
        gate_dec = clarify_instead_of_top_products(
            ctx, reason="weak_or_unknown_intent",
        )
        router_dec = try_contextual_clarification_fallback(
            ctx, trigger="staging_replay",
        )
        shadow = _shadow_line(msg, "staging_replay")
        prompt_chk = _prompt_salesperson_check(msg)

        row = {
            "message": msg,
            "rules_intent": getattr(ctx.intent, "name", ""),
            "engine": {
                "action": engine_dec.action,
                "topic": (engine_dec.args or {}).get("topic"),
                "reason": (engine_dec.reason or "")[:120],
                "legacy_template": _legacy_hit(engine_dec),
            },
            "clarify_instead": {
                "action": gate_dec.action,
                "topic": (gate_dec.args or {}).get("topic"),
                "legacy_template": _legacy_hit(gate_dec),
            },
            "router": {
                "action": router_dec.action if router_dec else None,
                "topic": (router_dec.args or {}).get("topic") if router_dec else None,
            },
            "shadow": shadow,
            "prompt": prompt_chk,
        }
        rows.append(row)

        print(f"--- {msg!r} ---")
        print(json.dumps(row, ensure_ascii=False, indent=2))
        print()

    failures = []
    for r in rows:
        if r["engine"]["legacy_template"] or r["clarify_instead"]["legacy_template"]:
            failures.append(f"legacy_template: {r['message']}")
        if r["prompt"]["has_salesperson_block"]:
            failures.append(f"salesperson_framing: {r['message']}")

    print("=== Summary ===")
    print(f"replay_count={len(rows)}")
    print(f"failures={failures or 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
