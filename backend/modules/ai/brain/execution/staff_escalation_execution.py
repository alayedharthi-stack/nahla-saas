"""Staff-escalation execution: durable queue + structured facts.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Platform owns operational status. The model owns customer wording.
Does not pause AI. Does not invent phones, assignment, or notification.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from modules.ai.brain.types import ActionResult, BrainContext, Decision

logger = logging.getLogger("nahla.brain.staff_escalation_execution")

STATUS_REQUESTED = "requested"
STATUS_QUEUED = "queued"
STATUS_ASSIGNED = "assigned"
STATUS_NOTIFIED = "notified"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"

_CLOSED_STATUSES = frozenset(
    {
        STATUS_REQUESTED,
        STATUS_QUEUED,
        STATUS_ASSIGNED,
        STATUS_NOTIFIED,
        STATUS_FAILED,
        STATUS_UNAVAILABLE,
    }
)

_DEFAULT_HANDOFF_SETTINGS: Dict[str, Any] = {
    "notification_method": "webhook",
    "webhook_url": "",
    "staff_whatsapp": "",
}

NOTIFY_NOT_ATTEMPTED = "not_attempted"
NOTIFY_UNAVAILABLE = "unavailable"
NOTIFY_ACCEPTED = "accepted"
NOTIFY_FAILED = "failed"

_CLOSED_NOTIFY_STATUSES = frozenset(
    {
        NOTIFY_NOT_ATTEMPTED,
        NOTIFY_UNAVAILABLE,
        NOTIFY_ACCEPTED,
        NOTIFY_FAILED,
    }
)

_NEUTRAL_CUSTOMER_NAME = ""


@dataclass(frozen=True)
class NotificationOutcome:
    attempted: bool
    accepted: bool
    status: str
    failure_code: str = ""
    reused_previous: bool = False


def _flag(value: Any) -> str:
    return "true" if value is True else "false"


def format_staff_escalation_facts_overlay(data: Dict[str, Any]) -> str:
    """Structured operational facts for compose. No customer prose."""
    status = str(data.get("escalation_status") or "").strip()
    if status not in _CLOSED_STATUSES:
        status = STATUS_REQUESTED
    lines = [
        "[STAFF_ESCALATION_EXECUTION_FACTS]",
        f"requested={_flag(bool(data.get('escalation_requested')))}",
        f"status={status}",
        f"session_created={_flag(bool(data.get('handoff_session_created')))}",
        f"session_reused={_flag(bool(data.get('handoff_session_reused')))}",
        f"notification_attempted={_flag(bool(data.get('notification_attempted')))}",
        f"notification_accepted={_flag(bool(data.get('notification_accepted')))}",
        f"notification_status={_notify_status(data)}",
        f"verified_contact_available={_flag(bool(data.get('verified_contact_available')))}",
    ]
    session_id = data.get("handoff_session_id")
    if session_id not in (None, ""):
        lines.append(f"handoff_session_id={session_id}")
    if data.get("verified_contact_available") is True:
        phone = str(data.get("verified_contact_phone") or "").strip()
        if phone:
            lines.append(f"verified_contact_phone={phone}")
    notify_failure = str(data.get("notification_failure_code") or "").strip()
    if notify_failure:
        lines.append(f"notification_failure_code={notify_failure}")
    if data.get("reused_previous_notification") is True:
        lines.append("reused_previous_notification=true")
    failure_code = str(data.get("failure_code") or "").strip()
    if failure_code:
        lines.append(f"failure_code={failure_code}")
    if data.get("after_hours") is True:
        lines.append("after_hours=true")
    return "\n".join(lines)


def _notify_status(data: Dict[str, Any]) -> str:
    status = str(data.get("notification_status") or "").strip()
    if status in _CLOSED_NOTIFY_STATUSES:
        return status
    if data.get("notification_accepted") is True:
        return NOTIFY_ACCEPTED
    if data.get("notification_attempted") is True:
        return NOTIFY_FAILED
    return NOTIFY_NOT_ATTEMPTED


def _trusted_customer_name(ctx: BrainContext) -> str:
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        for key in ("name", "customer_name", "display_name"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
    return _NEUTRAL_CUSTOMER_NAME


def _trusted_verified_contact(ctx: BrainContext) -> Tuple[bool, str]:
    """D2 has no trusted runtime producer of tenant-scoped staff contact.

    Inbound metadata, untyped profile values, and speculative objects
    with ``.phone``/``.source`` are not Merchant Truth. D3 owns
    manager/owner contact resolution.
    """
    del ctx
    return False, ""


def _result_payload(
    *,
    success: bool,
    status: str,
    failure_code: str = "",
    session_id: Any = None,
    session_created: bool = False,
    session_reused: bool = False,
    notification_attempted: bool = False,
    notification_accepted: bool = False,
    notification_status: str = NOTIFY_NOT_ATTEMPTED,
    notification_failure_code: str = "",
    reused_previous_notification: bool = False,
    verified_contact_available: bool = False,
    verified_contact_phone: str = "",
    after_hours: bool = False,
    error: Optional[str] = None,
) -> ActionResult:
    if status == STATUS_NOTIFIED and notification_accepted is not True:
        status = STATUS_QUEUED if session_id not in (None, "") else STATUS_REQUESTED
    if status == STATUS_ASSIGNED:
        # D2 has no assignment owner. Never invent assigned.
        status = STATUS_QUEUED if session_id not in (None, "") else STATUS_REQUESTED
    notify_status = str(notification_status or "").strip()
    if notify_status not in _CLOSED_NOTIFY_STATUSES:
        notify_status = NOTIFY_NOT_ATTEMPTED

    data: Dict[str, Any] = {
        "type": "handoff",
        "escalation_requested": True,
        "escalation_status": status,
        "handoff_session_id": session_id,
        "handoff_session_created": bool(session_created),
        "handoff_session_reused": bool(session_reused),
        "notification_attempted": bool(notification_attempted),
        "notification_accepted": bool(notification_accepted),
        "notification_sent": bool(notification_accepted),
        "notification_status": notify_status,
        "notification_failure_code": str(notification_failure_code or ""),
        "reused_previous_notification": bool(reused_previous_notification),
        "verified_contact_available": bool(verified_contact_available),
        "verified_contact_phone": (
            verified_contact_phone if verified_contact_available else ""
        ),
        "failure_code": failure_code,
        "after_hours": bool(after_hours),
        "ai_paused": False,
    }
    data["compose_facts_overlay"] = format_staff_escalation_facts_overlay(data)
    return ActionResult(success=success, data=data, error=error)


def _load_tenant_handoff_settings(db: Any, tenant_id: int) -> Dict[str, Any]:
    from core.tenant import get_or_create_settings, merge_defaults  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    meta = getattr(settings, "extra_metadata", None) or {}
    stored = meta.get("handoff_settings") if isinstance(meta, dict) else {}
    if not isinstance(stored, dict):
        stored = {}
    return merge_defaults(stored, _DEFAULT_HANDOFF_SETTINGS)


def _real_webhook_attempt_configured(settings: Dict[str, Any]) -> bool:
    method = str(settings.get("notification_method") or "").strip().lower()
    webhook_url = str(settings.get("webhook_url") or "").strip()
    return method in ("webhook", "both") and bool(webhook_url)


async def _attempt_notification(
    *,
    db: Any,
    tenant_id: int,
    session_id: int,
    customer_phone: str,
    customer_name: str,
    last_message: str,
) -> NotificationOutcome:
    """Real webhook POST only. WhatsApp TODO is not a provider attempt."""
    try:
        settings = _load_tenant_handoff_settings(db, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION] settings load failed tenant=%s err=%s",
            tenant_id,
            type(exc).__name__,
        )
        return NotificationOutcome(
            attempted=False,
            accepted=False,
            status=NOTIFY_UNAVAILABLE,
            failure_code="settings_unavailable",
        )

    method = str(settings.get("notification_method") or "").strip().lower()
    if method in ("", "none"):
        return NotificationOutcome(
            attempted=False,
            accepted=False,
            status=NOTIFY_NOT_ATTEMPTED,
        )
    if not _real_webhook_attempt_configured(settings):
        return NotificationOutcome(
            attempted=False,
            accepted=False,
            status=NOTIFY_UNAVAILABLE,
        )

    from handoff.notifier import notify_handoff  # noqa: PLC0415

    try:
        accepted = bool(
            await notify_handoff(
                session_id=int(session_id),
                tenant_id=int(tenant_id),
                customer_phone=customer_phone,
                customer_name=customer_name,
                last_message=last_message,
                handoff_settings=settings,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION] notify failed tenant=%s session=%s err=%s",
            tenant_id,
            session_id,
            type(exc).__name__,
        )
        return NotificationOutcome(
            attempted=True,
            accepted=False,
            status=NOTIFY_FAILED,
            failure_code="provider_exception",
        )
    if accepted:
        return NotificationOutcome(
            attempted=True,
            accepted=True,
            status=NOTIFY_ACCEPTED,
        )
    return NotificationOutcome(
        attempted=True,
        accepted=False,
        status=NOTIFY_FAILED,
        failure_code="provider_rejected",
    )


def _mark_session_notified(session: Any, accepted: bool) -> None:
    if accepted is not True:
        return
    if hasattr(session, "notification_sent"):
        session.notification_sent = True


def _prior_notification_sent(session: Any) -> bool:
    return getattr(session, "notification_sent", False) is True


async def execute_staff_escalation(
    decision: Decision,
    ctx: BrainContext,
) -> ActionResult:
    """Create or reuse a durable HandoffSession and return structured status."""
    after_hours = bool((decision.args or {}).get("after_hours"))
    verified_ok, verified_phone = _trusted_verified_contact(ctx)

    db = getattr(ctx, "_db", None) or getattr(ctx, "db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    customer_phone = str(getattr(ctx, "customer_phone", None) or "").strip()

    try:
        tenant_ok = int(tenant_id) > 0
    except (TypeError, ValueError):
        tenant_ok = False

    if db is None or not tenant_ok or not customer_phone:
        return _result_payload(
            success=False,
            status=STATUS_UNAVAILABLE,
            failure_code="execution_capability_unavailable",
            notification_status=NOTIFY_NOT_ATTEMPTED,
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="execution_capability_unavailable",
        )

    from handoff.manager import create_handoff_session, get_active_handoff  # noqa: PLC0415

    customer_name = _trusted_customer_name(ctx)
    last_message = str(getattr(ctx, "message", "") or "")
    existing = None
    try:
        existing = get_active_handoff(db, int(tenant_id), customer_phone)
        session = create_handoff_session(
            db,
            int(tenant_id),
            customer_phone,
            customer_name,
            last_message,
            reason="customer_request",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION] persistence failed tenant=%s err=%s",
            tenant_id,
            type(exc).__name__,
        )
        return _result_payload(
            success=False,
            status=STATUS_FAILED,
            failure_code="persistence_failed",
            notification_status=NOTIFY_NOT_ATTEMPTED,
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="persistence_failed",
        )

    if session is None or getattr(session, "id", None) in (None, ""):
        return _result_payload(
            success=False,
            status=STATUS_FAILED,
            failure_code="persistence_failed",
            notification_status=NOTIFY_NOT_ATTEMPTED,
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="persistence_failed",
        )

    session_id = session.id
    reused = existing is not None and getattr(existing, "id", None) == session_id
    created = not reused
    ctx.handoff_session_id = session_id  # type: ignore[attr-defined]

    notify = NotificationOutcome(
        attempted=False,
        accepted=False,
        status=NOTIFY_NOT_ATTEMPTED,
    )
    if reused and _prior_notification_sent(session):
        # Durable prior provider acceptance. Do not resend.
        notify = NotificationOutcome(
            attempted=False,
            accepted=True,
            status=NOTIFY_ACCEPTED,
            reused_previous=True,
        )
    elif reused:
        # Prior queue exists without accepted notify. D2 does not retry
        # on duplicate customer requests (avoids notification spam).
        notify = NotificationOutcome(
            attempted=False,
            accepted=False,
            status=NOTIFY_NOT_ATTEMPTED,
        )
    else:
        try:
            notify = await _attempt_notification(
                db=db,
                tenant_id=int(tenant_id),
                session_id=int(session_id),
                customer_phone=customer_phone,
                customer_name=customer_name,
                last_message=last_message,
            )
            if notify.accepted:
                _mark_session_notified(session, True)
                try:
                    db.flush()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[STAFF_ESCALATION] notify flag flush failed session=%s",
                        session_id,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_ESCALATION] notify wrapper failed session=%s err=%s",
                session_id,
                type(exc).__name__,
            )
            notify = NotificationOutcome(
                attempted=True,
                accepted=False,
                status=NOTIFY_FAILED,
                failure_code="provider_exception",
            )

    status = STATUS_NOTIFIED if notify.accepted else STATUS_QUEUED
    return _result_payload(
        success=True,
        status=status,
        session_id=session_id,
        session_created=created,
        session_reused=reused,
        notification_attempted=notify.attempted,
        notification_accepted=notify.accepted,
        notification_status=notify.status,
        notification_failure_code=notify.failure_code,
        reused_previous_notification=notify.reused_previous,
        verified_contact_available=verified_ok,
        verified_contact_phone=verified_phone,
        after_hours=after_hours,
    )


def should_defer_generic_prebrain_execution(
    *,
    is_handoff: bool,
    is_owner_contact: bool,
    is_post_pay_mod: bool,
    tier: str,
) -> bool:
    """True when generic staff-request must continue to Brain/D2.

    Owner/admin and post-payment PRE-BRAIN execution stay unchanged (D3).
    """
    if not is_handoff:
        return False
    if is_owner_contact or is_post_pay_mod:
        return False
    from core.handoff_detector import GENERIC_HANDOFF_TIER  # noqa: PLC0415

    return str(tier or "").strip() in {"", GENERIC_HANDOFF_TIER}


def action_handoff_already_executed(
    *,
    brain_handoff: bool,
    decision_action: str,
) -> bool:
    from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: PLC0415

    return bool(brain_handoff) or str(decision_action or "") == ACTION_HANDOFF


def build_staff_escalation_context(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str,
    conversation_id: Optional[int] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> BrainContext:
    from modules.ai.brain.types import (  # noqa: PLC0415
        CommerceFacts,
        Intent,
        MerchantConversationState,
    )

    ctx = BrainContext(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        message=str(message or ""),
        intent=Intent(
            name="talk_to_human",
            confidence=0.95,
            raw_message=str(message or ""),
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        profile=dict(profile or {}),
        conversation_id=conversation_id,
    )
    ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _mark_queue_visible(convo: Any, db: Any) -> None:
    if convo is None:
        return
    try:
        convo.needs_human = True
        db.flush()
    except Exception:  # noqa: BLE001
        logger.warning("[STAFF_ESCALATION] queue flag flush failed")


async def execute_staff_escalation_for_safety_signal(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str,
    convo: Any = None,
    profile: Optional[Dict[str, Any]] = None,
) -> ActionResult:
    """Same D2 core as ``_HandoffHandler``. Safety fallback only."""
    from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: PLC0415

    decision = Decision(
        action=ACTION_HANDOFF,
        args={},
        reason="customer_request",
    )
    ctx = build_staff_escalation_context(
        db=db,
        tenant_id=int(tenant_id),
        customer_phone=customer_phone,
        message=message,
        conversation_id=getattr(convo, "id", None),
        profile=profile,
    )
    result = await execute_staff_escalation(decision, ctx)
    if result.success:
        _mark_queue_visible(convo, db)
    return result


async def compose_staff_escalation_with_verifier(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str,
    result: ActionResult,
    conversation_id: Optional[int] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Canonical ACTION_HANDOFF compose + #920 semantic verifier."""
    from core.fallback_policy import empty_reply_fallback  # noqa: PLC0415
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
    from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: PLC0415

    decision = Decision(
        action=ACTION_HANDOFF,
        args={},
        reason="customer_request",
    )
    ctx = build_staff_escalation_context(
        db=db,
        tenant_id=int(tenant_id),
        customer_phone=customer_phone,
        message=message,
        conversation_id=conversation_id,
        profile=profile,
    )
    try:
        text = await DefaultComposer().compose(decision, result, ctx)
        return str(text or "").strip() or empty_reply_fallback()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION] safety compose failed tenant=%s",
            tenant_id,
        )
        return empty_reply_fallback()
