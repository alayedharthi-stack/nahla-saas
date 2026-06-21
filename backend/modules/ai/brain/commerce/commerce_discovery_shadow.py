"""
commerce/commerce_discovery_shadow.py
──────────────────────────────────────
Phase 2A — read-only discovery strategy shadow trace.

Observability only: resolves entry + discovery plan without mutating
commerce objective or decision routing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..types import BrainContext
from .commerce_objective import (
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_SELECTION,
    get_commerce_objective,
)
from .discovery_strategy import resolve_discovery_strategy_for_ctx

logger = logging.getLogger("nahla.brain.commerce_discovery_shadow")

_DISCOVERY_SHADOW_OBJECTIVES = frozenset({
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_SELECTION,
})


def _should_trace_shadow(ctx: BrainContext, *, entry_matched: bool) -> bool:
    if entry_matched:
        return True
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "").strip().lower()
    if intent_name in {"start_order", "ask_product", "ask_price", "need_based_product_advice"}:
        return True
    objective = get_commerce_objective(getattr(ctx, "state", None))
    return objective in _DISCOVERY_SHADOW_OBJECTIVES


def trace_commerce_discovery_shadow(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    """
    Shadow-resolve discovery strategy for browse/start-order paths.

    Returns a trace payload when traced; otherwise ``None``.
    """
    from ..discovery.entry import NO_DISCOVERY, resolve_discovery_entry  # noqa: PLC0415

    entry = resolve_discovery_entry(ctx)
    if not _should_trace_shadow(ctx, entry_matched=entry.matched):
        return None

    objective = get_commerce_objective(getattr(ctx, "state", None)) or COMMERCE_OBJECTIVE_DISCOVERY
    plan = resolve_discovery_strategy_for_ctx(ctx)
    entry_type = entry.entry_type if entry.matched else NO_DISCOVERY
    payload: Dict[str, Any] = {
        "entry_type": entry_type,
        "commerce_objective": objective,
        "discovery_mode": plan.mode.value,
        "rule": plan.evidence.get("rule", "-"),
        "shadow": True,
    }
    logger.info(
        "[COMMERCE_DISCOVERY_SHADOW] tenant=%s objective=%s entry=%s mode=%s rule=%s",
        getattr(ctx, "tenant_id", None),
        objective,
        entry_type,
        plan.mode.value,
        payload["rule"],
    )
    return payload


__all__ = ["trace_commerce_discovery_shadow"]
