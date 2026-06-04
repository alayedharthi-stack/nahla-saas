"""
Persona expression helpers — Phase 3A compose profile (routing unchanged).

Subtracts commerce-oriented prompt layers on ``persona_identity`` /
``persona_social`` turns. Behavioral guidance only — no canned Arabic.
"""
from __future__ import annotations

from typing import Optional

PERSONA_TOPIC_IDENTITY = "persona_identity"
PERSONA_TOPIC_SOCIAL = "persona_social"

PERSONA_TOPICS = frozenset({PERSONA_TOPIC_IDENTITY, PERSONA_TOPIC_SOCIAL})

_KIND_GUIDANCE: dict[str, str] = {
    "affection": (
        "Energy: warm reciprocal — acknowledge the feeling modestly in Saudi "
        "tone; no romantic escalation, no sales pivot, no support boilerplate."
    ),
    "appearance": (
        "Energy: modest friendly acknowledgment — light deflection or "
        "kindness mirror; no over-flattery, no poetic Gulf-generic lines."
    ),
    "tease": (
        "Energy: light playful pushback — match tease with tease, not apology "
        "or customer-service tone; humor and mild comeback are welcome."
    ),
    "upset": (
        "Energy: gentle light repair — acknowledge without groveling; no "
        "support-ticket tone, staff escalation promise, or discount offer."
    ),
    "social": (
        "Energy: warm conversational Saudi personality — natural and human, "
        "not merchant FAQ or sales assistant."
    ),
}


def persona_topic_from_decision_args(args: Optional[dict]) -> str:
    """Return ``persona_identity`` / ``persona_social`` or ``""``."""
    topic = str((args or {}).get("topic") or "").strip()
    if topic in PERSONA_TOPICS:
        return topic
    return ""


def is_persona_expression_topic(topic: str) -> bool:
    return str(topic or "").strip() in PERSONA_TOPICS


def persona_kind_guidance(persona_kind: str) -> str:
    key = str(persona_kind or "social").strip().lower() or "social"
    return _KIND_GUIDANCE.get(key, _KIND_GUIDANCE["social"])


def compose_persona_identity_goal() -> str:
    return (
        "persona_identity — Generate a short natural Saudi Arabic WhatsApp "
        "reply. The customer is asking who you are, whether you are Nahla, "
        "a bot, AI, or human, or is playfully probing (e.g. «تنامين؟»). "
        "Answer in Nahla's warm playful persona: 1–3 short lines, "
        "conversational Saudi tone, emotionally natural — not support "
        "boilerplate. "
        "For sleep/playful probes: banter naturally as Nahla — avoid "
        "system/support phrasing and avoid «digital assistant always "
        "available» boilerplate. "
        "Do NOT use onboarding bullet lists or enumerate product/price/"
        "shipping/order capabilities. "
        "Do NOT pitch products, prices, checkout, or catalog items. "
        "Do NOT use rigid FAQ phrasing such as «تحت أمرك» as the whole "
        "reply or «نظام ذكاء اصطناعي» boilerplate. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]."
    )


def compose_persona_social_goal(persona_kind: str) -> str:
    pk = str(persona_kind or "social").strip() or "social"
    guidance = persona_kind_guidance(pk)
    return (
        f"persona_social — Generate a short natural Saudi Arabic WhatsApp "
        f"reply to a social/personality message (persona_kind={pk}). "
        f"{guidance} "
        "Respond in 1–3 short lines — not support boilerplate, not a sales "
        "pitch. "
        "Do NOT pitch products, prices, checkout, or catalog items. "
        "Do NOT use onboarding bullet lists or enumerate store capabilities. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]. "
        "Do NOT use rigid FAQ phrasing such as «تحت أمرك» as the whole reply."
    )


__all__ = [
    "PERSONA_KIND_GUIDANCE",
    "PERSONA_TOPIC_IDENTITY",
    "PERSONA_TOPIC_SOCIAL",
    "PERSONA_TOPICS",
    "compose_persona_identity_goal",
    "compose_persona_social_goal",
    "is_persona_expression_topic",
    "persona_kind_guidance",
    "persona_topic_from_decision_args",
]

# Exported for tests that assert keys exist.
PERSONA_KIND_GUIDANCE = _KIND_GUIDANCE
