"""
core/ownership_state — Real Handoff Slice 1 (platform-wide).

Derived ownership lifecycle for human/AI coexistence. No DB column;
reads existing Conversation flags + MessageEvent history.

Doctrine:
    Customer escalation ≠ AI off.
    Manual staff activity ≠ AI off.
    Implicit takeover residue is audit/queue history, not AI execution.
    Staff-idle TTL must not turn AI on or off.
    Conversation-level AI off is explicit ``ai_paused`` only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.ai_pause_guard import (
    REASON_BOT_LOOP,
    HUMAN_PRESENCE_REASONS,
)
from models import Conversation, MessageEvent

# ── Ownership states (derived, not persisted) ─────────────────────────────
OWNERSHIP_AI_PRIMARY = "ai_primary"
OWNERSHIP_HUMAN_REQUESTED = "human_requested"
OWNERSHIP_HUMAN_ACTIVE = "human_active"
OWNERSHIP_HUMAN_IDLE = "human_idle"

TAKEOVER_NONE = "none"
TAKEOVER_IMPLICIT = "implicit"
TAKEOVER_EXPLICIT = "explicit"

_EXPLICIT_TAKEN_OVER_PREFIXES = (
    "dashboard:handoff",
    "system:loop_pause",
)


@dataclass(frozen=True)
class OwnershipStateResult:
    state: str
    takeover_class: str
    staff_idle_sec: Optional[int] = None
    customer_waiting_after_staff: bool = False
    last_staff_outbound_at: Optional[datetime] = None
    reason: str = ""


@dataclass(frozen=True)
class ImplicitTakeoverRecoveryResult:
    released: bool
    previous_state: str = ""
    new_state: str = ""
    reason: str = ""
    staff_idle_sec: Optional[int] = None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_explicit_takeover(convo: Any) -> bool:
    """Dashboard /handoff, loop guard, or ai_paused with handoff reasons."""
    if convo is None:
        return False
    if bool(getattr(convo, "ai_paused", False)):
        reason = str(getattr(convo, "ai_paused_reason", "") or "").strip()
        if reason in HUMAN_PRESENCE_REASONS or reason == REASON_BOT_LOOP:
            return True
    taken_by = str(getattr(convo, "taken_over_by", "") or "").strip()
    if any(taken_by.startswith(p) for p in _EXPLICIT_TAKEN_OVER_PREFIXES):
        return True
    if taken_by == "dashboard:handoff":
        return True
    return False


def has_implicit_takeover_signals(convo: Any) -> bool:
    if convo is None:
        return False
    return bool(
        getattr(convo, "paused_by_human", False)
        or getattr(convo, "taken_over_at", None) is not None
    )


def has_advisory_queue_signals(convo: Any) -> bool:
    if convo is None:
        return False
    status = str(getattr(convo, "status", "") or "").strip().lower()
    return bool(
        getattr(convo, "needs_human", False)
        or getattr(convo, "handoff_active", False)
        or getattr(convo, "is_human_handoff", False)
        or status == "human"
    )


def _is_manual_outbound_event(row: MessageEvent) -> bool:
    if (row.event_type or "").startswith("manual"):
        return True
    meta = row.extra_metadata or {}
    return isinstance(meta, dict) and meta.get("is_ai") is False


def staff_last_manual_outbound_at(
    db: Session,
    convo: Conversation,
) -> Optional[datetime]:
    """Most recent staff manual outbound timestamp, if any."""
    if convo is None or db is None:
        return None
    try:
        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == convo.tenant_id,
                MessageEvent.conversation_id == convo.id,
                MessageEvent.direction == "outbound",
            )
            .order_by(MessageEvent.id.desc())
            .limit(20)
            .all()
        )
    except Exception:
        return _aware(getattr(convo, "taken_over_at", None))

    for row in rows:
        if _is_manual_outbound_event(row):
            return _aware(getattr(row, "created_at", None))
    return _aware(getattr(convo, "taken_over_at", None))


def customer_waiting_after_staff(
    db: Session,
    convo: Conversation,
    *,
    last_staff_at: Optional[datetime],
    now: Optional[datetime] = None,
    assume_current_inbound: bool = False,
) -> bool:
    """True when the customer messaged after the last staff outbound."""
    staff_at = _aware(last_staff_at)
    if staff_at is None or convo is None:
        return False
    now_ = _aware(now) or datetime.now(timezone.utc)
    if now_ <= staff_at:
        return False
    if assume_current_inbound:
        return True
    if db is None:
        return True
    try:
        last_in = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == convo.tenant_id,
                MessageEvent.conversation_id == convo.id,
                MessageEvent.direction == "inbound",
            )
            .order_by(MessageEvent.id.desc())
            .first()
        )
    except Exception:
        # Current inbound is being processed — treat as waiting.
        return True
    if last_in is None:
        return True
    inbound_at = _aware(getattr(last_in, "created_at", None))
    if inbound_at is None:
        return True
    return inbound_at > staff_at


def resolve_ownership_state(
    db: Session,
    convo: Any,
    *,
    now: Optional[datetime] = None,
    assume_current_inbound: bool = False,
) -> OwnershipStateResult:
    """Derive platform-wide ownership state from flags + message history."""
    now_ = _aware(now) or datetime.now(timezone.utc)

    if convo is None:
        return OwnershipStateResult(
            state=OWNERSHIP_AI_PRIMARY,
            takeover_class=TAKEOVER_NONE,
            reason="no_conversation",
        )

    if is_explicit_takeover(convo):
        return OwnershipStateResult(
            state=OWNERSHIP_HUMAN_ACTIVE,
            takeover_class=TAKEOVER_EXPLICIT,
            reason="explicit_takeover",
        )

    # Implicit staff activity (paused_by_human / taken_over_at) is
    # leftover audit residue. It must not become HUMAN_ACTIVE or
    # HUMAN_IDLE, and must not control AI execution. Idle timestamps
    # remain observational only — TTL never flips AI on/off.
    last_staff = None
    idle_sec: Optional[int] = None
    waiting = False
    if has_implicit_takeover_signals(convo):
        last_staff = staff_last_manual_outbound_at(db, convo)
        if last_staff is not None:
            idle_sec = max(0, int((now_ - last_staff).total_seconds()))
        waiting = customer_waiting_after_staff(
            db,
            convo,
            last_staff_at=last_staff,
            now=now_,
            assume_current_inbound=assume_current_inbound,
        )

    if has_advisory_queue_signals(convo):
        return OwnershipStateResult(
            state=OWNERSHIP_HUMAN_REQUESTED,
            takeover_class=TAKEOVER_NONE,
            staff_idle_sec=idle_sec,
            customer_waiting_after_staff=waiting,
            last_staff_outbound_at=last_staff,
            reason="advisory_queue",
        )

    residue = has_implicit_takeover_signals(convo)
    return OwnershipStateResult(
        state=OWNERSHIP_AI_PRIMARY,
        takeover_class=TAKEOVER_NONE,
        staff_idle_sec=idle_sec,
        customer_waiting_after_staff=waiting,
        last_staff_outbound_at=last_staff,
        reason="legacy_implicit_residue_ignored" if residue else "default",
    )


def release_implicit_takeover(
    convo: Conversation,
    *,
    reason: str = "staff_idle_ttl",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Clear implicit takeover signals; preserve queue/advisory flags."""
    now_ = _aware(now) or datetime.now(timezone.utc)
    audit: dict[str, Any] = {
        "ownership_previous_taken_over_by": getattr(convo, "taken_over_by", None),
        "ownership_previous_taken_over_at": (
            convo.taken_over_at.isoformat()
            if getattr(convo, "taken_over_at", None) else None
        ),
        "ownership_release_reason": reason,
        "ownership_released_at": now_.isoformat(),
    }
    convo.paused_by_human = False
    convo.taken_over_at = None
    convo.taken_over_by = None
    try:
        meta = dict(convo.extra_metadata or {})
        meta.update(audit)
        convo.extra_metadata = meta
        flag_modified(convo, "extra_metadata")
    except Exception:
        pass
    return audit


