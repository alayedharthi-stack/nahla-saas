"""
Goal-aware retrieval from ``merchant_knowledge_sections``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .goal_schema import GoalKBMetadata

logger = logging.getLogger("nahla.brain.goal_retrieval")

GOAL_KB_KIND = "goal_based_recommendation"


@dataclass
class GoalKBEntry:
    section_id: int
    title: str
    body: str
    metadata: GoalKBMetadata
    priority: int = 100
    matched_goals: List[str] = field(default_factory=list)


def retrieve_goal_recommendations(
    db: Any,
    tenant_id: int,
    goal: str,
    *,
    limit: int = 3,
) -> List[GoalKBEntry]:
    """
    Fetch active ``goal_based_recommendation`` sections matching ``goal``.

    Returns empty list on miss — caller must fail-open.
    """
    if not db or not tenant_id or not goal:
        return []

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GOAL_KB_RETRIEVAL] import failed: %s", exc)
        return []

    try:
        rows = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.is_active.is_(True),
                MerchantKnowledgeSection.kind == GOAL_KB_KIND,
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[GOAL_KB_RETRIEVAL] query failed tenant=%s goal=%s err=%s",
            tenant_id,
            goal,
            exc,
        )
        return []

    goal_norm = (goal or "").strip().lower()
    hits: List[GoalKBEntry] = []

    for row in rows:
        meta = GoalKBMetadata.from_metadata_json(getattr(row, "metadata_json", None))
        if not meta:
            continue
        if goal_norm not in meta.goal_tags:
            continue
        hits.append(
            GoalKBEntry(
                section_id=int(row.id),
                title=str(getattr(row, "title", "") or "").strip(),
                body=str(getattr(row, "body", "") or "").strip(),
                metadata=meta,
                priority=int(getattr(row, "priority", 100) or 100),
                matched_goals=[goal_norm],
            )
        )
        if len(hits) >= limit:
            break

    return hits
