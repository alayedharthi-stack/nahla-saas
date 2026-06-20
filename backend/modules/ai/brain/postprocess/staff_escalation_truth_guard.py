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

# Deprecated generic stub — guards must use conversation_recovery instead.
# Kept for test assertions that verify we never emit this string.
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

        try:
            from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                is_generic_stub_reply,
                is_staff_route_rejection_message,
                resolve_staff_rejection_commerce_resume,
            )

            if is_staff_route_rejection_message(inbound_text):
                if is_generic_stub_reply(original) or reply_contains_escalation_claim(
                    original,
                ):
                    return StaffEscalationTruthGuardResult(
                        reply=resolve_staff_rejection_commerce_resume(state),
                        action="staff_route_rejected_resume",
                        replaced=True,
                        reason="staff_route_rejected_commerce_resume",
                    )
        except Exception as _rej_exc:  # noqa: BLE001  # noqa: silent-ok
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] staff_rejection resume failed err=%s",
                _rej_exc,
            )

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
                resolve_social_thanks_guard_reply,
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

            try:
                from modules.ai.brain.commerce.start_order_verb_guard import (  # noqa: PLC0415
                    is_bare_start_order_phrase,
                )
                from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
                    build_bare_start_order_guard_reply,
                )

                if is_bare_start_order_phrase(inbound_text):
                    return StaffEscalationTruthGuardResult(
                        reply=build_bare_start_order_guard_reply(inbound_text),
                        action="blocked_false_escalation_bare_start_order",
                        replaced=True,
                        reason="bare_start_order_guard_reply",
                        evidence=evidence,
                        staff_escalation_claim_blocked=True,
                    )
            except Exception as _bso_exc:  # noqa: BLE001  # noqa: silent-ok
                logger.debug(
                    "[STAFF_ESCALATION_TRUTH_GUARD] bare_start_order reply failed err=%s",
                    _bso_exc,
                )

            _active_commerce = has_active_commerce_from_state(state)
            _suppress_stub = should_suppress_generic_stub_injection(
                inbound_text=inbound_text,
                state=state,
            )
            if _suppress_stub and not _active_commerce:
                _social_reply = resolve_social_thanks_guard_reply(inbound_text)
                if _social_reply:
                    return StaffEscalationTruthGuardResult(
                        reply=_social_reply,
                        action="blocked_false_escalation_social_thanks",
                        replaced=True,
                        reason="social_thanks_mirror",
                        evidence=evidence,
                        staff_escalation_claim_blocked=True,
                    )
            if _suppress_stub or _active_commerce:
                try:
                    from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: PLC0415
                        message_has_bare_quantity_or_variant_signal,
                        resolve_active_order_quantity_reply,
                    )

                    if message_has_bare_quantity_or_variant_signal(inbound_text):
                        qty_reply = resolve_active_order_quantity_reply(
                            inbound_text,
                            state=state,
                            active_commerce=_active_commerce,
                        )
                        if qty_reply:
                            return StaffEscalationTruthGuardResult(
                                reply=qty_reply,
                                action="blocked_false_escalation_active_order_quantity",
                                replaced=True,
                                reason="active_order_quantity_input",
                                evidence=evidence,
                                staff_escalation_claim_blocked=True,
                            )
                except Exception as _qty_exc:  # noqa: BLE001  # noqa: silent-ok
                    logger.debug(
                        "[STAFF_ESCALATION_TRUTH_GUARD] qty reply failed err=%s",
                        _qty_exc,
                    )
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
        except Exception as _stub_ctx_exc:  # noqa: BLE001  # noqa: silent-ok — stub context enrich must not break guard
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] stub context failed err=%s",
                _stub_ctx_exc,
            )
        try:
            from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                resolve_social_thanks_guard_reply,
            )

            _social_fallback = resolve_social_thanks_guard_reply(inbound_text)
        except Exception as _soc_exc:  # noqa: BLE001
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] social thanks fallback failed err=%s",
                _soc_exc,
            )
            _social_fallback = None
        if _social_fallback:
            return StaffEscalationTruthGuardResult(
                reply=_social_fallback,
                action="blocked_false_escalation_social_thanks",
                replaced=True,
                reason="social_thanks_mirror_fallback",
                evidence=evidence,
                staff_escalation_claim_blocked=True,
            )
        try:
            from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                is_staff_route_rejection_message,
                resolve_staff_rejection_commerce_resume,
            )

            if is_staff_route_rejection_message(inbound_text):
                return StaffEscalationTruthGuardResult(
                    reply=resolve_staff_rejection_commerce_resume(state),
                    action="blocked_false_escalation_staff_rejected_resume",
                    replaced=True,
                    reason="staff_route_rejected_commerce_resume",
                    evidence=evidence,
                    staff_escalation_claim_blocked=True,
                )
        except Exception as _rej_fb_exc:  # noqa: BLE001  # noqa: silent-ok
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] staff_rejection fallback failed err=%s",
                _rej_fb_exc,
            )
        try:
            from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: PLC0415
                try_guard_recovery_reply,
            )

            recovery = try_guard_recovery_reply(
                inbound_text=inbound_text,
                state=state,
                history=history,
            )
            if recovery.reply and not recovery.needs_persona_compose:
                return StaffEscalationTruthGuardResult(
                    reply=recovery.reply,
                    action=f"blocked_false_escalation_{recovery.source}",
                    replaced=True,
                    reason=f"conversation_recovery:{recovery.source}",
                    evidence=evidence,
                    staff_escalation_claim_blocked=True,
                )
        except Exception as _rec_exc:  # noqa: BLE001  # noqa: silent-ok
            logger.debug(
                "[STAFF_ESCALATION_TRUTH_GUARD] conversation_recovery failed err=%s",
                _rec_exc,
            )
        return StaffEscalationTruthGuardResult(
            reply="",
            action="blocked_false_escalation_needs_recovery",
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
