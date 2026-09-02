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
    "auto_pause_ai": True,
}

_NEUTRAL_CUSTOMER_NAME = ""


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
        f"verified_contact_available={_flag(bool(data.get('verified_contact_available')))}",
    ]
    session_id = data.get("handoff_session_id")
    if session_id not in (None, ""):
        lines.append(f"handoff_session_id={session_id}")
    if data.get("verified_contact_available") is True:
        phone = str(data.get("verified_contact_phone") or "").strip()
        if phone:
            lines.append(f"verified_contact_phone={phone}")
    failure_code = str(data.get("failure_code") or "").strip()
    if failure_code:
        lines.append(f"failure_code={failure_code}")
    if data.get("after_hours") is True:
        lines.append("after_hours=true")
    return "\n".join(lines)


def _trusted_customer_name(ctx: BrainContext) -> str:
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        for key in ("name", "customer_name", "display_name"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
    return _NEUTRAL_CUSTOMER_NAME


def _trusted_verified_contact(ctx: BrainContext) -> Tuple[bool, str]:
    """Propagate an already-supplied verified contact. Never invent."""
    candidates: list[Any] = [
        getattr(ctx, "verified_staff_contact_phone", None),
        getattr(ctx, "verified_contact_phone", None),
    ]
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        candidates.append(profile.get("verified_staff_contact_phone"))
        candidates.append(profile.get("verified_contact_phone"))
        inbound = profile.get("inbound_metadata")
        if isinstance(inbound, dict):
            candidates.append(inbound.get("verified_staff_contact_phone"))
            candidates.append(inbound.get("verified_contact_phone"))
    projection = getattr(ctx, "trusted_context_projection", None)
    if isinstance(projection, dict):
        candidates.append(projection.get("verified_staff_contact_phone"))
        candidates.append(projection.get("verified_contact_phone"))

    for raw in candidates:
        phone = str(raw or "").strip()
        if phone:
            return True, phone
    return False, ""


def _stamp_profile_execution_facts(ctx: BrainContext, payload: Dict[str, Any]) -> None:
    """In-place stamp so existing pipeline metadata reads see execution truth."""
    profile = getattr(ctx, "profile", None)
    if not isinstance(profile, dict):
        ctx.profile = {"inbound_metadata": dict(payload)}
        return
    meta = profile.get("inbound_metadata")
    if not isinstance(meta, dict):
        profile["inbound_metadata"] = dict(payload)
        return
    meta.update(payload)


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
) -> Tuple[bool, bool]:
    """Return (attempted, accepted). WhatsApp TODO is not a real attempt."""
    try:
        settings = _load_tenant_handoff_settings(db, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION] settings load failed tenant=%s err=%s",
            tenant_id,
            type(exc).__name__,
        )
        return False, False

    if not _real_webhook_attempt_configured(settings):
        return False, False

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
        return True, False
    return True, accepted


def _mark_session_notified(session: Any, accepted: bool) -> None:
    if accepted is not True:
        return
    if hasattr(session, "notification_sent"):
        session.notification_sent = True


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
        result = _result_payload(
            success=False,
            status=STATUS_UNAVAILABLE,
            failure_code="execution_capability_unavailable",
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="execution_capability_unavailable",
        )
        _stamp_profile_execution_facts(ctx, dict(result.data))
        return result

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
        result = _result_payload(
            success=False,
            status=STATUS_FAILED,
            failure_code="persistence_failed",
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="persistence_failed",
        )
        _stamp_profile_execution_facts(ctx, dict(result.data))
        return result

    if session is None or getattr(session, "id", None) in (None, ""):
        result = _result_payload(
            success=False,
            status=STATUS_FAILED,
            failure_code="persistence_failed",
            verified_contact_available=verified_ok,
            verified_contact_phone=verified_phone,
            after_hours=after_hours,
            error="persistence_failed",
        )
        _stamp_profile_execution_facts(ctx, dict(result.data))
        return result

    session_id = session.id
    reused = existing is not None and getattr(existing, "id", None) == session_id
    created = not reused
    ctx.handoff_session_id = session_id  # type: ignore[attr-defined]

    notification_attempted = False
    notification_accepted = False
    try:
        notification_attempted, notification_accepted = await _attempt_notification(
            db=db,
            tenant_id=int(tenant_id),
            session_id=int(session_id),
            customer_phone=customer_phone,
            customer_name=customer_name,
            last_message=last_message,
        )
        if notification_accepted:
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
        notification_attempted = True
        notification_accepted = False

    status = STATUS_NOTIFIED if notification_accepted else STATUS_QUEUED
    result = _result_payload(
        success=True,
        status=status,
        session_id=session_id,
        session_created=created,
        session_reused=reused,
        notification_attempted=notification_attempted,
        notification_accepted=notification_accepted,
        verified_contact_available=verified_ok,
        verified_contact_phone=verified_phone,
        after_hours=after_hours,
    )
    _stamp_profile_execution_facts(ctx, dict(result.data))
    return result
