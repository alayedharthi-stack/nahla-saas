"""
core/automation_send_guard
──────────────────────────
Platform-wide outbound automation guard.

Automated WhatsApp replies are blocked when AI is explicitly paused,
store AI is disabled, the number is blocked, or another platform/safety
gate forbids send. Human/staff activity and ownership labels
(HUMAN_ACTIVE / HUMAN_REQUESTED) must not silence the wire.

Doctrine (AGENTS.md): operational silence must be deterministic — if the
merchant explicitly paused AI, the platform must not send. Customer
escalation ≠ AI off. Manual staff reply ≠ AI off. Advisory queue ≠
automation kill-switch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock, Mock

from sqlalchemy.orm import Session

from core.ai_pause_guard import is_internal_or_blocked
from models import Conversation, Customer

logger = logging.getLogger("nahla-backend")

REASON_AI_DISABLED = "ai_disabled"
REASON_HUMAN_TAKEOVER = "human_takeover"
REASON_REQUIRES_HUMAN = "requires_human"
REASON_BLOCKED_NUMBER = "blocked_number"
REASON_STORE_AI_DISABLED = "store_ai_disabled"
REASON_STORE_AI_TEST_MODE_NOT_ALLOWED = "store_ai_test_mode_not_allowed"


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
        from core.ai_disabled_gate import is_ai_disabled_for_conversation  # noqa: PLC0415

        decision = is_ai_disabled_for_conversation(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            source="automation_send_guard_lookup",
        )
        if decision.conversation is not None:
            return decision.conversation
    except Exception as exc:  # noqa: silent-ok — fall back to legacy phone lookup
        logger.debug("[AUTOMATION_BLOCKED] aggregate lookup failed: %s", exc)

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


def _explicit_ai_disabled_reason(convo: Conversation) -> str:
    if bool(getattr(convo, "ai_paused", False)):
        return REASON_AI_DISABLED
    return ""


def _human_takeover_reason(_db: Session, _convo: Conversation) -> str:
    """Human/staff activity is not an outbound automation blocker.

    Conversation-level off is ``ai_paused`` (checked separately).
    HUMAN_REQUESTED / HUMAN_ACTIVE / implicit residue must not block.
    """
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
        from core.ai_disabled_gate import is_ai_allowed_by_store_mode  # noqa: PLC0415

        mode_decision = is_ai_allowed_by_store_mode(
            db, int(tenant_id), customer_phone,
        )
        if not mode_decision.allowed:
            return AutomationBlockDecision(
                block=True,
                reason=mode_decision.reason or REASON_STORE_AI_DISABLED,
            )
    except Exception as exc:  # noqa: silent-ok — fall through to per-convo checks
        logger.debug("[AUTOMATION_BLOCKED] store_ai mode check failed: %s", exc)

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

    paused = _explicit_ai_disabled_reason(convo)
    if paused:
        return AutomationBlockDecision(block=True, reason=paused)

    takeover = _human_takeover_reason(db, convo)
    if takeover:
        return AutomationBlockDecision(block=True, reason=takeover)

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
    if decision.reason in {
        REASON_AI_DISABLED,
        REASON_STORE_AI_DISABLED,
        REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
    }:
        from core.ai_disabled_gate import (  # noqa: PLC0415
            AIDisabledDecision,
            log_ai_disabled_send_block,
        )

        log_ai_disabled_send_block(
            tenant_id=int(tenant_id),
            customer_phone=customer_phone,
            decision=AIDisabledDecision(
                disabled=True,
                reason=decision.reason,
                conversation=convo,
                source=blocked_path or "unknown",
            ),
            blocked_path=blocked_path or "unknown",
        )
    else:
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
    "REASON_STORE_AI_DISABLED",
    "REASON_STORE_AI_TEST_MODE_NOT_ALLOWED",
    "evaluate_automation_send",
    "log_automation_blocked",
    "lookup_conversation_for_phone",
    "should_block_automation_for_conversation",
]
