"""
turn/compose_bridge.py
──────────────────────
Phase 3A — attach OwnerBrief to compose when native brief compose is enabled.

Does not override enforce-injected briefs. Does not write reply text.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from ..types import BrainContext, Decision
from .contract import TurnArbitration
from .flags import is_owner_brief_native_compose_enabled
from .owner_brief import topic_for_owner


def _fact_answer_contract_owns(args: dict[str, Any]) -> bool:
    contract = args.get("answer_contract")
    return isinstance(contract, dict) and bool(
        str(contract.get("fact_kind") or "").strip()
    )


def _persona_social_brief(brief: Any) -> bool:
    if not isinstance(brief, dict):
        return False
    owner = str(brief.get("owner") or "")
    reply_goal = str(brief.get("reply_goal") or "")
    return owner == "persona/social" or reply_goal.startswith("respond_socially")


def resolve_owner_brief_dict(
    decision: Decision,
    ctx: BrainContext,
) -> Optional[dict[str, Any]]:
    """
    Resolve OwnerBrief dict for compose from decision args or ctx arbitration.

    Priority: ``decision.args.owner_brief`` (enforce path) then shadow arbitration.
    """
    args = dict(getattr(decision, "args", None) or {})
    existing = args.get("owner_brief")
    if _fact_answer_contract_owns(args) and _persona_social_brief(existing):
        return None
    if isinstance(existing, dict) and existing:
        return existing
    if _fact_answer_contract_owns(args):
        return None

    if not is_owner_brief_native_compose_enabled():
        return None

    arbitration: Optional[TurnArbitration] = getattr(
        ctx, "turn_arbitration_shadow", None,
    )
    if arbitration is None:
        return None

    return arbitration.owner_brief.to_dict()


def maybe_attach_owner_brief_for_compose(
    decision: Decision,
    ctx: BrainContext,
) -> Tuple[Decision, bool]:
    """
    Attach OwnerBrief to ``decision.args`` when native compose flag is on.

    Returns ``(decision, attached)``. Mutates ``decision.args`` in place when
    attaching; skipped when enforce already injected a brief.
    """
    if not is_owner_brief_native_compose_enabled():
        return decision, False

    args = dict(getattr(decision, "args", None) or {})
    if _fact_answer_contract_owns(args):
        return decision, False
    if isinstance(args.get("owner_brief"), dict) and args.get("owner_brief"):
        return decision, False

    arbitration: Optional[TurnArbitration] = getattr(
        ctx, "turn_arbitration_shadow", None,
    )
    if arbitration is None:
        return decision, False

    brief = arbitration.owner_brief
    owner = arbitration.turn_owner
    args["turn_owner"] = owner
    args["owner_brief"] = brief.to_dict()
    args["compose_mode"] = brief.compose_mode
    args["response_goal"] = brief.reply_goal
    args.setdefault("topic", topic_for_owner(owner))
    args["owner_brief_native_compose"] = True
    decision.args = args
    return decision, True


__all__ = [
    "maybe_attach_owner_brief_for_compose",
    "resolve_owner_brief_dict",
]
