"""
clarification/evidence.py
─────────────────────────
Build structured evidence from BrainContext for classification and compose.
"""
from __future__ import annotations

from typing import Any, Dict

from ..types import BrainContext


def build_clarification_evidence(
    ctx: BrainContext,
    *,
    trigger: str = "",
) -> Dict[str, Any]:
    """Sanitized operational facts — safe for logs and LLM evidence blocks."""
    intent = getattr(ctx, "intent", None)
    state = getattr(ctx, "state", None)
    facts = getattr(ctx, "facts", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    slots = dict(getattr(intent, "slots", None) or {})

    candidates = list(getattr(state, "last_search_candidates", None) or [])
    candidate_titles = [
        str((c or {}).get("title") or "").strip()
        for c in candidates[:6]
        if str((c or {}).get("title") or "").strip()
    ]

    return {
        "trigger": str(trigger or "").strip(),
        "intent_name": str(getattr(intent, "name", "") or ""),
        "intent_confidence": float(getattr(intent, "confidence", 0.0) or 0.0),
        "stage": str(getattr(state, "stage", "") or ""),
        "greeted": bool(getattr(state, "greeted", False)),
        "has_product_focus": bool(focus),
        "product_focus_title": str(focus.get("title") or "").strip(),
        "product_focus_id": str(focus.get("id") or focus.get("product_id") or "").strip(),
        "has_last_browse_query": bool(str(getattr(state, "last_browse_query", "") or "").strip()),
        "last_browse_query": str(getattr(state, "last_browse_query", "") or "").strip()[:80],
        "search_candidate_count": len(candidates),
        "search_candidate_titles": candidate_titles,
        "has_order_prep": bool(getattr(state, "order_prep", None)),
        "slot_product_query": str(
            slots.get("product_query") or slots.get("product_name") or ""
        ).strip()[:80],
        "catalog_available": bool(getattr(facts, "has_products", False)),
        "catalog_product_count": int(getattr(facts, "product_count", 0) or 0),
        "customer_goal": str(getattr(state, "customer_goal", "") or "").strip()[:80],
        "last_action": str(getattr(state, "last_action", "") or "").strip(),
    }


__all__ = ["build_clarification_evidence"]
