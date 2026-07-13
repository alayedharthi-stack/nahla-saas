"""
core.commerce_lifecycle
───────────────────────
Unified Commerce Lifecycle — Business Intent contracts (PR 2A).

Lifecycle owns:
  * BusinessIntent canonical model
  * evidence requirements and validation
  * capability policy metadata (declarative)
  * delivery strategy declarations
  * structured dispatch outcomes (no prose)

Lifecycle does not own:
  * AI wording, prompts, or route ownership
  * template submission or message sending
  * provider status normalization (future PR 2C)
  * order or conversation mutation
  * runtime dispatch, ledger writes, or service-window resolution
"""
from __future__ import annotations

from core.commerce_lifecycle.contracts import (
    ApprovedTemplateRequired,
    Blocked,
    DispatchOutcome,
    MerchantActionRequired,
    NoNotification,
    SessionMessageRequired,
)
from core.commerce_lifecycle.definitions import BusinessIntentDefinition
from core.commerce_lifecycle.evidence import (
    CapabilityValidationResult,
    EvidenceValidationResult,
    OrderLifecycleEvidence,
    validate_capabilities,
    validate_evidence,
    validate_template_evidence,
)
from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.registry import (
    LifecycleIntentRegistry,
    get_default_registry,
)
from core.commerce_lifecycle.strategies import (
    ClosedWindowStrategy,
    MerchantModeConstraint,
    OpenWindowStrategy,
    RetryPolicy,
)

__all__ = [
    "ApprovedTemplateRequired",
    "Blocked",
    "BusinessIntent",
    "BusinessIntentDefinition",
    "CapabilityValidationResult",
    "ClosedWindowStrategy",
    "DispatchOutcome",
    "EvidenceValidationResult",
    "LifecycleIntentRegistry",
    "MerchantActionRequired",
    "MerchantModeConstraint",
    "NoNotification",
    "OpenWindowStrategy",
    "OrderLifecycleEvidence",
    "RetryPolicy",
    "SessionMessageRequired",
    "get_default_registry",
    "validate_capabilities",
    "validate_evidence",
    "validate_template_evidence",
]
