"""
modules/ai/brain/postprocess/staff_escalation_evidence.py
──────────────────────────────────────────────────────────
Structured staff-escalation evidence only — never infer from LLM
wording, customer frustration alone, or stale ``needs_human`` flags.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

_DETERMINISTIC_ESCALATION_PATHS = frozenset({
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


def _deterministic_path_grants_evidence(chosen_path: str) -> tuple[bool, str]:
    path = str(chosen_path or "").strip()
    if not path:
        return False, ""
    if path in _DETERMINISTIC_ESCALATION_PATHS:
        return True, f"deterministic_path={path}"
    if path.startswith("pre_brain_handoff:"):
        return True, f"deterministic_path={path}"
    return False, ""


def _metadata_grants_evidence(metadata: Optional[Dict[str, Any]]) -> tuple[bool, str, bool, bool]:
    md = metadata or {}
    session_present = bool(str(md.get("handoff_session_id") or "").strip())
    notification_present = md.get("notification_sent") is True

    if session_present:
        return True, "metadata.handoff_session_id", True, notification_present
    if notification_present:
        return True, "metadata.notification_sent", session_present, True
    if str(md.get("escalation_event") or "").strip() == "handoff_created":
        return True, "metadata.escalation_event", session_present, notification_present
    if str(md.get("event_type") or "").strip() == "ai_handoff_ack":
        return True, "metadata.ai_handoff_ack", session_present, notification_present

    dp = str(md.get("deterministic_path") or "").strip()
    if dp.startswith("pre_brain_handoff:"):
        if md.get("handoff_active") or md.get("handoff_session_created"):
            return True, f"metadata.{dp}", session_present, notification_present

    ok, source = _deterministic_path_grants_evidence(dp)
    if ok:
        return True, source, session_present, notification_present

    return False, "", session_present, notification_present


def _conversation_grants_evidence(
    conversation_flags: Optional[Dict[str, Any]],
    *,
    brain_handoff: bool = False,
) -> tuple[bool, str]:
    if brain_handoff:
        return True, "brain_handoff_session_created"

    flags = conversation_flags or {}
    needs_human = bool(flags.get("needs_human"))
    handoff_active = bool(flags.get("handoff_active"))
    is_human = bool(flags.get("is_human_handoff"))
    status_human = str(flags.get("status") or "").strip().lower() == "human"

    # Soft ``needs_human`` alone (VAGUE tier) is not operational evidence.
    if handoff_active and needs_human and (is_human or status_human):
        return True, "conversation_active_handoff"

    return False, ""


def evaluate_staff_escalation_evidence(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation_flags: Optional[Dict[str, Any]] = None,
    chosen_path: str = "",
    brain_handoff: bool = False,
) -> StaffEscalationEvidenceResult:
    """Return whether trusted staff-escalation evidence exists for this turn."""
    ok, source = _deterministic_path_grants_evidence(chosen_path)
    if ok:
        return StaffEscalationEvidenceResult(
            evidence_ok=True,
            evidence_source=source,
            handoff_session_present=brain_handoff,
            notification_present=False,
            reason=source,
        )

    conv_ok, conv_source = _conversation_grants_evidence(
        conversation_flags,
        brain_handoff=brain_handoff,
    )
    if conv_ok:
        return StaffEscalationEvidenceResult(
            evidence_ok=True,
            evidence_source=conv_source,
            handoff_session_present=brain_handoff or conv_source.startswith("conversation"),
            notification_present=False,
            reason=conv_source,
        )

    meta_ok, meta_source, session_present, notification_present = (
        _metadata_grants_evidence(inbound_metadata)
    )
    if meta_ok:
        return StaffEscalationEvidenceResult(
            evidence_ok=True,
            evidence_source=meta_source,
            handoff_session_present=session_present or brain_handoff,
            notification_present=notification_present,
            reason=meta_source,
        )

    return StaffEscalationEvidenceResult(
        evidence_ok=False,
        evidence_source="none",
        handoff_session_present=False,
        notification_present=False,
        reason="no_structured_escalation_evidence",
    )
