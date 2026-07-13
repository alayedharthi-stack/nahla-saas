"""
Layer 2 shadow contracts — PROPOSED / SHADOW CONTRACT only.

Not wired at runtime. No enforcement, telemetry, webhook, Brain, or Compose integration.
"""
from __future__ import annotations

from .builders import build_decision_plan_shadow, build_intent_evidence
from .decision_plan_shadow import (
    CONTRACT_STATUS as DECISION_CONTRACT_STATUS,
    DecisionPlanShadow,
    ProposedActionKind,
    SCHEMA_VERSION as DECISION_SCHEMA_VERSION,
)
from .domain_registry import (
    CONTRACT_STATUS as DOMAIN_CONTRACT_STATUS,
    DomainDefinition,
    FreshnessPolicy,
    OwnerAgent,
    PrivacyClassification,
    domains_for_triggers,
    get_domain_definition,
    list_domain_definitions,
    registered_domain_ids,
)
from .intent_evidence import (
    CONTRACT_STATUS as INTENT_CONTRACT_STATUS,
    AmbiguityState,
    IntentEvidence,
    SCHEMA_VERSION as INTENT_SCHEMA_VERSION,
)

LAYER2_CONTRACT_STATUS = "PROPOSED / SHADOW CONTRACT"

__all__ = [
    "AmbiguityState",
    "DECISION_CONTRACT_STATUS",
    "DECISION_SCHEMA_VERSION",
    "DOMAIN_CONTRACT_STATUS",
    "DecisionPlanShadow",
    "DomainDefinition",
    "FreshnessPolicy",
    "INTENT_CONTRACT_STATUS",
    "INTENT_SCHEMA_VERSION",
    "IntentEvidence",
    "LAYER2_CONTRACT_STATUS",
    "OwnerAgent",
    "PrivacyClassification",
    "ProposedActionKind",
    "build_decision_plan_shadow",
    "build_intent_evidence",
    "domains_for_triggers",
    "get_domain_definition",
    "list_domain_definitions",
    "registered_domain_ids",
]
