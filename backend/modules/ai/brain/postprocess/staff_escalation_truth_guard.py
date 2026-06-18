"""
modules/ai/brain/postprocess/staff_escalation_truth_guard.py
────────────────────────────────────────────────────────────
Block false staff-escalation wording when operational escalation
evidence is missing. AI continuity is preserved — only the claim
is replaced with a neutral stub, never a new operational promise.
Persona compose may later consume ``staff_escalation_claim_blocked``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.outbound_sanitizer import contains_handoff_promise
from modules.ai.brain.postprocess.staff_escalation_evidence import (
    StaffEscalationEvidenceResult,
    evaluate_staff_escalation_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.staff_escalation_truth_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

# Neutral stub only — no promises, escalation, notification, or
# follow-up actions. Personality/warmth belongs to persona compose.
SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR = "تمام 🌷 وصلت رسالتك."

_ESCALATION_CLAIM_MARKERS = (
    "تم تحويلك",
    "تم تحويلك للدعم",
    "تم تحويلك لفريق الدعم",
    "تم إشعار الفريق",
    "تم رفع الطلب",
    "تم التصعيد",
    "سيتم تحويلك",
    "تم تحويل المحادثة",
)


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def reply_contains_escalation_claim(reply: Optional[str]) -> bool:
    if not reply or not isinstance(reply, str):
        return False
    if contains_handoff_promise(reply):
        return True
    norm = _norm(reply)
    if not norm:
        return False
    markers = (_norm(marker) for marker in _ESCALATION_CLAIM_MARKERS)
    return any(marker in norm for marker in markers)


@dataclass(frozen=True)
class StaffEscalationTruthGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    evidence: Optional[StaffEscalationEvidenceResult] = None
    staff_escalation_claim_blocked: bool = False


def guard_metadata_patch(
    result: StaffEscalationTruthGuardResult,
) -> Dict[str, Any]:
    """Metadata for downstream persona compose (future hook)."""
    if not result.staff_escalation_claim_blocked:
        return {}
    return {
        "staff_escalation_claim_blocked": True,
        "staff_escalation_guard_reason": result.reason or "",
    }


def log_staff_escalation_truth_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    action: str,
    reason: str,
    evidence_source: str,
    handoff_session_present: bool,
    notification_present: bool,
    staff_escalation_claim_blocked: bool = False,
) -> None:
    try:
        logger.info(
            "[STAFF_ESCALATION_TRUTH_GUARD] tenant_id=%s conversation_id=%s "
            "action=%s reason=%s evidence_source=%s "
            "handoff_session_present=%s notification_present=%s "
            "staff_escalation_claim_blocked=%s",
            tenant_id,
            conversation_id,
            action,
            reason or "-",
            evidence_source or "-",
            bool(handoff_session_present),
            bool(notification_present),
            bool(staff_escalation_claim_blocked),
        )
    except Exception:  # noqa: BLE001
        pass


def apply_staff_escalation_truth_guard(
    *,
    reply: str,
    inbound_text: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation_flags: Optional[Dict[str, Any]] = None,
    chosen_path: str = "",
    brain_handoff: bool = False,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    state: Any = None,
    history: Optional[list] = None,
) -> StaffEscalationTruthGuardResult:
    try:
        original = str(reply or "")
        if not original.strip():
            return StaffEscalationTruthGuardResult(reply=original, action="allowed")

        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata=inbound_metadata,
            conversation_flags=conversation_flags,
            chosen_path=chosen_path,
            brain_handoff=brain_handoff,
        )

        if not reply_contains_escalation_claim(original):
            log_staff_escalation_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason="no_escalation_claim_wording",
                evidence_source=evidence.evidence_source,
                handoff_session_present=evidence.handoff_session_present,
                notification_present=evidence.notification_present,
            )
            return StaffEscalationTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        if evidence.evidence_ok:
            log_staff_escalation_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason=evidence.reason,
                evidence_source=evidence.evidence_source,
                handoff_session_present=evidence.handoff_session_present,
                notification_present=evidence.notification_present,
            )
            return StaffEscalationTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        log_staff_escalation_truth_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="blocked_false_escalation",
            reason=evidence.reason,
            evidence_source=evidence.evidence_source,
            handoff_session_present=evidence.handoff_session_present,
            notification_present=evidence.notification_present,
            staff_escalation_claim_blocked=True,
        )
        try:
            from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
                is_order_tracking_follow_up,
                resolve_order_tracking_guard_reply,
            )

            if is_order_tracking_follow_up(
                inbound_text,
                state=state,
                history=history,
            ):
                tracking_reply = resolve_order_tracking_guard_reply(
                    state=state,
                    history=history,
                )
                return StaffEscalationTruthGuardResult(
                    reply=tracking_reply,
                    action="blocked_false_escalation_order_tracking",
                    replaced=True,
                    reason="order_tracking_guard_stub_replacement",
                    evidence=evidence,
                    staff_escalation_claim_blocked=True,
                )
        except Exception as _otg_exc:  # noqa: BLE001  # noqa: silent-ok — fallback to generic stub
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] order_tracking_guard failed err=%s",
                _otg_exc,
            )
        try:
            from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                has_active_commerce_from_state,
                should_suppress_generic_stub_injection,
                strip_escalation_claim_sentences,
            )
            from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
                build_short_honey_order_clarify_reply,
                is_short_honey_order_request,
            )

            if is_short_honey_order_request(inbound_text):
                return StaffEscalationTruthGuardResult(
                    reply=build_short_honey_order_clarify_reply(inbound_text),
                    action="blocked_false_escalation_order_clarify",
                    replaced=True,
                    reason="short_honey_order_clarify",
                    evidence=evidence,
                    staff_escalation_claim_blocked=True,
                )

            if should_suppress_generic_stub_injection(
                inbound_text=inbound_text,
                state=state,
            ) or has_active_commerce_from_state(state):
                scrubbed = strip_escalation_claim_sentences(original)
                if scrubbed and len(scrubbed.strip()) >= 6:
                    return StaffEscalationTruthGuardResult(
                        reply=scrubbed,
                        action="blocked_false_escalation_scrubbed",
                        replaced=True,
                        reason="escalation_claim_scrubbed_active_commerce",
                        evidence=evidence,
                        staff_escalation_claim_blocked=True,
                    )
                return StaffEscalationTruthGuardResult(
                    reply="تمام، أكمل معك الطلب — وش تحتاج؟",
                    action="blocked_false_escalation_order_continue",
                    replaced=True,
                    reason="active_commerce_continue",
                    evidence=evidence,
                    staff_escalation_claim_blocked=True,
                )
        except Exception as _stub_ctx_exc:  # noqa: BLE001
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] stub context failed err=%s",
                _stub_ctx_exc,
            )
        return StaffEscalationTruthGuardResult(
            reply=SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
            action="blocked_false_escalation",
            replaced=True,
            reason=evidence.reason,
            evidence=evidence,
            staff_escalation_claim_blocked=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[STAFF_ESCALATION_TRUTH_GUARD] guard failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return StaffEscalationTruthGuardResult(reply=str(reply or ""), action="allowed")
