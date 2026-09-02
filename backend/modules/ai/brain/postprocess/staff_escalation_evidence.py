"""
modules/ai/brain/postprocess/staff_escalation_evidence.py
──────────────────────────────────────────────────────────
Structured staff-escalation evidence only — never infer from LLM
wording, customer frustration alone, stale ``needs_human`` flags,
or decision/action names.

Queue evidence and notification evidence are separate claim strengths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

_ACTION_NAME_PATHS = frozenset({
    "ACTION_HANDOFF",
    "action_handoff",
    "handoff",
    "handoff_guard_ack_send",
    "customer_request_handoff",
})


@dataclass(frozen=True)
class StaffEscalationEvidenceResult:
    evidence_ok: bool
    evidence_source: str
    handoff_session_present: bool
    notification_present: bool
    reason: str
    queue_evidence_ok: bool = False
    notification_evidence_ok: bool = False
    contact_delivery_evidence_ok: bool = False


def _session_id_present(metadata: Dict[str, Any]) -> bool:
    return bool(str(metadata.get("handoff_session_id") or "").strip())


def _notification_accepted(metadata: Dict[str, Any]) -> bool:
    return (
        metadata.get("notification_accepted") is True
        or metadata.get("notification_sent") is True
        or str(metadata.get("notification_status") or "").strip() == "accepted"
    )


def _verified_contact_delivered(metadata: Dict[str, Any]) -> bool:
    if metadata.get("verified_contact_available") is not True:
        return False
    return bool(str(metadata.get("verified_contact_phone") or "").strip())


def _pre_brain_path(chosen_path: str, metadata: Dict[str, Any]) -> bool:
    path = str(chosen_path or "").strip()
    dp = str(metadata.get("deterministic_path") or "").strip()
    return path.startswith("pre_brain_handoff:") or dp.startswith("pre_brain_handoff:")


def _result(
    *,
    source: str,
    queue: bool = False,
    notification: bool = False,
    contact: bool = False,
    session_present: bool = False,
    reason: str = "",
) -> StaffEscalationEvidenceResult:
    return StaffEscalationEvidenceResult(
        evidence_ok=bool(queue or notification or contact),
        evidence_source=source,
        handoff_session_present=bool(session_present and queue),
        notification_present=bool(notification),
        reason=reason or source,
        queue_evidence_ok=bool(queue),
        notification_evidence_ok=bool(notification),
        contact_delivery_evidence_ok=bool(contact),
    )


def evaluate_staff_escalation_evidence(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation_flags: Optional[Dict[str, Any]] = None,
    chosen_path: str = "",
    brain_handoff: bool = False,
) -> StaffEscalationEvidenceResult:
    """Return structured execution evidence. Action names have zero authority.

    ``evidence_ok`` means *some* operational execution exists. It does not
    authorize every escalation claim. Callers must use the split flags:

    - queue_evidence_ok → durable HandoffSession / queue record
    - notification_evidence_ok → provider acceptance / notification_sent
    - contact_delivery_evidence_ok → verified delivered staff contact
    """
    del brain_handoff
    del conversation_flags  # lifecycle/UI only — never impersonates a session

    md = inbound_metadata or {}
    path = str(chosen_path or "").strip()
    if path in _ACTION_NAME_PATHS:
        path = ""

    session_present = _session_id_present(md)
    notification = _notification_accepted(md)
    contact = _verified_contact_delivered(md)

    if _session_id_present(md):
        return _result(
            source="metadata.handoff_session_id",
            queue=True,
            notification=notification,
            contact=contact,
            session_present=True,
        )
    if notification:
        return _result(
            source="metadata.notification_accepted",
            queue=False,
            notification=True,
            contact=contact,
            session_present=False,
        )
    if contact:
        return _result(
            source="metadata.verified_contact",
            queue=False,
            notification=False,
            contact=True,
            session_present=False,
        )

    if _pre_brain_path(chosen_path, md) and (session_present or contact):
        return _result(
            source="pre_brain_handoff_execution",
            queue=bool(session_present),
            notification=notification,
            contact=contact,
            session_present=bool(session_present),
        )

    return StaffEscalationEvidenceResult(
        evidence_ok=False,
        evidence_source="none",
        handoff_session_present=False,
        notification_present=False,
        reason="no_structured_escalation_evidence",
        queue_evidence_ok=False,
        notification_evidence_ok=False,
        contact_delivery_evidence_ok=False,
    )
