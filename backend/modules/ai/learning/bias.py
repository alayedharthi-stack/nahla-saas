"""
modules/ai/learning/bias.py
───────────────────────────
Phase 1.9 — Soft Policy Bias (guarded, narrow, reversible).

The ``PolicyBiasLayer`` is the *first* component in this codebase that
modifies how the brain reaches its reply.  Because of that, every
guard in this file is intentionally over-cautious:

1. Master switch ``LEARNED_POLICY_BIAS_ENABLED`` defaults to ``False``;
   when ``False`` the layer is a pure no-op pass-through.
2. ``PROTECTED_ACTIONS`` and ``PROTECTED_INTENTS`` form a hard-coded
   denylist that bypasses every other gate — no operator setting can
   bias them.
3. Per-intent and per-industry allowlists narrow the scope further on
   top of the readiness verdict.
4. The layer NEVER mutates ``decision.action``.  It only adds keys to
   ``decision.args`` (UI / choice_count / recommendation_style) that
   downstream executors and composers may consume.  Any executor that
   ignores them keeps producing exactly the pre-bias output.
5. Any failure (registry outage, malformed hint, missing ctx field…)
   results in a silent no-op — the inner decision is returned
   unchanged.  No turn is ever broken by the bias layer.

Trace integration
─────────────────
When a bias is applied the layer stamps these keys on
``decision.args`` so ``MemoryUpdater._collect_hint_metadata`` can
record them in the anonymized trace::

    decision.args["bias_applied"]            = True
    decision.args["bias_type"]               = "ui+recommendation_style"
    decision.args["bias_reason"]             = "ready:uplift_0.12"
    decision.args["preferred_ui_mode"]       = "product_cards"
    decision.args["recommendation_style"]    = "cards"
    decision.args["choice_count"]            = 5
    decision.args["bias_intent"]             = "ask_product"
    decision.args["bias_industry"]           = "fashion"

These are categorical / bucketed labels and survive
``validate_anonymized``; raw merchant data is never written here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Iterable, Optional

from ..brain.protocols import DecisionMaker
from ..brain.types import BrainContext, Decision

from .policy_store import PolicyHint
from .readiness import ReadinessVerdict
from .readiness_registry import ReadinessRegistry

logger = logging.getLogger("nahla.ai.learning.bias")


# ── Hard-coded protection lists ─────────────────────────────────────────────

# Action labels that must NEVER be biased.  These cover every action that
# moves money, creates orders, sends payment links, applies discounts, or
# escalates to a human.  Adding a new action that affects money MUST also
# add it here.  Note: matching is case-insensitive and exact.
PROTECTED_ACTIONS: FrozenSet[str] = frozenset({
    "create_order",
    "propose_draft_order",
    "send_payment_link",
    "apply_coupon",
    "suggest_coupon",
    "checkout",
    "complete_order",
    "cancel_order",
    "refund",
    "track_order",
    "handoff",
    "handoff_to_human",
})

# Intent labels that must NEVER be biased even if the action looks safe.
# Mirrors ``readiness.DEFAULT_SENSITIVE_INTENTS`` but is duplicated here to
# keep the bias contract self-contained — the bias layer must remain
# correct even if an operator widens the readiness set.
PROTECTED_INTENTS: FrozenSet[str] = frozenset({
    "checkout",
    "payment",
    "complete_order",
    "objection",
    "complaint",
    "handoff",
    "support_handoff",
    "abandon",
    "abandoned",
    "refund",
    "cancel",
    "cancellation",
})


# ── PolicyBiasLayer ────────────────────────────────────────────────────────

class PolicyBiasLayer(DecisionMaker):
    """Decorator that *augments* an inner ``DecisionMaker``.

    Pre-conditions a caller must satisfy:
      * ``inner.decide(ctx)`` returns a ``Decision`` whose ``args`` is
        a mutable ``dict``.  Standard ``Decision`` instances satisfy
        this by default.

    The layer expects ``decision.args["policy_hint"]`` to be set by an
    earlier ``PolicyOverrideLayer`` in the chain.  When the hint is
    missing the bias layer is a no-op.
    """

    def __init__(
        self,
        inner: DecisionMaker,
        *,
        registry: Optional[ReadinessRegistry] = None,
        allowed_intents: Optional[Iterable[str]] = None,
        allowed_industries: Optional[Iterable[str]] = None,
    ) -> None:
        if inner is None:
            raise ValueError("PolicyBiasLayer requires an inner DecisionMaker")
        self._inner = inner
        self._registry = registry
        self._allowed_intents: Optional[FrozenSet[str]] = (
            frozenset(_norm(i) for i in allowed_intents) if allowed_intents is not None else None
        )
        self._allowed_industries: Optional[FrozenSet[str]] = (
            frozenset(_norm(i) for i in allowed_industries) if allowed_industries is not None else None
        )

    # ── DecisionMaker protocol ─────────────────────────────────────────

    def decide(self, ctx: BrainContext) -> Decision:
        decision = self._inner.decide(ctx)
        try:
            self._maybe_apply_bias(decision, ctx)
        except Exception as exc:  # pragma: no cover — defensive only
            logger.debug("[PolicyBiasLayer] application failed: %s", exc)
        return decision

    # ── Configuration ──────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        """``True`` only when both the master switch and the environment
        gate say "go".  The environment gate prevents an accidental enable
        in production when ``LEARNED_POLICY_BIAS_ENVIRONMENTS=staging``
        (the default).  Use ``LEARNED_POLICY_BIAS_ENVIRONMENTS=*`` to
        opt out of the gate entirely.
        """
        try:
            from core import config as cfg
        except Exception:
            return False
        if not bool(getattr(cfg, "LEARNED_POLICY_BIAS_ENABLED", False)):
            return False
        envs = _split_csv(getattr(cfg, "LEARNED_POLICY_BIAS_ENVIRONMENTS", "*"))
        if envs and "*" not in envs:
            current = _norm(getattr(cfg, "ENVIRONMENT", "production"))
            if not current or current not in envs:
                return False
        return True

    @staticmethod
    def _component_flags() -> Dict[str, bool]:
        """Return the per-component allow flags.

        Defensively defaults to ``{ui: True, choice: True, style: False}``
        — matching the staging rollout plan — when the import fails so the
        layer never silently widens beyond what config sanctions.
        """
        try:
            from core import config as cfg
            return {
                "ui_mode":              bool(getattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_UI_MODE", True)),
                "choice_count":         bool(getattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_CHOICE_COUNT", True)),
                "recommendation_style": bool(getattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_RECOMMENDATION_STYLE", False)),
            }
        except Exception:
            return {"ui_mode": False, "choice_count": False, "recommendation_style": False}

    def allowed_intents(self) -> FrozenSet[str]:
        if self._allowed_intents is not None:
            return self._allowed_intents
        try:
            from core.config import LEARNED_POLICY_BIAS_INTENTS
            return _split_csv(LEARNED_POLICY_BIAS_INTENTS)
        except Exception:
            return frozenset()

    def allowed_industries(self) -> FrozenSet[str]:
        if self._allowed_industries is not None:
            return self._allowed_industries
        try:
            from core.config import LEARNED_POLICY_BIAS_INDUSTRIES
            return _split_csv(LEARNED_POLICY_BIAS_INDUSTRIES)
        except Exception:
            return frozenset({"*"})

    # ── Core decision flow ─────────────────────────────────────────────

    def _maybe_apply_bias(self, decision: Decision, ctx: BrainContext) -> None:
        if not self.is_enabled():
            return

        # Hard guards — bypass every other gate.
        action_lower = _norm(decision.action)
        if not action_lower or action_lower in PROTECTED_ACTIONS:
            return

        intent = _norm(getattr(ctx.intent, "name", ""))
        if not intent or intent in PROTECTED_INTENTS:
            return

        # Per-intent allowlist (operator override).
        allowed_intents = self.allowed_intents()
        if allowed_intents and intent not in allowed_intents:
            return

        # Hint must already be attached by PolicyOverrideLayer.
        if not isinstance(decision.args, dict):
            return
        hint_dict = decision.args.get("policy_hint")
        if not isinstance(hint_dict, dict) or not hint_dict:
            return

        # Per-industry rollout filter.
        industry = _resolve_industry(ctx)
        allowed_industries = self.allowed_industries()
        if allowed_industries and "*" not in allowed_industries:
            if not industry or industry not in allowed_industries:
                return

        # Readiness gate — default-deny on missing / unready bucket.
        registry = self._registry or self._lazy_registry(ctx)
        if registry is None:
            return
        try:
            verdict = registry.get(intent, industry=industry)
        except Exception:
            return
        if verdict is None or not verdict.ready:
            return

        # All gates passed — apply soft bias on whitelisted args only.
        self._apply_soft_bias(decision, hint_dict, intent=intent,
                              industry=industry, verdict=verdict)

    # ── Bias application (args-only) ───────────────────────────────────

    def _apply_soft_bias(
        self,
        decision: Decision,
        hint: dict,
        *,
        intent: str,
        industry: str,
        verdict: ReadinessVerdict,
    ) -> None:
        flags = self._component_flags()
        components: list[str] = []

        ui = _norm(hint.get("recommended_ui"))
        if flags["ui_mode"] and ui and ui != "unknown":
            decision.args["preferred_ui_mode"] = ui
            components.append("ui")

        # ``choice_count`` and ``recommendation_style`` are derived from
        # the recommended UI; keeping the mapping deterministic ensures a
        # given hint always produces the same args.  Any executor that
        # already set ``choice_count`` is preserved (we only ``setdefault``).
        if flags["choice_count"]:
            choice_count = _suggested_choice_count(ui)
            if choice_count is not None:
                existing = decision.args.get("choice_count")
                if existing in (None, 0):
                    decision.args["choice_count"] = choice_count
                    components.append("choice_count")

        if flags["recommendation_style"]:
            rec_style = _suggested_recommendation_style(ui)
            if rec_style:
                decision.args["recommendation_style"] = rec_style
                components.append("recommendation_style")

        # Even when no component matched we still record the attempt so
        # adoption metrics can distinguish "considered but inert" from
        # "never considered".
        decision.args["bias_applied"] = bool(components)
        decision.args["bias_type"]    = "+".join(components) if components else "noop"
        decision.args["bias_reason"]  = _format_reason(verdict)
        decision.args["bias_intent"]  = intent
        decision.args["bias_industry"] = industry or "*"

    # ── Lazy registry build ────────────────────────────────────────────

    def _lazy_registry(self, ctx: BrainContext) -> Optional[ReadinessRegistry]:
        db = getattr(ctx, "_db", None)
        if db is None:
            return None
        try:
            from core.config import LEARNED_POLICY_BIAS_REGISTRY_TTL_SECONDS as _ttl
        except Exception:
            _ttl = 600
        try:
            registry = ReadinessRegistry(db, ttl_seconds=int(_ttl))
            self._registry = registry
            return registry
        except Exception as exc:  # pragma: no cover — defensive only
            logger.debug("[PolicyBiasLayer] registry build failed: %s", exc)
            return None


# ── Helpers (pure, side-effect-free) ────────────────────────────────────────

def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _split_csv(value: Any) -> FrozenSet[str]:
    if not value:
        return frozenset()
    if isinstance(value, (set, frozenset, list, tuple)):
        return frozenset(_norm(v) for v in value if v)
    return frozenset(
        _norm(part) for part in str(value).split(",") if part.strip()
    )


def _resolve_industry(ctx: BrainContext) -> str:
    """Return the lower-cased industry tag, or ``""`` for the global tier."""
    tc = getattr(ctx, "tenant_context", None)
    if tc is not None:
        v = getattr(tc, "industry", "")
        if v:
            return _norm(v)
    facts = getattr(ctx, "facts", None)
    if facts is not None:
        v = getattr(facts, "industry", "")
        if v:
            return _norm(v)
    return ""


def _suggested_choice_count(ui: str) -> Optional[int]:
    """Return a deterministic suggested choice count for a UI mode.

    Returns ``None`` for free-form text replies — the bias layer must
    not fabricate a count where the executor wouldn't render one anyway.
    """
    if ui == "buttons":
        return 3
    if ui == "list":
        return 5
    if ui == "product_cards":
        return 4
    return None


def _suggested_recommendation_style(ui: str) -> str:
    if ui == "product_cards":
        return "cards"
    if ui == "buttons":
        return "compact"
    if ui == "list":
        return "list"
    if ui == "voice":
        return "spoken"
    if ui == "image":
        return "visual"
    return "default"


def _format_reason(verdict: ReadinessVerdict) -> str:
    """Stable, anonymized reason string suitable for trace ``extra``.

    Keeps the format ``ready:uplift_<rounded>:n=<bucket>`` so dashboards
    can group by readiness signal without needing the full verdict.
    """
    sample = verdict.sample_size or 0
    if sample < 100:
        n_bucket = "lt_100"
    elif sample < 500:
        n_bucket = "100_500"
    elif sample < 2000:
        n_bucket = "500_2k"
    else:
        n_bucket = "2k_plus"
    uplift = round(float(verdict.observed_uplift or 0.0), 4)
    return f"ready:uplift_{uplift}:n={n_bucket}"


# ── Pure helper used by tests / dashboards ──────────────────────────────────

def is_action_protected(action: Any) -> bool:
    """Public helper — ``True`` when an action label is in ``PROTECTED_ACTIONS``."""
    return _norm(action) in PROTECTED_ACTIONS


def is_intent_protected(intent: Any) -> bool:
    """Public helper — ``True`` when an intent label is in ``PROTECTED_INTENTS``."""
    return _norm(intent) in PROTECTED_INTENTS


def hint_to_PolicyHint(hint_dict: dict) -> Optional[PolicyHint]:
    """Coerce a dict-shaped hint into a frozen ``PolicyHint`` for callers
    that prefer typed access.  Returns ``None`` on malformed input."""
    if not isinstance(hint_dict, dict):
        return None
    try:
        return PolicyHint(
            scope              = str(hint_dict.get("scope") or "global"),
            industry           = str(hint_dict.get("industry") or "*"),
            intent             = str(hint_dict.get("intent") or "unknown"),
            recommended_action = str(hint_dict.get("recommended_action") or "unknown"),
            recommended_ui     = str(hint_dict.get("recommended_ui") or "unknown"),
            confidence         = float(hint_dict.get("confidence") or 0.0),
            sample_size        = int(hint_dict.get("sample_size") or 0),
        )
    except Exception:
        return None
