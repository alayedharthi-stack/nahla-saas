"""
core/customer_name_adoption_guard.py
────────────────────────────────────
Central gate for adopting a human name onto ``Customer.name`` (or
``proposed_name``) from message-derived or channel-derived inputs.

Operational rule (AGENTS.md): names may become official identity only
from evidence-backed, trusted sources. Outbound echoes, campaigns,
automations, and system-generated text must never adopt names — even
when the string happens to pass the name validator.

This module is intentionally small and deterministic. Personality and
extraction heuristics live elsewhere; this guard is the last line
before ``upsert_customer_identity`` writes identity fields.
"""
from __future__ import annotations

import logging
from typing import FrozenSet, Optional, Tuple

logger = logging.getLogger("nahla.customer_name_adoption_guard")

# ── Sources that must NEVER adopt a name from message/channel text ───────────
UNTRUSTED_MESSAGE_NAME_SOURCES: FrozenSet[str] = frozenset({
    "whatsapp_outbound_echo",
    "outbound",
    "campaign",
    "template",
    "automation",
    "ai_reply",
    "merchant_reply",
    "system",
    "conversation_summary",
    "generated",
})

# ── Sources allowed to set official ``Customer.name`` ────────────────────────
TRUSTED_OFFICIAL_NAME_SOURCES: FrozenSet[str] = frozenset({
    "manual",
    "manual_admin",
    "merchant_correction",
    "salla_sync",
    "customer_webhook",
    "zid_sync",
    "shopify_sync",
    "order_sync",
    "order_webhook",
    "order_incremental",
    "order",
    "manual_import",
    "ai_sales",
})

# ── Inbound customer self-ID (extractor/validator must run upstream) ─────────
INBOUND_EXPLICIT_NAME_SOURCES: FrozenSet[str] = frozenset({
    "ai_detected_name",
})

# ── WhatsApp profile hints — proposed only, never from message text ────────
WHATSAPP_PROFILE_HINT_SOURCES: FrozenSet[str] = frozenset({
    "whatsapp_inbound",
    "whatsapp_lead",
})


def _norm_source(source: Optional[str]) -> str:
    return (source or "").strip().lower()


def is_untrusted_message_name_source(source: Optional[str]) -> bool:
    """True when the source represents outbound/system/generated traffic."""
    return _norm_source(source) in UNTRUSTED_MESSAGE_NAME_SOURCES


def is_trusted_name_adoption_source(
    source: Optional[str],
    *,
    direction: Optional[str] = None,
    explicit_customer_entry: bool = False,
) -> bool:
    """
    Return True when ``upsert_customer_identity`` may consider ``name=…``.

    ``direction`` when provided must be ``inbound`` for message-derived
    adoption (``ai_detected_name``).
    """
    src = _norm_source(source)
    if direction is not None and str(direction).strip().lower() != "inbound":
        return False
    if src in UNTRUSTED_MESSAGE_NAME_SOURCES:
        return False
    if explicit_customer_entry and src in INBOUND_EXPLICIT_NAME_SOURCES:
        return True
    if src in TRUSTED_OFFICIAL_NAME_SOURCES:
        return True
    if src in WHATSAPP_PROFILE_HINT_SOURCES:
        return True
    return False


def filter_name_for_identity_upsert(
    name: Optional[str],
    source: Optional[str],
    *,
    direction: Optional[str] = None,
    explicit_customer_entry: bool = False,
) -> Tuple[Optional[str], str]:
    """
    Decide whether ``name`` may flow into ``upsert_customer_identity``.

    Returns ``(sanitized_name, mode)`` where ``mode`` is one of:
      * ``blocked`` — do not call ``apply_customer_name``
      * ``official`` — full resolver path (trusted / explicit inbound)
      * ``proposed`` — WhatsApp profile hint only
    """
    clean = str(name or "").strip()
    if not clean:
        return None, "blocked"

    src = _norm_source(source)
    if src in UNTRUSTED_MESSAGE_NAME_SOURCES:
        logger.info(
            "[NAME_ADOPTION_GUARD] blocked source=%s name=%r",
            src,
            clean[:60],
        )
        return None, "blocked"

    if direction is not None and str(direction).strip().lower() != "inbound":
        if src in INBOUND_EXPLICIT_NAME_SOURCES or src in WHATSAPP_PROFILE_HINT_SOURCES:
            logger.info(
                "[NAME_ADOPTION_GUARD] blocked non-inbound direction=%s source=%s",
                direction,
                src,
            )
            return None, "blocked"

    if explicit_customer_entry and src in INBOUND_EXPLICIT_NAME_SOURCES:
        return clean, "official"

    if src in TRUSTED_OFFICIAL_NAME_SOURCES:
        return clean, "official"

    if src in WHATSAPP_PROFILE_HINT_SOURCES:
        return clean, "proposed"

    # Unknown sources: never adopt message-like names platform-wide.
    logger.info(
        "[NAME_ADOPTION_GUARD] blocked unknown source=%s name=%r",
        src,
        clean[:60],
    )
    return None, "blocked"
