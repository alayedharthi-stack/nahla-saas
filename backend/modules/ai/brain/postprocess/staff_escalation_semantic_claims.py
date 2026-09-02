"""Independent staff-escalation operational claim capabilities.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Queue != notified. Notified != assigned. Notified != future follow-up.
Phrase detectors are not the allow-gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional

from modules.ai.brain.decision.actions import ACTION_HANDOFF
from modules.ai.brain.types import ActionResult, BrainContext, Decision

logger = logging.getLogger("nahla.brain.staff_escalation_semantic_claims")

CLAIM_REQUEST_ACKNOWLEDGED = "request_acknowledged"
CLAIM_QUEUED = "queued"
CLAIM_STAFF_ASSIGNED = "staff_assigned"
CLAIM_STAFF_NOTIFIED = "staff_notified"
CLAIM_FUTURE_FOLLOWUP = "future_followup"
CLAIM_CONTACT_DELIVERED = "contact_delivered"

_CLAIM_KEYS = (
    CLAIM_REQUEST_ACKNOWLEDGED,
    CLAIM_QUEUED,
    CLAIM_STAFF_ASSIGNED,
    CLAIM_STAFF_NOTIFIED,
    CLAIM_FUTURE_FOLLOWUP,
    CLAIM_CONTACT_DELIVERED,
)

_CLAIM_ATTR = {
    CLAIM_REQUEST_ACKNOWLEDGED: "claims_request_acknowledged",
    CLAIM_QUEUED: "claims_queued",
    CLAIM_STAFF_ASSIGNED: "claims_staff_assigned",
    CLAIM_STAFF_NOTIFIED: "claims_staff_notified",
    CLAIM_FUTURE_FOLLOWUP: "claims_future_followup",
    CLAIM_CONTACT_DELIVERED: "claims_contact_delivered",
}

_CAP_ATTR = {
    CLAIM_REQUEST_ACKNOWLEDGED: "request_acknowledged",
    CLAIM_QUEUED: "queued",
    CLAIM_STAFF_ASSIGNED: "staff_assigned",
    CLAIM_STAFF_NOTIFIED: "staff_notified",
    CLAIM_FUTURE_FOLLOWUP: "future_followup_committed",
    CLAIM_CONTACT_DELIVERED: "contact_delivered",
}

DECISION_ALLOWED = "allowed"
DECISION_RECOMPOSE = "recompose"
DECISION_FAIL_CLOSED = "required_fail_closed"

RECOMPOSE_MAX_ATTEMPTS = 1


@dataclass(frozen=True)
class StaffEscalationTruthCapabilities:
    request_acknowledged: bool = False
    queued: bool = False
    staff_assigned: bool = False
    staff_notified: bool = False
    future_followup_committed: bool = False
    contact_delivered: bool = False

    def authorized_claims(self) -> FrozenSet[str]:
        return frozenset(
            key for key, attr in _CAP_ATTR.items() if bool(getattr(self, attr))
        )

    def as_dict(self) -> Dict[str, bool]:
        return {attr: bool(getattr(self, attr)) for attr in _CAP_ATTR.values()}


@dataclass(frozen=True)
class StaffEscalationCandidateClaims:
    claims_request_acknowledged: bool = False
    claims_queued: bool = False
    claims_staff_assigned: bool = False
    claims_staff_notified: bool = False
    claims_future_followup: bool = False
    claims_contact_delivered: bool = False
    valid_parse: bool = False
    confidence: float = 0.0
    provenance: str = ""
    model: str = ""

    def asserted_claims(self) -> FrozenSet[str]:
        if not self.valid_parse:
            return frozenset()
        return frozenset(
            key for key, attr in _CLAIM_ATTR.items() if bool(getattr(self, attr))
        )

    def as_dict(self) -> Dict[str, Any]:
        payload = {attr: bool(getattr(self, attr)) for attr in _CLAIM_ATTR.values()}
        payload["valid_parse"] = bool(self.valid_parse)
        payload["confidence"] = float(self.confidence)
        payload["provenance"] = str(self.provenance or "")
        payload["model"] = str(self.model or "")
        return payload


def capabilities_from_execution_data(data: Optional[Dict[str, Any]]) -> StaffEscalationTruthCapabilities:
    """Derive independent execution capabilities. Action names have zero authority."""
    md = data if isinstance(data, dict) else {}
    session_id = str(md.get("handoff_session_id") or "").strip()
    queued = bool(session_id)
    notified = (
        md.get("notification_accepted") is True
        or md.get("notification_sent") is True
    )
    assigned = md.get("staff_assigned") is True
    followup = md.get("future_followup_committed") is True
    # Availability of a verified contact is not delivery evidence.
    contact = (
        md.get("contact_delivered") is True
        or md.get("verified_contact_delivered") is True
    )
    acknowledged = bool(md.get("escalation_requested")) or queued or notified
    return StaffEscalationTruthCapabilities(
        request_acknowledged=bool(acknowledged),
        queued=bool(queued),
        staff_assigned=bool(assigned),
        staff_notified=bool(notified),
        future_followup_committed=bool(followup),
        contact_delivered=bool(contact),
    )


def unsupported_claims(
    claims: StaffEscalationCandidateClaims,
    capabilities: StaffEscalationTruthCapabilities,
) -> FrozenSet[str]:
    if not claims.valid_parse:
        return frozenset({"unparsed_claims"})
    return frozenset(claims.asserted_claims() - capabilities.authorized_claims())


def format_allowed_facts_overlay(capabilities: StaffEscalationTruthCapabilities) -> str:
    lines = ["[STAFF_ESCALATION_ALLOWED_FACTS]"]
    for key, attr in _CAP_ATTR.items():
        value = "true" if bool(getattr(capabilities, attr)) else "false"
        lines.append(f"{key}={value}")
    return "\n".join(lines)


def format_previous_validation_overlay(unsupported: FrozenSet[str]) -> str:
    ordered = ",".join(key for key in _CLAIM_KEYS if key in unsupported)
    return "[PREVIOUS_CANDIDATE_VALIDATION]\nunsupported_claims=" + (ordered or "none")


def _conversation_id(ctx: BrainContext) -> Any:
    for key in ("conversation_id", "convo_id"):
        value = getattr(ctx, key, None)
        if value not in (None, ""):
            return value
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        for key in ("conversation_id", "convo_id"):
            value = profile.get(key)
            if value not in (None, ""):
                return value
    return None


def _log_verify(
    *,
    tenant_id: Any,
    conversation_id: Any,
    capabilities: StaffEscalationTruthCapabilities,
    claims: Optional[StaffEscalationCandidateClaims],
    unsupported: FrozenSet[str],
    verifier_status: str,
    candidate_attempt: int,
    decision: str,
) -> None:
    logger.info(
        "[STAFF_ESCALATION_SEMANTIC_VERIFY] tenant_id=%s conversation_id=%s "
        "execution_truth_capabilities=%s candidate_claims=%s unsupported_claims=%s "
        "verifier_status=%s candidate_attempt=%s decision=%s",
        tenant_id,
        conversation_id,
        capabilities.as_dict(),
        claims.as_dict() if claims is not None else {},
        sorted(unsupported),
        verifier_status,
        candidate_attempt,
        decision,
    )


def _stamp(
    result: ActionResult,
    *,
    capabilities: StaffEscalationTruthCapabilities,
    claims: Optional[StaffEscalationCandidateClaims],
    unsupported: FrozenSet[str],
    verifier_status: str,
    candidate_attempt: int,
    decision: str,
) -> None:
    if not isinstance(result.data, dict):
        result.data = {}
    result.data["staff_escalation_semantic_verify"] = {
        "execution_truth_capabilities": capabilities.as_dict(),
        "candidate_claims": claims.as_dict() if claims is not None else {},
        "unsupported_claims": sorted(unsupported),
        "verifier_status": verifier_status,
        "candidate_attempt": int(candidate_attempt),
        "decision": decision,
    }
    result.data["staff_escalation_semantic_authority"] = True


def _fail_closed_reply() -> str:
    from core.fallback_policy import empty_reply_fallback  # noqa: PLC0415

    return str(empty_reply_fallback() or "").strip()


async def enforce_staff_escalation_semantic_truth(
    *,
    text: str,
    decision: Decision,
    result: ActionResult,
    ctx: BrainContext,
    compose_impl: Callable[[Decision, ActionResult, BrainContext], Awaitable[str]],
    classify_claims: Optional[
        Callable[[str], Awaitable[StaffEscalationCandidateClaims]]
    ] = None,
) -> str:
    """Primary claim authority for ACTION_HANDOFF customer-facing candidates."""
    from modules.ai.brain.postprocess.staff_escalation_semantic_verifier import (  # noqa: PLC0415
        classify_staff_escalation_claims,
    )

    if not isinstance(result.data, dict):
        result.data = {}
    data = result.data
    capabilities = capabilities_from_execution_data(data)
    tenant_id = getattr(ctx, "tenant_id", None)
    conversation_id = _conversation_id(ctx)
    candidate = str(text or "").strip()
    if not candidate:
        _stamp(
            result,
            capabilities=capabilities,
            claims=None,
            unsupported=frozenset({"empty_candidate"}),
            verifier_status="empty_candidate",
            candidate_attempt=1,
            decision=DECISION_FAIL_CLOSED,
        )
        _log_verify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            capabilities=capabilities,
            claims=None,
            unsupported=frozenset({"empty_candidate"}),
            verifier_status="empty_candidate",
            candidate_attempt=1,
            decision=DECISION_FAIL_CLOSED,
        )
        data["fallback_reason"] = "staff_escalation_semantic_empty_candidate"
        data["fallback_action_type"] = "staff_escalation_semantic_truth"
        data["compose_source"] = "fallback_deterministic"
        return _fail_closed_reply()

    async def _verify(candidate_text: str, attempt: int) -> tuple[StaffEscalationCandidateClaims, FrozenSet[str], str]:
        try:
            if classify_claims is None:
                claims = await classify_staff_escalation_claims(
                    candidate_text,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                )
            else:
                claims = await classify_claims(candidate_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_ESCALATION_SEMANTIC_VERIFY] classifier_exception tenant_id=%s err=%s",
                tenant_id,
                type(exc).__name__,
            )
            failed = StaffEscalationCandidateClaims(
                valid_parse=False,
                provenance="classifier_exception",
            )
            return failed, frozenset({"verifier_exception"}), "exception"
        if not claims.valid_parse:
            return claims, frozenset({"invalid_verifier_output"}), str(claims.provenance or "invalid")
        return claims, unsupported_claims(claims, capabilities), str(claims.provenance or "ok")

    first_claims, unsupported, status = await _verify(candidate, 1)
    if not unsupported:
        _stamp(
            result,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=frozenset(),
            verifier_status=status,
            candidate_attempt=1,
            decision=DECISION_ALLOWED,
        )
        _log_verify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=frozenset(),
            verifier_status=status,
            candidate_attempt=1,
            decision=DECISION_ALLOWED,
        )
        return candidate

    if status in {"exception", "invalid", "invalid_verifier_output", "timeout", "unavailable"} or not first_claims.valid_parse:
        _stamp(
            result,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=unsupported,
            verifier_status=status,
            candidate_attempt=1,
            decision=DECISION_FAIL_CLOSED,
        )
        _log_verify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=unsupported,
            verifier_status=status,
            candidate_attempt=1,
            decision=DECISION_FAIL_CLOSED,
        )
        data["fallback_reason"] = "staff_escalation_semantic_verifier_failed"
        data["fallback_action_type"] = "staff_escalation_semantic_truth"
        data["compose_source"] = "fallback_deterministic"
        return _fail_closed_reply()

    _log_verify(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        capabilities=capabilities,
        claims=first_claims,
        unsupported=unsupported,
        verifier_status=status,
        candidate_attempt=1,
        decision=DECISION_RECOMPOSE,
    )
    existing_overlay = str(data.get("compose_facts_overlay") or "").strip()
    correction = (
        format_allowed_facts_overlay(capabilities)
        + "\n"
        + format_previous_validation_overlay(unsupported)
    )
    data["compose_facts_overlay"] = (
        f"{existing_overlay}\n\n{correction}" if existing_overlay else correction
    )
    data["staff_escalation_semantic_recompose"] = True
    if str(getattr(decision, "action", "") or "") != ACTION_HANDOFF:
        decision.action = ACTION_HANDOFF
    second = ""
    try:
        second = str(await compose_impl(decision, result, ctx) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION_SEMANTIC_VERIFY] recompose_exception tenant_id=%s err=%s",
            tenant_id,
            type(exc).__name__,
        )
        _stamp(
            result,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=unsupported,
            verifier_status="recompose_exception",
            candidate_attempt=2,
            decision=DECISION_FAIL_CLOSED,
        )
        data["fallback_reason"] = "staff_escalation_semantic_recompose_failed"
        data["fallback_action_type"] = "staff_escalation_semantic_truth"
        data["compose_source"] = "fallback_deterministic"
        return _fail_closed_reply()

    if not second:
        _stamp(
            result,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=unsupported,
            verifier_status="empty_recompose",
            candidate_attempt=2,
            decision=DECISION_FAIL_CLOSED,
        )
        _log_verify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            capabilities=capabilities,
            claims=first_claims,
            unsupported=unsupported,
            verifier_status="empty_recompose",
            candidate_attempt=2,
            decision=DECISION_FAIL_CLOSED,
        )
        data["fallback_reason"] = "staff_escalation_semantic_recompose_empty"
        data["fallback_action_type"] = "staff_escalation_semantic_truth"
        data["compose_source"] = "fallback_deterministic"
        return _fail_closed_reply()

    second_claims, second_unsupported, second_status = await _verify(second, 2)
    if second_claims.valid_parse and not second_unsupported:
        _stamp(
            result,
            capabilities=capabilities,
            claims=second_claims,
            unsupported=frozenset(),
            verifier_status=second_status,
            candidate_attempt=2,
            decision=DECISION_ALLOWED,
        )
        _log_verify(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            capabilities=capabilities,
            claims=second_claims,
            unsupported=frozenset(),
            verifier_status=second_status,
            candidate_attempt=2,
            decision=DECISION_ALLOWED,
        )
        return second

    _stamp(
        result,
        capabilities=capabilities,
        claims=second_claims,
        unsupported=second_unsupported,
        verifier_status=second_status,
        candidate_attempt=2,
        decision=DECISION_FAIL_CLOSED,
    )
    _log_verify(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        capabilities=capabilities,
        claims=second_claims,
        unsupported=second_unsupported,
        verifier_status=second_status,
        candidate_attempt=2,
        decision=DECISION_FAIL_CLOSED,
    )
    data["fallback_reason"] = "staff_escalation_semantic_second_overclaim"
    data["fallback_action_type"] = "staff_escalation_semantic_truth"
    data["compose_source"] = "fallback_deterministic"
    return _fail_closed_reply()


async def maybe_enforce_staff_escalation_semantic_truth(
    *,
    original_action: str,
    text: str,
    decision: Decision,
    result: ActionResult,
    ctx: BrainContext,
    compose_impl: Callable[[Decision, ActionResult, BrainContext], Awaitable[str]],
) -> str:
    if str(original_action or "") != ACTION_HANDOFF:
        return text
    return await enforce_staff_escalation_semantic_truth(
        text=text,
        decision=decision,
        result=result,
        ctx=ctx,
        compose_impl=compose_impl,
    )
