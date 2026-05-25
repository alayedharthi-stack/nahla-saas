"""
brain/relational
────────────────
Relational layer for Nahla's conversational commerce engine.

This package owns ONE responsibility: classify the relational
moment of the customer this turn, produce a typed verdict downstream
layers may consume to adjust framing / suppression / prioritisation —
but NEVER to fabricate business state.

See :mod:`relational.state` for the public function and dataclass.
See :mod:`relational.contracts` for the architectural invariants
that any future change must respect.
"""
from __future__ import annotations

from .contracts import (
    ARCHITECTURAL_RULE_TEXT,
    BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS,
    FORBIDDEN_SIDE_EFFECT_SYMBOLS,
    RELATIONAL_LAYER_PERMITTED_OUTPUTS,
)
from .moments import (
    ALL_MOMENTS,
    ConversationMoment,
    LifecycleStage,
    PostPurchaseWindow,
    Sentiment,
    Urgency,
)
from .state import (
    RELATIONAL_LAYER_RULE,
    RelationalState,
    compute_relational_state,
    log_relational_state,
)
from .decision_router import (
    RESPONSE_GOAL_APPRECIATION_ACK,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING,
    RESPONSE_GOAL_TRUST_BUILDING,
    apply_relational_preference,
    is_decision_router_enabled,
)


__all__ = [
    "ConversationMoment",
    "ALL_MOMENTS",
    "LifecycleStage",
    "PostPurchaseWindow",
    "Sentiment",
    "Urgency",
    "RelationalState",
    "compute_relational_state",
    "log_relational_state",
    "apply_relational_preference",
    "is_decision_router_enabled",
    "RESPONSE_GOAL_APPRECIATION_ACK",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT",
    "RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING",
    "RESPONSE_GOAL_TRUST_BUILDING",
    "BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS",
    "RELATIONAL_LAYER_PERMITTED_OUTPUTS",
    "FORBIDDEN_SIDE_EFFECT_SYMBOLS",
    "ARCHITECTURAL_RULE_TEXT",
    "RELATIONAL_LAYER_RULE",
]
