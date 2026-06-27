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
import re
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

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
    "merchant_manual",
    "merchant_correction",
    "salla",
    "salla_sync",
    "customer_webhook",
    "zid",
    "zid_sync",
    "shopify",
    "shopify_sync",
    "commerce_platform",
    "sales_channel",
    "platform_verified",
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

COMMERCE_PLATFORM_NAME_SOURCES: FrozenSet[str] = frozenset({
    "salla",
    "salla_order",
    "salla_sync",
    "zid",
    "zid_order",
    "zid_sync",
    "shopify",
    "shopify_order",
    "shopify_sync",
    "commerce_platform",
    "sales_channel",
    "platform_verified",
    "customer_webhook",
    "order_webhook",
    "order_sync",
    "order_incremental",
    "order",
})

MERCHANT_MANUAL_NAME_SOURCES: FrozenSet[str] = frozenset({
    "merchant_manual",
    "merchant_correction",
    "manual",
    "manual_admin",
})

CUSTOMER_ENTERED_NAME_SOURCES: FrozenSet[str] = frozenset({
    "customer_message",
    "ai_detected_name",
})

_ROLE_CONTEXT_RE = re.compile(
    r"(?:"
    r"\b(?:smsa|aramex|spl|naqel|courier|delivery|shipping)\b|"
    r"مندوب|سمسا|ارامكس|أرامكس|ناقل|توصيل|الشحن|شحن|شركة|"
    r"خدمة\s*العملاء|موظف|المعرض|الاداره|الإدارة|"
    r"انا\s*في\s*الموقع|أنا\s*في\s*الموقع|في\s*الموقع"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_NAME_CORRECTION_RE = re.compile(
    r"(?:"
    r"اسمي\s*الصحيح|إسمي\s*الصحيح|"
    r"صحح\s*اسمي|صحح\s*الاسم|"
    r"غير\s*اسمي|غيّر\s*اسمي|غير\s*الاسم|غيّر\s*الاسم|"
    r"الاسم\s*المسجل\s*خط[اأأء]|الاسم\s*عندكم\s*خط[اأأء]"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm_source(source: Optional[str]) -> str:
    return (source or "").strip().lower()


def contains_customer_name_role_context(text: Optional[str]) -> bool:
    """True when text is a role/logistics/context phrase, not a human name."""
    return bool(_ROLE_CONTEXT_RE.search(str(text or "")))


def is_explicit_name_correction_message(text: Optional[str]) -> bool:
    """True only for clear customer statements correcting a stored name."""
    return bool(_EXPLICIT_NAME_CORRECTION_RE.search(str(text or "")))


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


def _message_context_dict(message_context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return dict(message_context or {})


def can_ai_update_customer_name(
    customer: Any,
    candidate_name: Optional[str],
    message_context: Optional[Mapping[str, Any]] = None,
) -> bool:
    """
    Decide whether AI/customer-message evidence may update ``Customer.name``.

    Source priority:
    merchant manual non-empty > commerce platform > validated customer-entered
    > WhatsApp profile / AI candidate.  Role/logistics contexts never win.
    """
    candidate = str(candidate_name or "").strip()
    if not candidate:
        return False

    ctx = _message_context_dict(message_context)
    inbound_text = str(
        ctx.get("message")
        or ctx.get("inbound_text")
        or ctx.get("raw_message")
        or "",
    ).strip()
    source = _norm_source(ctx.get("source"))
    explicit_customer_entry = bool(ctx.get("explicit_customer_entry"))

    if contains_customer_name_role_context(candidate) or contains_customer_name_role_context(inbound_text):
        logger.info(
            "[NAME_ADOPTION_GUARD] blocked role_context source=%s candidate=%r",
            source,
            candidate[:60],
        )
        return False

    try:
        from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

        validation = validate_customer_name(candidate)
        if not validation.valid:
            return False
    except Exception:  # noqa: BLE001
        logger.exception("[NAME_ADOPTION_GUARD] validator unavailable")
        return False

    if customer is None:
        return explicit_customer_entry or source in CUSTOMER_ENTERED_NAME_SOURCES

    try:
        from core.customer_identity_resolver import (  # noqa: PLC0415
            STATUS_CUSTOMER_ENTERED,
            read_customer_identity,
        )

        snap = read_customer_identity(customer)
        current_status = str(snap.customer_name_status or "").strip().lower()
        current_source = _norm_source(snap.customer_name_source)
    except Exception:  # noqa: BLE001
        logger.exception("[NAME_ADOPTION_GUARD] current identity read failed")
        current_status = ""
        current_source = _norm_source(
            (getattr(customer, "extra_metadata", None) or {}).get("customer_name_source")
        )

    meta = dict(getattr(customer, "extra_metadata", None) or {})
    current_name = str(getattr(customer, "name", None) or "").strip()
    manual_cleared = bool(meta.get("manual_name_cleared"))
    manual_override = bool(meta.get("manual_name_override"))

    if current_name and (
        current_source in MERCHANT_MANUAL_NAME_SOURCES
        or (manual_override and not manual_cleared)
    ):
        return False

    if current_name and current_source in COMMERCE_PLATFORM_NAME_SOURCES:
        return False

    explicit_correction = bool(ctx.get("explicit_name_correction")) or is_explicit_name_correction_message(inbound_text)

    if current_name and current_status == STATUS_CUSTOMER_ENTERED:
        return explicit_correction

    if manual_override and manual_cleared and not current_name:
        return explicit_customer_entry or explicit_correction

    if not current_name:
        return explicit_customer_entry or explicit_correction or source in CUSTOMER_ENTERED_NAME_SOURCES

    if current_source in {"", "whatsapp_profile", "whatsapp_inbound", "whatsapp_lead"}:
        return explicit_customer_entry or explicit_correction

    return explicit_correction
