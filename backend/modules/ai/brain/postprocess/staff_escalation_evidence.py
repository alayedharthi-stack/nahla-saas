"""
modules/ai/brain/postprocess/staff_escalation_evidence.py
──────────────────────────────────────────────────────────
Structured staff-escalation evidence only — never infer from LLM
wording, customer frustration alone, stale ``needs_human`` flags,
or decision/action names.
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


def _session_id_present(metadata: Dict[str, Any]) -> bool:
    return bool(str(metadata.get("handoff_session_id") or "").strip())


def _notification_accepted(metadata: Dict[str, Any]) -> bool:
    return (
        metadata.get("notification_accepted") is True
        or metadata.get("notification_sent") is True
    )


def _verified_contact_delivered(metadata: Dict[str, Any]) -> bool:
    if metadata.get("verified_contact_available") is not True:
        return False
    return bool(str(metadata.get("verified_contact_phone") or "").strip())


def _pre_brain_path(chosen_path: str, metadata: Dict[str, Any]) -> bool:
    path = str(chosen_path or "").strip()
    dp = str(metadata.get("deterministic_path") or "").strip()
    return path.startswith("pre_brain_handoff:") or dp.startswith("pre_brain_handoff:")


def _metadata_execution_evidence(
    metadata: Optional[Dict[str, Any]],
) -> tuple[bool, str, bool, bool]:
    md = metadata or {}
    session_present = _session_id_present(md) or md.get("handoff_session_created") is True
    notification_present = _notification_accepted(md)

    if _session_id_present(md):
        return True, "metadata.handoff_session_id", True, notification_present
    if md.get("handoff_session_created") is True:
        return True, "metadata.handoff_session_created", True, notification_present
    if notification_present:
        return True, "metadata.notification_accepted", session_present, True
    if _verified_contact_delivered(md):
        return True, "metadata.verified_contact", session_present, notification_present
    return False, "", session_present, notification_present


def _conversation_grants_evidence(
    conversation_flags: Optional[Dict[str, Any]],
) -> tuple[bool, str]:
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
    """Return whether trusted staff-escalation *execution* evidence exists.

    ``brain_handoff`` and action/chosen_path names have zero authority.
    They remain accepted kwargs for telemetry/routing callers only.
    """
    del brain_handoff  # decision-level flag is not operational evidence

    md = inbound_metadata or {}
    path = str(chosen_path or "").strip()
    if path in _ACTION_NAME_PATHS:
        path = ""

    meta_ok, meta_source, session_present, notification_present = (
        _metadata_execution_evidence(md)
    )
    if meta_ok:
        return StaffEscalationEvidenceResult(
            evidence_ok=True,
            evidence_source=meta_source,
            handoff_session_present=session_present,
            notification_present=notification_present,
            reason=meta_source,
        )

    if _pre_brain_path(chosen_path, md):
        # Legitimate pre-brain only with real session/contact execution.
        if session_present or _verified_contact_delivered(md):
            source = "pre_brain_handoff_execution"
            return StaffEscalationEvidenceResult(
                evidence_ok=True,
                evidence_source=source,
                handoff_session_present=session_present,
                notification_present=notification_present,
                reason=source,
            )

    conv_ok, conv_source = _conversation_grants_evidence(conversation_flags)
    if conv_ok:
        return StaffEscalationEvidenceResult(
            evidence_ok=True,
            evidence_source=conv_source,
            handoff_session_present=True,
            notification_present=False,
            reason=conv_source,
        )

    return StaffEscalationEvidenceResult(
        evidence_ok=False,
        evidence_source="none",
        handoff_session_present=False,
        notification_present=False,
        reason="no_structured_escalation_evidence",
    )
