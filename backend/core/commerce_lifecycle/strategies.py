"""
Delivery strategy enums — policy metadata only, no send implementations.
"""
from __future__ import annotations

from enum import Enum


class OpenWindowStrategy(str, Enum):
    SESSION_HANDOFF = "session_handoff"
    NO_MESSAGE = "no_message"
    MERCHANT_TEMPLATE_ONLY = "merchant_template_only"


class ClosedWindowStrategy(str, Enum):
    APPROVED_TEMPLATE = "approved_template"
    BLOCKED = "blocked"
    NO_MESSAGE = "no_message"


class RetryPolicy(str, Enum):
    NONE = "none"
    ONCE = "once"
    EXPONENTIAL = "exponential"


class MerchantModeConstraint(str, Enum):
    WHATSAPP_ONLY = "whatsapp_only"
    EXTERNAL_STORE = "external_store"
    HYBRID = "hybrid"
    ANY = "any"


__all__ = [
    "ClosedWindowStrategy",
    "MerchantModeConstraint",
    "OpenWindowStrategy",
    "RetryPolicy",
]
