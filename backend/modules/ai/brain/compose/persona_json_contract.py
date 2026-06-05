"""
brain/compose/persona_json_contract.py
──────────────────────────────────────
Persona JSON Contract v1 — **shadow measurement only** (Phase B0).

Computes the allowlisted JSON shape defined in the Phase B design review.
Must never alter the prompt; used exclusively for ``[BRAIN_STATE_SLIM]`` v2
telemetry until B2 enforcement is approved.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..persona_expression import PERSONA_TOPICS
from ..types import BrainReplyState

_SCHEMA_VERSION = 1

_TIER_A_KEYS = (
    "store_name",
    "tone",
    "stage",
    "intent_name",
    "identity_already_introduced",
    "persona_expression_mode",
    "persona_topic",
    "persona_kind",
    "non_commerce_block_mode",
    "response_goal",
)

_PERSONA_MEMORY_ALLOW = frozenset({
    "first_name",
    "preferred_name",
    "communication_style",
    "relationship_notes",
})

_MAX_RECENT_TURNS = 4
_MAX_TURN_CHARS = 120
_MAX_SUMMARY_CHARS = 800
_MAX_RESPONSE_GOAL_CHARS = 2000
_MAX_RELATIONSHIP_NOTES_CHARS = 300


def is_persona_contract_eligible(state: BrainReplyState) -> bool:
    """
    Gate for shadow contract metrics — mirrors approved B2 eligibility (no enforce).
    """
    if not bool(getattr(state, "persona_expression_mode", False)):
        return False

    if bool(getattr(state, "platform_kb_mode", False)):
        return False

    if bool(getattr(state, "contextual_clarify_mode", False)):
        return False

    topic = str(getattr(state, "persona_topic", "") or "").strip()
    if topic not in PERSONA_TOPICS:
        return False

    return True


def _cap_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _cap_recent_turns(turns: Any) -> List[str]:
    if not isinstance(turns, list):
        return []
    out: List[str] = []
    for item in turns[:_MAX_RECENT_TURNS]:
        out.append(_cap_text(item, _MAX_TURN_CHARS))
    return out


def _subset_customer_memory(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _PERSONA_MEMORY_ALLOW:
        if key not in raw:
            continue
        val = raw[key]
        if key == "relationship_notes":
            val = _cap_text(val, _MAX_RELATIONSHIP_NOTES_CHARS)
        elif val is not None:
            val = str(val).strip()
        if val:
            out[key] = val
    return out


def _slim_merchant_context(mc: Any) -> Dict[str, Any]:
    if not isinstance(mc, dict):
        return {}
    slim: Dict[str, Any] = {}
    tid = mc.get("tenant_id")
    if tid is not None:
        slim["tenant_id"] = tid
    customer = mc.get("customer")
    if isinstance(customer, dict):
        display = str(customer.get("display_name") or "").strip()
        if display:
            slim["customer"] = {"display_name": display[:128]}
    return slim


def apply_persona_json_contract_shadow(
    state_dict: Dict[str, Any],
    *,
    state: BrainReplyState | None = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Build the v1 allowlisted dict for measurement only.

    ``state_dict`` should be the **pre-slim** brain state (full pipeline output).
    Returns ``(contract_dict, omitted_top_level_keys)``.
    """
    omitted: List[str] = []
    out: Dict[str, Any] = {"_contract_schema": f"nahla/persona-json-contract/v{_SCHEMA_VERSION}"}

    for key in _TIER_A_KEYS:
        if key not in state_dict:
            continue
        val = state_dict[key]
        if key == "response_goal":
            val = _cap_text(val, _MAX_RESPONSE_GOAL_CHARS)
        out[key] = val

    out["recent_turns"] = _cap_recent_turns(state_dict.get("recent_turns"))

    summary = _cap_text(state_dict.get("conversation_summary"), _MAX_SUMMARY_CHARS)
    if summary:
        out["conversation_summary"] = summary

    memory = _subset_customer_memory(state_dict.get("customer_memory"))
    if memory:
        out["customer_memory"] = memory

    mc = _slim_merchant_context(state_dict.get("merchant_context"))
    if mc:
        out["merchant_context"] = mc

    if state is not None:
        out["persona_expression_mode"] = True
        pt = str(getattr(state, "persona_topic", "") or "").strip()
        if pt:
            out["persona_topic"] = pt
        pk = str(getattr(state, "persona_kind", "") or "").strip()
        if pk:
            out["persona_kind"] = pk

    allowed = set(out.keys())
    for key in state_dict:
        if key not in allowed and key != "tenant_overlay":
            omitted.append(key)

    return out, sorted(omitted)


__all__ = [
    "apply_persona_json_contract_shadow",
    "is_persona_contract_eligible",
]
