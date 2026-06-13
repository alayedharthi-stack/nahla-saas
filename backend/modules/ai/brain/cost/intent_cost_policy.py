"""
brain/cost/intent_cost_policy.py
────────────────────────────────
Platform-wide intent cost policy — when to avoid full Brain LLM compose.

PR2B: routine social turns use templates (ACTION_GREET / ACTION_SOCIAL_REPLY)
instead of Sonnet + ``build_brain_reply_prompt``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, FrozenSet, Optional

from ..types import INTENT_GREETING, INTENT_SOCIAL

_log = logging.getLogger("nahla.ai.brain.cost")

_FLAG = "NAHLA_ROUTINE_LLM_AVOID_ENABLED"

LLM_MODE_AVOID = "avoid"
LLM_MODE_ALLOW = "allow"

ROUTINE_LLM_AVOID_INTENTS: FrozenSet[str] = frozenset({
    INTENT_GREETING,
    INTENT_SOCIAL,
    "thanks",
    "farewell",
    "gratitude",
})

# Social categories answered via ``templates.social_reply`` (variant rotation).
TEMPLATE_FIRST_SOCIAL_CATEGORIES: FrozenSet[str] = frozenset({
    "thanks",
    "blessing",
    "general_courtesy",
    "morning_greeting",
    "celebration",
    "informational_only",
    "social_forward",
    "basmala",
    "prophet_invocation",
    "compliment",
    "strong_praise",
    "emotional_personal",
    "eid_greeting",
    "dua",
    "condolence",
    "religious_media",
})


@dataclass(frozen=True)
class IntentCostPolicy:
    llm_mode: str
    allow_kb: bool
    allow_catalog: bool
    allow_tools: bool
    max_input_tokens: int

    @property
    def should_avoid_llm(self) -> bool:
        return self.llm_mode == LLM_MODE_AVOID


_AVOID_POLICY = IntentCostPolicy(
    llm_mode=LLM_MODE_AVOID,
    allow_kb=False,
    allow_catalog=False,
    allow_tools=False,
    max_input_tokens=2000,
)

_DEFAULT_POLICY = IntentCostPolicy(
    llm_mode=LLM_MODE_ALLOW,
    allow_kb=True,
    allow_catalog=True,
    allow_tools=True,
    max_input_tokens=30_000,
)


def is_routine_llm_avoid_enabled() -> bool:
    return os.getenv(_FLAG, "true").strip().lower() in {
        "1", "true", "yes", "on",
    }


def get_intent_cost_policy(intent_name: str) -> IntentCostPolicy:
    if is_routine_llm_avoid_enabled():
        if str(intent_name or "").strip().lower() in ROUTINE_LLM_AVOID_INTENTS:
            return _AVOID_POLICY
    return _DEFAULT_POLICY


def should_avoid_llm_for_intent(intent_name: str) -> bool:
    return get_intent_cost_policy(intent_name).should_avoid_llm


def should_avoid_llm_for_social_category(category: str) -> bool:
    if not is_routine_llm_avoid_enabled():
        return False
    cat = str(category or "").strip().lower()
    if not cat:
        return False
    return cat in TEMPLATE_FIRST_SOCIAL_CATEGORIES


def should_use_template_for_pure_greeting(
    *,
    intent_name: str,
    embedded_greeting: bool,
    has_actionable_substance: bool,
) -> bool:
    """True when a greeting turn should use ACTION_GREET, not full LLM."""
    if not should_avoid_llm_for_intent(intent_name):
        return False
    if str(intent_name or "").strip().lower() != INTENT_GREETING:
        return False
    if embedded_greeting or has_actionable_substance:
        return False
    return True


def emit_llm_avoidable_call(
    *,
    tenant_id: Any = None,
    conversation_id: Any = None,
    turn_id: Any = None,
    intent: Optional[str] = None,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    estimated_input_tokens: Optional[int] = None,
    system_chars: Optional[int] = None,
) -> None:
    """Log when an avoid-policy turn still reached full LLM compose."""
    try:
        payload = {
            k: v
            for k, v in {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "intent": intent,
                "action": action,
                "reason": reason,
                "estimated_input_tokens": estimated_input_tokens,
                "system_chars": system_chars,
            }.items()
            if v is not None
        }
        _log.warning("[LLM_AVOIDABLE_CALL] %s", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "[LLM_COST_AUDIT_ERROR] failed avoidable call log: %s",
            type(exc).__name__,
        )
