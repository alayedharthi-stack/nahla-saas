"""
Pack A3 — per-turn fact scoping for merchant knowledge ownership.

Neighboring Pack B capability facts and A2 profile description must not
silently substitute for missing owner-domain MKS evidence on
``merchant_knowledge_*`` turns.

IO-free: given decision.args, return which known_facts keys / profile
surfaces to suppress for THIS turn only.

Failure contract: truth-ownership failure must NOT broaden the factual
surface (never restore neighboring Pack B / A2 substitutes on knowledge turns).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Mapping, Optional, Set, Tuple

logger = logging.getLogger("nahla.brain.commerce.merchant_knowledge_fact_scope")

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

# Conservative fail-safe: union of owner-substitute keys for ANY knowledge turn
# when kind-specific scoping cannot be computed.
_CONSERVATIVE_KNOWLEDGE_SUPPRESS_KEYS: FrozenSet[str] = (
    _SHIPPING_POLICY_SUPPRESS_KEYS | _STORY_SUPPRESS_KEYS
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


def store_story_capability_args(facts: Any = None) -> Dict[str, Any]:
    """Brain-owned store-story retrieval request — not a raw-text topic router."""
    status = str(getattr(facts, "store_story_status", "") or "UNKNOWN")
    if status not in {"KNOWN_PRESENT", "UNKNOWN"}:
        status = "UNKNOWN"
    return {
        "topic": "merchant_knowledge_store_story",
        "policy_surface": _POLICY_SURFACE,
        "question_kind": "store_story",
        "knowledge_kind": "store_story",
        "merchant_policy_status": status,
        "block_catalog_navigation": True,
        "response_goal": (
            "merchant_knowledge_store_story — answer from retrieved tenant "
            "store_story documents and structured merchant facts only. "
            "Do not pursue checkout next_goal."
        ),
    }


def should_request_store_story_knowledge(
    *,
    intent_name: str = "",
    facts: Any = None,
) -> bool:
    """True when platform should retrieve tenant store_story for this intent."""
    if str(getattr(facts, "store_story_status", "") or "") != "KNOWN_PRESENT":
        return False
    name = str(intent_name or "").strip()
    return name == "ask_store_info"


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


def apply_conservative_knowledge_fact_scope_failsafe(
    known_facts: Dict[str, Any],
    decision_args: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Narrower-or-equal fact surface when kind-specific scoping fails.

    Always strips the full neighboring substitute key set for knowledge turns
    and clears merchant_capabilities.shipping. Does nothing for non-knowledge turns.
    """
    facts = dict(known_facts or {})
    if not is_merchant_knowledge_surface(decision_args):
        return facts
    for key in _CONSERVATIVE_KNOWLEDGE_SUPPRESS_KEYS:
        facts.pop(key, None)
    caps = facts.get("merchant_capabilities")
    if isinstance(caps, dict) and caps:
        slim_caps = dict(caps)
        slim_caps.pop("shipping", None)
        facts["merchant_capabilities"] = slim_caps
    return facts


def scope_merchant_profiles_for_knowledge_turn(
    merchant_profile: Any,
    tenant_profile: Any,
    decision_args: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
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


def scope_merchant_profiles_failsafe(
    merchant_profile: Any,
    tenant_profile: Any,
    decision_args: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Conservative profile strip for knowledge turns when kind scoping fails."""
    mp = dict(merchant_profile) if isinstance(merchant_profile, dict) else {}
    tp = dict(tenant_profile) if isinstance(tenant_profile, dict) else {}
    if not is_merchant_knowledge_surface(decision_args):
        return mp, tp
    # Knowledge turn fail-safe: never leave description as story fuel.
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


def authoritative_merchant_knowledge_response_goal(
    decision_args: Optional[Mapping[str, Any]],
) -> str:
    """Deterministic knowledge goal from decision.args — no unsafe fallthrough.

    When the turn is a merchant knowledge surface and ``response_goal`` is set,
    that goal is authoritative.
    """
    args = dict(decision_args or {})
    topic = str(args.get("topic") or "")
    surface = str(args.get("policy_surface") or "")
    if surface != _POLICY_SURFACE and not topic.startswith("merchant_knowledge_"):
        return ""
    return str(args.get("response_goal") or "").strip()


def log_pack_a3_truth_hook_failure(
    *,
    hook: str,
    exc: BaseException,
    decision_args: Optional[Mapping[str, Any]] = None,
    tenant_id: Any = None,
    conversation_id: Any = None,
) -> None:
    """Narrow structured log — no MKS bodies / sensitive fact dumps."""
    args = dict(decision_args or {})
    try:
        logger.warning(
            "[PACK_A3_TRUTH_HOOK] hook=%s tenant_id=%s conversation_id=%s "
            "knowledge_kind=%s exception_class=%s err=%s",
            hook,
            tenant_id,
            conversation_id,
            knowledge_kind_from_args(args) or str(args.get("topic") or ""),
            type(exc).__name__,
            str(exc)[:200],
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — logging must not raise
        pass


def safe_apply_knowledge_turn_fact_scope(
    known_facts: Dict[str, Any],
    decision_args: Optional[Mapping[str, Any]],
    *,
    tenant_id: Any = None,
    conversation_id: Any = None,
) -> Dict[str, Any]:
    """Apply kind-specific scope; on failure log + conservative fail-safe."""
    if not is_merchant_knowledge_surface(decision_args):
        return dict(known_facts or {})
    try:
        return apply_knowledge_turn_fact_scope(known_facts, decision_args)
    except Exception as exc:  # noqa: BLE001
        log_pack_a3_truth_hook_failure(
            hook="apply_knowledge_turn_fact_scope",
            exc=exc,
            decision_args=decision_args,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        return apply_conservative_knowledge_fact_scope_failsafe(
            known_facts, decision_args
        )


def safe_scope_merchant_profiles_for_knowledge_turn(
    merchant_profile: Any,
    tenant_profile: Any,
    decision_args: Optional[Mapping[str, Any]],
    *,
    tenant_id: Any = None,
    conversation_id: Any = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Scope profiles; on failure log + strip description for knowledge turns."""
    if not is_merchant_knowledge_surface(decision_args):
        mp = dict(merchant_profile) if isinstance(merchant_profile, dict) else {}
        tp = dict(tenant_profile) if isinstance(tenant_profile, dict) else {}
        return mp, tp
    try:
        return scope_merchant_profiles_for_knowledge_turn(
            merchant_profile, tenant_profile, decision_args
        )
    except Exception as exc:  # noqa: BLE001
        log_pack_a3_truth_hook_failure(
            hook="scope_merchant_profiles_for_knowledge_turn",
            exc=exc,
            decision_args=decision_args,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        return scope_merchant_profiles_failsafe(
            merchant_profile, tenant_profile, decision_args
        )


__all__ = [
    "apply_conservative_knowledge_fact_scope_failsafe",
    "apply_knowledge_turn_fact_scope",
    "authoritative_merchant_knowledge_response_goal",
    "is_merchant_knowledge_surface",
    "knowledge_kind_from_args",
    "knowledge_turn_suppressed_fact_keys",
    "log_pack_a3_truth_hook_failure",
    "merchant_knowledge_response_goal",
    "safe_apply_knowledge_turn_fact_scope",
    "safe_scope_merchant_profiles_for_knowledge_turn",
    "scope_merchant_profiles_failsafe",
    "scope_merchant_profiles_for_knowledge_turn",
    "should_inject_shipping_knowledge_facts",
    "should_request_store_story_knowledge",
    "store_story_capability_args",
]