def attempt_implicit_takeover_recovery(
    db: Session,
    convo: Conversation,
    *,
    now: Optional[datetime] = None,
    assume_current_inbound: bool = True,
) -> ImplicitTakeoverRecoveryResult:
    """TTL must not turn AI on or off.

    Implicit residue is not an AI owner. This stays a no-op so webhook
    callers need not change. ``ai_paused`` and queue flags are untouched.
    """
    before = resolve_ownership_state(
        db, convo, now=now, assume_current_inbound=assume_current_inbound,
    )
    return ImplicitTakeoverRecoveryResult(
        released=False,
        previous_state=before.state,
        new_state=before.state,
        reason="ttl_does_not_control_ai",
        staff_idle_sec=before.staff_idle_sec,
    )


def conversation_handoff_active(
    db: Session,
    convo: Any,
    *,
    now: Optional[datetime] = None,
    assume_current_inbound: bool = False,
) -> bool:
    """True only for explicit dashboard/loop takeover labels.

    Not an AI on/off signal. Implicit staff activity never returns True.
    """
    result = resolve_ownership_state(
        db, convo, now=now, assume_current_inbound=assume_current_inbound,
    )
    return result.state == OWNERSHIP_HUMAN_ACTIVE


__all__ = [
    "OWNERSHIP_AI_PRIMARY",
    "OWNERSHIP_HUMAN_ACTIVE",
    "OWNERSHIP_HUMAN_IDLE",
    "OWNERSHIP_HUMAN_REQUESTED",
    "ImplicitTakeoverRecoveryResult",
    "OwnershipStateResult",
    "TAKEOVER_EXPLICIT",
    "TAKEOVER_IMPLICIT",
    "TAKEOVER_NONE",
    "attempt_implicit_takeover_recovery",
    "conversation_handoff_active",
    "customer_waiting_after_staff",
    "has_advisory_queue_signals",
    "has_implicit_takeover_signals",
    "is_explicit_takeover",
    "release_implicit_takeover",
    "resolve_ownership_state",
    "staff_last_manual_outbound_at",
]
