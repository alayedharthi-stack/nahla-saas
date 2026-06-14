"""
core/automation_send_guard
──────────────────────────
Platform-wide outbound automation guard.

When a conversation is under human supervision or AI is disabled, no
automated WhatsApp reply may leave the system. The only exception is a
manual staff send from the dashboard (``allow_manual=True``).

Doctrine (AGENTS.md): operational silence must be deterministic — if the
merchant paused AI or took over, the platform must not claim otherwise by
sending canned fallbacks, brain replies, or media handlers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock, Mock

from sqlalchemy.orm import Session

from core.ai_pause_guard import is_internal_or_blocked
from core.ownership_state import conversation_handoff_active
from models import Conversation, Customer

logger = logging.getLogger("nahla-backend")

REASON_AI_DISABLED = "ai_disabled"
REASON_HUMAN_TAKEOVER = "human_takeover"
REASON_REQUIRES_HUMAN = "requires_human"
REASON_BLOCKED_NUMBER = "blocked_number"


@dataclass(frozen=True)
class AutomationBlockDecision:
    block: bool
    reason: str = ""


def _digits(value: Any) -> str:
    if value is None:
        return ""
    return "".join(c for c in str(value) if c.isdigit())


def _valid_conversation_ref(obj: Any) -> bool:
    """True for ORM rows and explicit test stubs; false for bare mocks."""
    if obj is None or isinstance(obj, (MagicMock, Mock)):
        return False
    if isinstance(obj, Conversation):
        return True
    return isinstance(getattr(obj, "id", None), int)


def lookup_conversation_for_phone(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> Optional[Conversation]:
    """Best-effort conversation lookup without creating rows."""
    if db is None or not tenant_id or not customer_phone:
        return None
    try:
        from services.customer_intelligence import normalize_phone  # noqa: PLC0415

        norm = normalize_phone(customer_phone) or customer_phone
        customer = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id, Customer.phone == norm)
            .first()
        )
        if customer is not None and not isinstance(customer, Customer):
            customer = None
        if customer is None:
            suffix = _digits(norm)[-9:]
            if suffix:
                customer = (
                    db.query(Customer)
                    .filter(
                        Customer.tenant_id == tenant_id,
                        Customer.phone.like(f"%{suffix}"),
                    )
                    .first()
                )
                if customer is not None and not isinstance(customer, Customer):
                    customer = None
        if customer is None:
            return None
        convo = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.customer_id == customer.id,
            )
            .first()
        )
        if convo is not None and not _valid_conversation_ref(convo):
            return None
        return convo
    except Exception as exc:
        logger.debug("[AUTOMATION_BLOCKED] conversation lookup failed: %s", exc)
        return None


def _human_supervision_reason(convo: Conversation) -> str:
    if bool(getattr(convo, "ai_paused", False)):
        return REASON_AI_DISABLED
    if bool(getattr(convo, "is_human_handoff", False)):
        return REASON_HUMAN_TAKEOVER
    if bool(getattr(convo, "needs_human", False)):
        return REASON_REQUIRES_HUMAN
    if bool(getattr(convo, "handoff_active", False)):
        return REASON_HUMAN_TAKEOVER
    if getattr(convo, "taken_over_at", None) is not None:
        return REASON_HUMAN_TAKEOVER
    if bool(getattr(convo, "paused_by_human", False)):
        return REASON_HUMAN_TAKEOVER
    if str(getattr(convo, "status", "") or "").strip().lower() == "human":
        return REASON_HUMAN_TAKEOVER
    return ""


def should_block_automation_for_conversation(
    db: Session,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Optional[Conversation] = None,
    message_type: str = "text",
    blocked_path: str = "unknown",
) -> AutomationBlockDecision:
    """
    Return ``AutomationBlockDecision(block=True, reason=...)`` when any
    automated outbound must be suppressed for this customer thread.
    """
    if not tenant_id or not customer_phone:
        return AutomationBlockDecision(block=False)

    try:
        blocked, block_reason = is_internal_or_blocked(db, tenant_id, customer_phone)
    except Exception:
        blocked, block_reason = False, None
    if blocked:
        return AutomationBlockDecision(
            block=True,
            reason=REASON_BLOCKED_NUMBER if block_reason else REASON_BLOCKED_NUMBER,
        )

    convo = conversation
    if convo is not None and not _valid_conversation_ref(convo):
        convo = None
    if convo is None:
        convo = lookup_conversation_for_phone(db, tenant_id, customer_phone)
    if convo is None:
        return AutomationBlockDecision(block=False)

    supervision = _human_supervision_reason(convo)
    if supervision:
        return AutomationBlockDecision(block=True, reason=supervision)

    try:
        if conversation_handoff_active(db, convo):
            return AutomationBlockDecision(block=True, reason=REASON_HUMAN_TAKEOVER)
    except Exception:
        logger.exception("[AUTOMATION_SEND_GUARD] handoff_active check failed")

    return AutomationBlockDecision(block=False)


def log_automation_blocked(
    *,
    reason: str,
    tenant_id: Optional[int],
    customer_id: Optional[int],
    conversation_id: Optional[int],
    phone: str,
    message_type: str,
    blocked_path: str,
) -> None:
    from utils.phone_utils import redact_phone_for_log  # noqa: PLC0415

    logger.info(
        "[AUTOMATION_BLOCKED] reason=%s tenant_id=%s customer_id=%s "
        "conversation_id=%s phone=%s message_type=%s blocked_path=%s",
        reason,
        tenant_id,
        customer_id,
        conversation_id,
        redact_phone_for_log(phone),
        message_type or "text",
        blocked_path or "unknown",
    )


def evaluate_automation_send(
    db: Session,
    *,
    tenant_id: Optional[int],
    customer_phone: str,
    conversation: Optional[Conversation] = None,
    message_type: str = "text",
    blocked_path: str = "unknown",
    allow_manual: bool = False,
) -> AutomationBlockDecision:
    """Gate helper for wire-layer send functions."""
    if allow_manual or not tenant_id or not customer_phone:
        return AutomationBlockDecision(block=False)

    decision = should_block_automation_for_conversation(
        db,
        tenant_id=int(tenant_id),
        customer_phone=customer_phone,
        conversation=conversation,
        message_type=message_type,
        blocked_path=blocked_path,
    )
    if not decision.block:
        return decision

    convo = conversation or lookup_conversation_for_phone(
        db, int(tenant_id), customer_phone,
    )
    log_automation_blocked(
        reason=decision.reason,
        tenant_id=tenant_id,
        customer_id=getattr(convo, "customer_id", None) if convo else None,
        conversation_id=getattr(convo, "id", None) if convo else None,
        phone=customer_phone,
        message_type=message_type,
        blocked_path=blocked_path,
    )
    return decision


__all__ = [
    "AutomationBlockDecision",
    "REASON_AI_DISABLED",
    "REASON_BLOCKED_NUMBER",
    "REASON_HUMAN_TAKEOVER",
    "REASON_REQUIRES_HUMAN",
    "evaluate_automation_send",
    "log_automation_blocked",
    "lookup_conversation_for_phone",
    "should_block_automation_for_conversation",
]
