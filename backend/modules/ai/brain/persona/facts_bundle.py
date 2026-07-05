"""Contracts for FactBoundPersonaComposer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Optional

PHASE2_SOCIAL_SURFACES: FrozenSet[str] = frozenset(
    {
        "social_greeting",
        "social_checkin",
        "thanks",
        "dua",
    }
)

PERSONA_SURFACE_PAYMENT_MEDIA_INTRO = "payment_media_intro"
PERSONA_SURFACE_KB_PRODUCT_ANSWER = "kb_product_answer"
PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER = "catalog_product_answer"

PERSONA_COMPOSER_SURFACES: FrozenSet[str] = PHASE2_SOCIAL_SURFACES | frozenset(
    {
        PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
        PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    }
)


@dataclass(frozen=True)
class PersonaConstraints:
    max_chars: int = 180
    language_policy: str = "saudi_arabic_for_ar"
    allow_emoji: bool = True
    require_emoji: bool = False
    max_emojis: int = 1
    tone: str = "warm_saudi_merchant"
    non_deterministic: bool = True
    banned_phrases: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaFactsBundle:
    surface: str
    inbound_text: str
    language: str
    dialect: Optional[str]
    verified_facts: dict[str, Any]
    customer_context: dict[str, Any]
    merchant_persona: dict[str, Any]
    constraints: PersonaConstraints
    tenant_id: int = 0
    customer_phone: str = ""


@dataclass(frozen=True)
class PersonaComposeResult:
    text: str
    source: str  # persona_llm | fallback_deterministic
    surface: str
    facts_hash: str
    guard_passed: bool
    guard_failed_reason: str = ""
    fallback_reason: str = ""
    language: str = "ar"
    dialect: Optional[str] = None
    emoji_count: int = 0
    latency_ms: int = 0
    model: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
