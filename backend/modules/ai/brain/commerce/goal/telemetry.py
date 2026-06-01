"""Structured telemetry for goal-based commerce."""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger("nahla.brain.goal_commerce")


def log_goal_commerce(
    *,
    tenant_id: Any = None,
    goal: str = "",
    kb_hits: int = 0,
    selected_bundle: str = "",
    resolved_products: int = 0,
    unresolved_products: int = 0,
    retrieval_source: str = "goal_based_recommendation",
    fallback_used: bool = False,
    final_action: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[GOAL_COMMERCE] tenant=%s goal=%s kb_hits=%d selected_bundle=%r "
            "resolved_products=%d unresolved_products=%d retrieval_source=%s "
            "fallback_used=%s final_action=%s preview=%r",
            tenant_id,
            goal or "-",
            int(kb_hits),
            selected_bundle or "-",
            int(resolved_products),
            int(unresolved_products),
            retrieval_source or "-",
            str(bool(fallback_used)).lower(),
            final_action or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_goal_resolution_failed(
    *,
    tenant_id: Any = None,
    goal: str = "",
    ref: str = "",
    reason: str = "",
) -> None:
    try:
        logger.info(
            "[GOAL_COMMERCE_RESOLUTION_FAILED] tenant=%s goal=%s ref=%r reason=%s",
            tenant_id,
            goal or "-",
            (ref or "")[:60],
            reason or "unknown",
        )
    except Exception:  # noqa: BLE001
        pass
