"""
Orchestrator: detect goal → retrieve KB → compose bundle.

Used by pipeline before decision engine; fail-open when no KB hit.
"""
from __future__ import annotations

from typing import Any, Optional

from .bundle_composition import RegimenBundle, compose_regimen_bundle
from .goal_reasoning import GoalMatch, detect_customer_goal
from .goal_retrieval import retrieve_goal_recommendations
from .telemetry import log_goal_commerce


def prepare_goal_regimen_bundle(
    db: Any,
    tenant_id: int,
    message: str,
    *,
    canonical_message: str = "",
) -> tuple[Optional[RegimenBundle], Optional[GoalMatch], int]:
    """
    Try to build a structured regimen bundle for this turn.

    Returns ``(bundle, goal_match, kb_hits)``. ``bundle`` is ``None`` on miss.
    """
    texts = [message or "", canonical_message or ""]
    goal_match: Optional[GoalMatch] = None
    for text in texts:
        if not text.strip():
            continue
        goal_match = detect_customer_goal(text)
        if goal_match is not None:
            break

    if goal_match is None:
        return None, None, 0

    entries = retrieve_goal_recommendations(db, tenant_id, goal_match.goal)
    kb_hits = len(entries)
    if not entries:
        log_goal_commerce(
            tenant_id=tenant_id,
            goal=goal_match.goal,
            kb_hits=0,
            fallback_used=True,
            final_action="pending",
            preview=message or "",
        )
        return None, goal_match, 0

    bundle = compose_regimen_bundle(db, tenant_id, goal_match.goal, entries[0])
    if bundle.resolved_count <= 0:
        log_goal_commerce(
            tenant_id=tenant_id,
            goal=goal_match.goal,
            kb_hits=kb_hits,
            selected_bundle=bundle.title,
            resolved_products=0,
            unresolved_products=len(bundle.unresolved_refs),
            fallback_used=True,
            final_action="pending",
            preview=message or "",
        )
        return None, goal_match, kb_hits

    return bundle, goal_match, kb_hits
