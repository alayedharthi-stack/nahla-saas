"""
core/ai_disabled_gate.py
────────────────────────
P0 kill switch — when AI is disabled or the conversation is under human
supervision, suppress ALL automated inbound processing and outbound sends.

Uses the same multi-row phone lookup as the dashboard pause API so a
sibling Conversation row cannot bypass the merchant's toggle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from models import Conversation

logger = logging.getLogger("nahla-backend")

REASON_AI_PAUSED = "ai_paused"
REASON_MANUAL_PAUSE = "manual_pause"
REASON_HUMAN_SUPERVISION = "human_supervision"
REASON_STORE_AI_DISABLED = "store_ai_disabled"


@dataclass(frozen=True)
class AIDisabledDecision:
    disabled: bool
    reason: str = ""
    conversation: Optional[Conversation] = None
    source: str = ""


@dataclass(frozen=True)
class NoAIReplyResult:
    suppressed: bool = True
    reason: str = ""
    conversation_id: Optional[int] = None


def disabled_reason_for_conversation(convo: Conversation | None) -> str:
    """Return a non-empty reason when automated AI must not reply."""
    if convo is None:
        return ""

    if bool(getattr(convo, "ai_paused", False)):
        return str(getattr(convo, "ai_paused_reason", None) or REASON_AI_PAUSED)

    if bool(getattr(convo, "is_human_handoff", False)):
        return REASON_HUMAN_SUPERVISION
    if bool(getattr(convo, "needs_human", False)):
        return REASON_HUMAN_SUPERVISION
    if bool(getattr(convo, "handoff_active", False)):
        return REASON_HUMAN_SUPERVISION
    if getattr(convo, "taken_over_at", None) is not None:
        return REASON_HUMAN_SUPERVISION
    if bool(getattr(convo, "paused_by_human", False)):
        return REASON_HUMAN_SUPERVISION
    if str(getattr(convo, "status", "") or "").strip().lower() == "human":
        return REASON_HUMAN_SUPERVISION

    return ""


def is_store_ai_enabled(db: Session, tenant_id: int) -> bool:
    """Return True when the merchant has store-wide AI replies enabled."""
    from core.tenant import get_or_create_settings, merge_ai_defaults  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    ai = merge_ai_defaults(settings.ai_settings)
    raw = ai.get("store_ai_enabled", True)
    if raw is None:
        return True
    return bool(raw)


def _find_conversations_for_phone(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> list[Conversation]:
    from routers.conversations import (  # noqa: PLC0415
        _find_conversations_for_phone as _find,
    )

    return _find(db, tenant_id, customer_phone)


def is_ai_disabled_for_conversation(
    db: Session,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Conversation | None = None,
    source: str = "unknown",
) -> AIDisabledDecision:
    """
    Aggregate kill-switch check across ALL conversation rows for a phone.

    Mirrors dashboard pause semantics: if ANY matching row is paused or
    under human supervision, automated AI is disabled for the thread.

    Store-wide pause is checked first and does not mutate per-conversation
    ai_paused flags — individual pauses remain intact when the store toggle
    is turned back on.
    """
    if not is_store_ai_enabled(db, tenant_id):
        convos = _find_conversations_for_phone(db, tenant_id, customer_phone)
        anchor = conversation
        if anchor is None and convos:
            anchor = convos[0]
        logger.info(
            "[AI_DISABLED_GATE] reason=%s tenant_id=%s source=%s phone=%s conversation_id=%s",
            REASON_STORE_AI_DISABLED,
            tenant_id,
            source,
            customer_phone,
            getattr(anchor, "id", None),
        )
        return AIDisabledDecision(
            disabled=True,
            reason=REASON_STORE_AI_DISABLED,
            conversation=anchor,
            source=source,
        )

    convos = _find_conversations_for_phone(db, tenant_id, customer_phone)
    if conversation is not None:
        known_ids = {getattr(c, "id", None) for c in convos}
        if getattr(conversation, "id", None) not in known_ids:
            convos = list(convos) + [conversation]

    if not convos:
        return AIDisabledDecision(disabled=False, conversation=conversation)

    for convo in convos:
        reason = disabled_reason_for_conversation(convo)
        if reason:
            return AIDisabledDecision(
                disabled=True,
                reason=reason,
                conversation=convo,
                source=source,
            )

    return AIDisabledDecision(
        disabled=False,
        conversation=convos[0],
        source=source,
    )


def log_ai_disabled_gate(
    *,
    tenant_id: int,
    customer_phone: str,
    decision: AIDisabledDecision,
    source: str,
) -> None:
    convo = decision.conversation
    logger.info(
        "[AI_DISABLED_GATE] suppressed_ai_reply tenant_id=%s conversation_id=%s "
        "customer_id=%s phone=%s source=%s reason=%s",
        tenant_id,
        getattr(convo, "id", None),
        getattr(convo, "customer_id", None),
        customer_phone,
        source,
        decision.reason or "unknown",
    )


def log_ai_disabled_send_block(
    *,
    tenant_id: int,
    customer_phone: str,
    decision: AIDisabledDecision,
    blocked_path: str,
) -> None:
    convo = decision.conversation
    logger.warning(
        "[AI_DISABLED_SEND_BLOCK] prevented_outbound_after_pipeline tenant_id=%s "
        "conversation_id=%s customer_id=%s phone=%s blocked_path=%s reason=%s",
        tenant_id,
        getattr(convo, "id", None),
        getattr(convo, "customer_id", None),
        customer_phone,
        blocked_path or "unknown",
        decision.reason or "unknown",
    )


def persist_inbound_for_suppressed_turn(
    db: Session,
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_body: str,
    wa_msg_id: str | None = None,
    wa_message_ts: Any = None,
    inbound_metadata: dict[str, Any] | None = None,
) -> Conversation:
    """Save inbound only — no outbound, no brain, no arbiter."""
    from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
    from core.conversation_engine import StateManager  # noqa: PLC0415

    convo = _get_or_create_conversation(db, tenant_id, customer_phone)
    if convo.status != "human" and not convo.is_human_handoff:
        convo.status = "active"
    db.add(convo)
    db.flush()

    meta: dict[str, Any] = {
        "message_origin": "live_webhook",
        "historical_import": False,
        "ai_disabled_gate": True,
    }
    if wa_msg_id:
        meta["wa_message_id"] = wa_msg_id
    if wa_message_ts is not None and hasattr(wa_message_ts, "isoformat"):
        meta["whatsapp_timestamp"] = wa_message_ts.isoformat()
    if inbound_metadata:
        meta.update(inbound_metadata)

    StateManager.save_message(
        db,
        customer_phone,
        (inbound_body or "").strip(),
        "inbound",
        conversation_id=convo.id,
        tenant_id=tenant_id,
        extra_metadata=meta,
    )
    return convo


def no_ai_reply_result(decision: AIDisabledDecision) -> NoAIReplyResult:
    return NoAIReplyResult(
        suppressed=True,
        reason=decision.reason,
        conversation_id=getattr(decision.conversation, "id", None),
    )


def evaluate_ai_disabled_send_block(
    db: Session,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Conversation | None = None,
    blocked_path: str = "unknown",
    allow_manual: bool = False,
) -> Tuple[bool, AIDisabledDecision]:
    """Second-layer send protection. Manual dashboard sends may bypass."""
    if allow_manual:
        return False, AIDisabledDecision(disabled=False)

    decision = is_ai_disabled_for_conversation(
        db,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        conversation=conversation,
        source=blocked_path,
    )
    if decision.disabled:
        log_ai_disabled_send_block(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            decision=decision,
            blocked_path=blocked_path,
        )
        return True, decision
    return False, decision


__all__ = [
    "AIDisabledDecision",
    "NoAIReplyResult",
    "REASON_AI_PAUSED",
    "REASON_HUMAN_SUPERVISION",
    "REASON_MANUAL_PAUSE",
    "REASON_STORE_AI_DISABLED",
    "disabled_reason_for_conversation",
    "evaluate_ai_disabled_send_block",
    "is_ai_disabled_for_conversation",
    "is_store_ai_enabled",
    "log_ai_disabled_gate",
    "log_ai_disabled_send_block",
    "no_ai_reply_result",
    "persist_inbound_for_suppressed_turn",
]
