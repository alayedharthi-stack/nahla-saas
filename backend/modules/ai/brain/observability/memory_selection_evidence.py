"""
memory_selection_evidence.py
────────────────────────────
Phase 1 — discover where stale conversation memory enters compose.

Emits structured log lines (no behavior change):
  [MEMORY_SELECTION]  candidate memory + selected/excluded + reason
  [CONTEXT_DECAY]     shadow expiry for ephemeral social memory
  [REPLY_CONTEXT]     sources actually injected into compose
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.observability.memory_selection")

MEMORY_PERSISTENT_COMMERCE = "persistent_commerce"
MEMORY_PERSISTENT_SUPPORT = "persistent_support"
MEMORY_EPHEMERAL_SOCIAL = "ephemeral_social"
MEMORY_HISTORY_TAIL = "history_tail"
MEMORY_CONVERSATION_SUMMARY = "conversation_summary"

CONTEXT_DECAY_DAYS_DEFAULT = 7


@dataclass
class MemoryCandidate:
    candidate_id: str
    memory_class: str
    memory_age_days: Optional[float] = None
    selected: bool = True
    selection_reason: str = ""


@dataclass
class ReplyContextSnapshot:
    sources_used: List[str] = field(default_factory=list)
    history_turns_injected: int = 0
    summary_chars: int = 0
    social_turn_count_in_window: int = 0
    lightweight_social_turn: bool = False
    fresh_social_context: bool = False
    fresh_social_reason: str = ""
    gap_days: Optional[float] = None


def _gap_days(state: Any) -> Optional[float]:
    try:
        from modules.ai.brain.context.fresh_social_context import (  # noqa: PLC0415
            days_since_last_activity,
        )

        return days_since_last_activity(state)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — gap helper must not break telemetry
        return None


def build_memory_candidates(
    *,
    state: Any,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    conversation_summary: str = "",
    fresh_social_context: bool = False,
    fresh_social_reason: str = "",
) -> List[MemoryCandidate]:
    gap = _gap_days(state)
    candidates: List[MemoryCandidate] = []

    summary = str(conversation_summary or "").strip()
    if summary:
        expired = gap is not None and gap > CONTEXT_DECAY_DAYS_DEFAULT
        selected = not (fresh_social_context and expired)
        candidates.append(MemoryCandidate(
            candidate_id="conversation_summary",
            memory_class=MEMORY_CONVERSATION_SUMMARY,
            memory_age_days=gap,
            selected=selected,
            selection_reason=(
                fresh_social_reason if fresh_social_context and not selected
                else ("active" if selected else "expired_ephemeral_shadow")
            ),
        ))

    hist = list(history or [])
    if hist:
        try:
            from modules.ai.brain.context.fresh_social_context import (  # noqa: PLC0415
                history_looks_social_only,
            )

            socialish = history_looks_social_only(hist)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — social heuristic optional for telemetry
            socialish = False
        mem_class = MEMORY_EPHEMERAL_SOCIAL if socialish else MEMORY_HISTORY_TAIL
        expired = gap is not None and gap > CONTEXT_DECAY_DAYS_DEFAULT and socialish
        selected = not (fresh_social_context and expired)
        candidates.append(MemoryCandidate(
            candidate_id="history_tail",
            memory_class=mem_class,
            memory_age_days=gap,
            selected=selected,
            selection_reason=(
                fresh_social_reason if fresh_social_context and not selected
                else ("injected" if selected else "expired_ephemeral_shadow")
            ),
        ))

    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )

        if has_active_commerce_from_state(state):
            candidates.append(MemoryCandidate(
                candidate_id="active_order",
                memory_class=MEMORY_PERSISTENT_COMMERCE,
                memory_age_days=0.0,
                selected=True,
                selection_reason="active_order",
            ))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — commerce probe optional for telemetry
        pass

    try:
        from modules.ai.brain.context.fresh_social_context import (  # noqa: PLC0415
            has_open_support_case,
        )

        if has_open_support_case(state):
            candidates.append(MemoryCandidate(
                candidate_id="support_case",
                memory_class=MEMORY_PERSISTENT_SUPPORT,
                memory_age_days=gap,
                selected=True,
                selection_reason="open_support",
            ))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — support probe optional for telemetry
        pass

    return candidates


def emit_memory_selection(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    candidates: Sequence[MemoryCandidate],
) -> None:
    for cand in candidates:
        try:
            logger.info(
                "[MEMORY_SELECTION] tenant=%s phone=*%s candidate=%s "
                "memory_class=%s memory_age_days=%s selected=%s reason=%s",
                tenant_id,
                phone_tail,
                cand.candidate_id,
                cand.memory_class,
                f"{cand.memory_age_days:.1f}" if cand.memory_age_days is not None else "-",
                cand.selected,
                cand.selection_reason or "-",
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            pass


def emit_context_decay_shadow(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    memory_class: str = MEMORY_EPHEMERAL_SOCIAL,
    memory_age_days: Optional[float] = None,
    expired: bool = False,
    would_exclude: bool = False,
) -> None:
    try:
        logger.info(
            "[CONTEXT_DECAY] tenant=%s phone=*%s memory_class=%s "
            "memory_age_days=%s expired=%s would_exclude=%s",
            tenant_id,
            phone_tail,
            memory_class,
            f"{memory_age_days:.1f}" if memory_age_days is not None else "-",
            expired,
            would_exclude,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass


def emit_reply_context(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    snapshot: ReplyContextSnapshot,
) -> None:
    try:
        logger.info(
            "[REPLY_CONTEXT] tenant=%s phone=*%s sources=%s "
            "history_turns=%s summary_chars=%s social_in_window=%s "
            "lightweight_social=%s fresh_social=%s fresh_reason=%s gap_days=%s",
            tenant_id,
            phone_tail,
            ",".join(snapshot.sources_used) or "-",
            snapshot.history_turns_injected,
            snapshot.summary_chars,
            snapshot.social_turn_count_in_window,
            snapshot.lightweight_social_turn,
            snapshot.fresh_social_context,
            snapshot.fresh_social_reason or "-",
            f"{snapshot.gap_days:.1f}" if snapshot.gap_days is not None else "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass


def emit_compose_memory_evidence(
    *,
    tenant_id: Optional[int] = None,
    phone: str = "",
    state: Any = None,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    conversation_summary: str = "",
    inbound_text: str = "",
    intent_name: str = "",
    primary_customer_goal: str = "",
    inbound_metadata: Optional[dict] = None,
    human_priority: bool = False,
    history_messages_count: int = 0,
    fresh_social_context: bool = False,
    fresh_social_reason: str = "",
) -> None:
    """Single helper for pipeline/responder hooks."""
    phone_tail = (phone or "")[-4:]
    gap = _gap_days(state)

    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            is_lightweight_social_turn,
        )

        lightweight = is_lightweight_social_turn(
            inbound_text,
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_metadata=inbound_metadata,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — lightweight probe optional for telemetry
        lightweight = False

    if not fresh_social_context:
        try:
            from modules.ai.brain.context.fresh_social_context import (  # noqa: PLC0415
                should_apply_fresh_social_context,
            )

            fresh_social_context, fresh_social_reason = should_apply_fresh_social_context(
                inbound_text=inbound_text,
                state=state,
                intent_name=intent_name,
                primary_customer_goal=primary_customer_goal,
                inbound_metadata=inbound_metadata,
                human_priority=human_priority,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — fresh-social shadow eval optional
            pass

    candidates = build_memory_candidates(
        state=state,
        history=history,
        conversation_summary=conversation_summary,
        fresh_social_context=fresh_social_context,
        fresh_social_reason=fresh_social_reason,
    )
    emit_memory_selection(
        tenant_id=tenant_id,
        phone_tail=phone_tail,
        candidates=candidates,
    )

    expired = gap is not None and gap > CONTEXT_DECAY_DAYS_DEFAULT
    emit_context_decay_shadow(
        tenant_id=tenant_id,
        phone_tail=phone_tail,
        memory_class=MEMORY_EPHEMERAL_SOCIAL,
        memory_age_days=gap,
        expired=expired,
        would_exclude=bool(fresh_social_context),
    )

    sources: List[str] = ["customer_profile"]
    summary_chars = len(str(conversation_summary or ""))
    if fresh_social_context:
        summary_chars = 0
    else:
        if summary_chars:
            sources.append("conversation_summary")
        if history:
            sources.append("history_turns")

    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )

        if has_active_commerce_from_state(state):
            sources.append("active_order")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — active-order probe optional for telemetry
        pass

    social_count = 0
    try:
        from modules.ai.brain.context.fresh_social_context import history_looks_social_only  # noqa: PLC0415

        if history_looks_social_only(list(history or [])):
            social_count = min(len(list(history or [])), 6)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — social count optional for telemetry
        pass

    emit_reply_context(
        tenant_id=tenant_id,
        phone_tail=phone_tail,
        snapshot=ReplyContextSnapshot(
            sources_used=sources,
            history_turns_injected=history_messages_count,
            summary_chars=summary_chars,
            social_turn_count_in_window=social_count,
            lightweight_social_turn=lightweight,
            fresh_social_context=fresh_social_context,
            fresh_social_reason=fresh_social_reason,
            gap_days=gap,
        ),
    )


__all__ = [
    "CONTEXT_DECAY_DAYS_DEFAULT",
    "MemoryCandidate",
    "ReplyContextSnapshot",
    "build_memory_candidates",
    "emit_compose_memory_evidence",
    "emit_context_decay_shadow",
    "emit_memory_selection",
    "emit_reply_context",
    "MEMORY_CONVERSATION_SUMMARY",
    "MEMORY_EPHEMERAL_SOCIAL",
    "MEMORY_HISTORY_TAIL",
    "MEMORY_PERSISTENT_COMMERCE",
    "MEMORY_PERSISTENT_SUPPORT",
]
