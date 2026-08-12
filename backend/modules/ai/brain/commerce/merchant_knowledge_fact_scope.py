"""
Pack A3 — per-turn fact scoping for merchant knowledge ownership.

Neighboring Pack B capability facts and A2 profile description must not
silently substitute for missing owner-domain MKS evidence on
``merchant_knowledge_*`` turns.

IO-free: given decision.args, return which known_facts keys / profile
surfaces to suppress for THIS turn only.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping, Optional, Set

_POLICY_SURFACE = "merchant_knowledge_section"

# Always suppress these on shipping_policy knowledge turns (Pack B / checkout
# neighbors that became substitute policy evidence in live Production).
_SHIPPING_POLICY_SUPPRESS_KEYS: FrozenSet[str] = frozenset(
    {
        "shipping_policy",
        "shipping_methods",
        "shipping_methods_source",
        "shipping_notes",
        "shipping_knowledge",
        "merchant_capability_answer",
        "salla_shipping_companies_status",
    }
)

_STORY_SUPPRESS_KEYS: FrozenSet[str] = frozenset(
    {
        "store_description",
    }
)


def is_merchant_knowledge_surface(decision_args: Optional[Mapping[str, Any]]) -> bool:
    args = dict(decision_args or {})
    topic = str(args.get("topic") or "")
    surface = str(args.get("policy_surface") or "")
    return surface == _POLICY_SURFACE or topic.startswith("merchant_knowledge_")


def knowledge_kind_from_args(decision_args: Optional[Mapping[str, Any]]) -> str:
    args = dict(decision_args or {})
    kind = str(args.get("knowledge_kind") or args.get("question_kind") or "").strip()
    if kind:
        return kind
    topic = str(args.get("topic") or "")
    if topic.startswith("merchant_knowledge_"):
        return topic[len("merchant_knowledge_") :]
    return ""


def knowledge_turn_suppressed_fact_keys(
    decision_args: Optional[Mapping[str, Any]],
) -> FrozenSet[str]:
    """Return known_facts keys to clear for this knowledge turn (per-turn only)."""
    if not is_merchant_knowledge_surface(decision_args):
        return frozenset()
    kind = knowledge_kind_from_args(decision_args)
    out: Set[str] = set()
    if kind == "shipping_policy":
        out.update(_SHIPPING_POLICY_SUPPRESS_KEYS)
    if kind == "store_story":
        out.update(_STORY_SUPPRESS_KEYS)
    return frozenset(out)


def should_inject_shipping_knowledge_facts(
    decision_args: Optional[Mapping[str, Any]],
) -> bool:
    """False when Pack A3 owns the turn — do not inject checkout shipping_knowledge."""
    return not is_merchant_knowledge_surface(decision_args)


def apply_knowledge_turn_fact_scope(
    known_facts: Dict[str, Any],
    decision_args: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a shallow-copied known_facts with neighboring owner-substitutes cleared."""
    facts = dict(known_facts or {})
    suppress = knowledge_turn_suppressed_fact_keys(decision_args)
    for key in suppress:
        facts.pop(key, None)
    kind = knowledge_kind_from_args(decision_args)
    if kind == "shipping_policy":
        caps = facts.get("merchant_capabilities")
        if isinstance(caps, dict) and caps:
            slim_caps = dict(caps)
            slim_caps.pop("shipping", None)
            facts["merchant_capabilities"] = slim_caps
    if kind == "store_story":
        facts.pop("store_description", None)
    return facts


def scope_merchant_profiles_for_knowledge_turn(
    merchant_profile: Any,
    tenant_profile: Any,
    decision_args: Optional[Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Strip description from profile surfaces on store_story knowledge turns."""
    mp = dict(merchant_profile) if isinstance(merchant_profile, dict) else {}
    tp = dict(tenant_profile) if isinstance(tenant_profile, dict) else {}
    if knowledge_kind_from_args(decision_args) != "store_story":
        return mp, tp
    if not is_merchant_knowledge_surface(decision_args):
        return mp, tp
    mp = dict(mp)
    tp = dict(tp)
    mp.pop("description", None)
    tp.pop("description", None)
    return mp, tp


def merchant_knowledge_response_goal(
    decision_args: Optional[Mapping[str, Any]],
) -> str:
    """Return decision.args response_goal when this is a merchant knowledge turn."""
    if not is_merchant_knowledge_surface(decision_args):
        return ""
    return str((decision_args or {}).get("response_goal") or "").strip()


__all__ = [
    "apply_knowledge_turn_fact_scope",
    "is_merchant_knowledge_surface",
    "knowledge_kind_from_args",
    "knowledge_turn_suppressed_fact_keys",
    "merchant_knowledge_response_goal",
    "scope_merchant_profiles_for_knowledge_turn",
    "should_inject_shipping_knowledge_facts",
]
