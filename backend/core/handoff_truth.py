"""
core/handoff_truth.py
─────────────────────
Unified handoff truth contract for wire-layer scrubbing and AI
suppression fail-closed policy.

Truth predicate (``resolve_handoff_truth_active``) aligns with
``staff_escalation_evidence.evaluate_staff_escalation_evidence``:

  * Active ``HandoffSession`` for tenant + phone.
  * Conversation flags: ``handoff_active`` AND ``needs_human`` AND
    (``is_human_handoff`` OR ``status == 'human'``) — soft
    ``needs_human`` alone is NOT sufficient.
  * ``conversation_handoff_active`` (human_active ownership).
  * Structured execution metadata (session id / notification accepted /
    verified delivered contact). Action names and chosen_path alone
    are not operational evidence.

Fail-closed scope is limited to the "may AI reply?" decision when gate
verification fails while possible human-ownership signals are present.
Transient DB errors with no handoff signals fail open (telemetry only).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.ownership_state import (
    conversation_handoff_active,
    has_advisory_queue_signals,
    has_implicit_takeover_signals,
    is_explicit_takeover,
)
from models import Conversation, HandoffSession

logger = logging.getLogger("nahla-backend")

REASON_GATE_VERIFY_FAILED = "gate_verify_failed_safe_block"


@dataclass(frozen=True)
class HandoffTruthResult:
    active: bool
    source: str = ""
    verify_failed: bool = False


def conversation_flags_dict(convo: Any) -> dict[str, Any]:
    if convo is None:
        return {}
    return {
        "needs_human": bool(getattr(convo, "needs_human", False)),
        "handoff_active": bool(getattr(convo, "handoff_active", False)),
        "is_human_handoff": bool(getattr(convo, "is_human_handoff", False)),
        "status": str(getattr(convo, "status", "") or ""),
    }


def _get_active_handoff_session(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> Optional[HandoffSession]:
    row = (
        db.query(HandoffSession)
        .filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.customer_phone == customer_phone,
            HandoffSession.status == "active",
        )
        .first()
    )
    if row is None:
        return None
    status = getattr(row, "status", None)
    if isinstance(status, str) and status.strip().lower() == "active":
        return row
    return None


def _find_conversations_for_phone(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> list[Conversation]:
    from core.ai_disabled_gate import _find_conversations_for_phone  # noqa: PLC0415

    return _find_conversations_for_phone(db, tenant_id, customer_phone)


def has_possible_human_ownership_signals(convo: Any) -> bool:
    """Fast heuristic for fail-closed when gate verification fails."""
    if convo is None:
        return False
    if bool(getattr(convo, "ai_paused", False)):
        return True
    if str(getattr(convo, "status", "") or "").strip().lower() == "human":
        return True
    if is_explicit_takeover(convo):
        return True
    if has_implicit_takeover_signals(convo):
        return True
    if has_advisory_queue_signals(convo):
        return True
    return False


def aggregate_possible_human_ownership_signals(
    db: Session,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Conversation | None = None,
) -> bool:
    """True when ANY sibling conversation row shows human-ownership signals."""
    try:
        if _get_active_handoff_session(db, tenant_id, customer_phone) is not None:
            return True
    except Exception:
        return True

    convos = _find_conversations_for_phone(db, tenant_id, customer_phone)
    if conversation is not None:
        known_ids = {getattr(c, "id", None) for c in convos}
        if getattr(conversation, "id", None) not in known_ids:
            convos = list(convos) + [conversation]

    for convo in convos:
        if has_possible_human_ownership_signals(convo):
            return True
        try:
            if conversation_handoff_active(db, convo):
                return True
        except Exception:
            return True
    return False


def resolve_handoff_truth_active(
    db: Session | None,
    *,
    tenant_id: int | None = None,
    customer_phone: str | None = None,
    conversation: Conversation | None = None,
    outbound_metadata: dict[str, Any] | None = None,
    chosen_path: str = "",
    brain_handoff: bool = False,
) -> HandoffTruthResult:
    """
    Return whether operational handoff truth exists for outbound scrub.

    When ``db`` is unavailable, returns ``active=False`` (scrub promises).
    On verification failure under possible handoff, ``verify_failed=True``.
    """
    if db is None or not tenant_id or not customer_phone:
        return HandoffTruthResult(active=False, source="no_db_context")

    try:
        session = _get_active_handoff_session(db, int(tenant_id), customer_phone)
        if session is not None:
            return HandoffTruthResult(active=True, source="handoff_session_active")

        convos = _find_conversations_for_phone(db, int(tenant_id), customer_phone)
        if conversation is not None:
            known_ids = {getattr(c, "id", None) for c in convos}
            if getattr(conversation, "id", None) not in known_ids:
                convos = list(convos) + [conversation]

        for convo in convos:
            if conversation_handoff_active(db, convo):
                return HandoffTruthResult(
                    active=True,
                    source="ownership_human_active",
                )

        from modules.ai.brain.postprocess.staff_escalation_evidence import (  # noqa: PLC0415
            evaluate_staff_escalation_evidence,
        )

        for convo in convos:
            evidence = evaluate_staff_escalation_evidence(
                inbound_metadata=outbound_metadata,
                conversation_flags=conversation_flags_dict(convo),
                chosen_path=chosen_path,
                brain_handoff=brain_handoff,
            )
            if evidence.evidence_ok:
                return HandoffTruthResult(
                    active=True,
                    source=evidence.evidence_source or "staff_escalation_evidence",
                )

        if outbound_metadata or chosen_path:
            evidence = evaluate_staff_escalation_evidence(
                inbound_metadata=outbound_metadata,
                conversation_flags={},
                chosen_path=chosen_path,
                brain_handoff=brain_handoff,
            )
            if evidence.evidence_ok:
                return HandoffTruthResult(
                    active=True,
                    source=evidence.evidence_source or "metadata_escalation",
                )

        return HandoffTruthResult(active=False, source="no_handoff_truth")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[HANDOFF_TRUTH] verify_failed tenant=%s phone=%s err=%s",
            tenant_id,
            customer_phone,
            type(exc).__name__,
        )
        return HandoffTruthResult(
            active=False,
            source="verify_failed",
            verify_failed=True,
        )


def log_handoff_promise_scrub_decision(
    *,
    tenant_id: int | None,
    recipient: str | None,
    truth: HandoffTruthResult,
    scrubbed: bool,
) -> None:
    if scrubbed:
        logger.warning(
            "[HANDOFF_PROMISE_WIRE] decision=scrubbed tenant=%s to=%s "
            "truth_active=%s truth_source=%s verify_failed=%s",
            tenant_id,
            recipient,
            truth.active,
            truth.source,
            truth.verify_failed,
        )
    elif truth.active:
        logger.info(
            "[HANDOFF_PROMISE_WIRE] decision=allowed tenant=%s to=%s "
            "truth_source=%s",
            tenant_id,
            recipient,
            truth.source,
        )


def evaluate_gate_error_fail_closed(
    db: Session | None,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Conversation | None = None,
    gate: str,
    error: BaseException,
) -> bool:
    """
    Fail-closed on 'may AI reply?' / automated send when verification
    fails under possible human ownership. Returns True to suppress.
    """
    if db is None:
        logger.warning(
            "[AI_GATE_FAIL_OPEN] gate=%s tenant=%s phone=%s err=%s "
            "reason=no_db",
            gate,
            tenant_id,
            customer_phone,
            type(error).__name__,
        )
        return False

    try:
        if aggregate_possible_human_ownership_signals(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            conversation=conversation,
        ):
            logger.warning(
                "[AI_GATE_FAIL_CLOSED] gate=%s tenant=%s phone=%s "
                "reason=%s err=%s",
                gate,
                tenant_id,
                customer_phone,
                REASON_GATE_VERIFY_FAILED,
                type(error).__name__,
            )
            return True
    except Exception as agg_exc:  # noqa: BLE001
        logger.warning(
            "[AI_GATE_FAIL_CLOSED] gate=%s tenant=%s phone=%s "
            "reason=%s aggregate_err=%s original_err=%s",
            gate,
            tenant_id,
            customer_phone,
            REASON_GATE_VERIFY_FAILED,
            type(agg_exc).__name__,
            type(error).__name__,
        )
        return True

    logger.warning(
        "[AI_GATE_FAIL_OPEN] gate=%s tenant=%s phone=%s err=%s",
        gate,
        tenant_id,
        customer_phone,
        type(error).__name__,
    )
    return False


__all__ = [
    "HandoffTruthResult",
    "REASON_GATE_VERIFY_FAILED",
    "aggregate_possible_human_ownership_signals",
    "conversation_flags_dict",
    "evaluate_gate_error_fail_closed",
    "has_possible_human_ownership_signals",
    "log_handoff_promise_scrub_decision",
    "resolve_handoff_truth_active",
]
