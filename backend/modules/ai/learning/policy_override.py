"""
modules/ai/learning/policy_override.py
──────────────────────────────────────
``PolicyOverrideLayer`` — a thin decorator around any ``DecisionMaker``.

Purpose
───────
Phase 1.7 wants to expose the freshly-learned global / vertical
policies to the decision pipeline **without** changing how the engine
reaches its primary action.  The layer therefore *augments* a decision
rather than replacing it.

Behavior
────────
1. Always delegate ``inner.decide(ctx)`` first.  If the inner engine
   raised, that exception propagates unchanged.
2. Look up a ``PolicyHint`` for the active ``(intent, industry)``.
   * Industry comes from ``ctx.tenant_context.industry`` first, then
     from ``ctx.facts.industry`` if present, then "" → global tier.
3. If a hint exists, attach it under ``decision.args["policy_hint"]``
   without overwriting any field the inner engine already set.
4. On any error in steps 2-3 the original decision is returned
   unchanged.  The override layer must never break a turn.

Why not change ``decision.action``?
──────────────────────────────────
Keeping the inner action intact preserves every existing test, every
permission gate, every per-tenant policy.  Future phases can choose
when (or whether) to promote the hint to a hard override; today we
only surface it to downstream consumers — composer, suggestion engine,
Brain trace logger — so we can measure adoption without changing
behavior.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..brain.protocols import DecisionMaker
from ..brain.types import BrainContext, Decision

from .policy_store import LearnedPolicyStore, PolicyHint

logger = logging.getLogger("nahla.ai.learning.override")


class PolicyOverrideLayer(DecisionMaker):
    """Decorator: ``inner_engine.decide`` + ``policy_hint`` annotation."""

    def __init__(
        self,
        inner: DecisionMaker,
        *,
        store: Optional[LearnedPolicyStore] = None,
    ) -> None:
        if inner is None:
            raise ValueError("PolicyOverrideLayer requires an inner DecisionMaker")
        self._inner = inner
        self._store = store  # may be None — created lazily per call

    # ── DecisionMaker protocol ─────────────────────────────────────────

    def decide(self, ctx: BrainContext) -> Decision:
        decision = self._inner.decide(ctx)
        try:
            self._annotate(decision, ctx)
        except Exception as exc:  # pragma: no cover — defensive only
            logger.debug("[PolicyOverrideLayer] annotation failed: %s", exc)
        return decision

    # ── Internal ───────────────────────────────────────────────────────

    def _annotate(self, decision: Decision, ctx: BrainContext) -> None:
        store = self._store or self._lazy_store(ctx)
        if store is None:
            return
        if not store.is_enabled():
            return

        intent = getattr(ctx.intent, "name", "") or ""
        industry = _resolve_industry(ctx)
        hint = store.lookup(intent, industry=industry)
        if hint is None:
            return

        # Never clobber an existing hint already attached by another layer.
        existing = decision.args.get("policy_hint") if isinstance(decision.args, dict) else None
        if existing:
            return

        decision.args["policy_hint"] = _hint_to_dict(hint, inner_action=decision.action)

    def _lazy_store(self, ctx: BrainContext) -> Optional[LearnedPolicyStore]:
        db = getattr(ctx, "_db", None)
        if db is None:
            return None
        try:
            store = LearnedPolicyStore(db)
            self._store = store
            return store
        except Exception as exc:  # pragma: no cover — defensive only
            logger.debug("[PolicyOverrideLayer] store build failed: %s", exc)
            return None


def _resolve_industry(ctx: BrainContext) -> str:
    """Best-effort industry derivation; returns ``""`` for the global tier."""
    tc = getattr(ctx, "tenant_context", None)
    if tc is not None:
        value = getattr(tc, "industry", "")
        if value:
            return str(value).strip().lower()
    facts = getattr(ctx, "facts", None)
    if facts is not None:
        value = getattr(facts, "industry", "")
        if value:
            return str(value).strip().lower()
    return ""


def _hint_to_dict(hint: PolicyHint, *, inner_action: str) -> dict:
    """Project a ``PolicyHint`` into the slim shape the rest of the
    pipeline (composer / suggestion / trace logger) reads."""
    return {
        "scope":              hint.scope,
        "industry":           hint.industry,
        "intent":             hint.intent,
        "recommended_action": hint.recommended_action,
        "recommended_ui":     hint.recommended_ui,
        "confidence":         float(hint.confidence),
        "sample_size":        int(hint.sample_size),
        "matches_inner":      hint.recommended_action == (inner_action or ""),
    }
