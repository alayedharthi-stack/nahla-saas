"""Dashboard conversation timeline — message_events are send-evidence source of truth."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set


def _trace_near_message_event(
    ts: Optional[datetime],
    me_times: Set[datetime],
    *,
    window_seconds: float = 3.0,
) -> bool:
    if not ts:
        return False
    for met in me_times:
        if met and abs((ts - met).total_seconds()) < window_seconds:
            return True
    return False


def append_trace_inbound_fallbacks(
    messages: List[Dict[str, Any]],
    trace_rows: Sequence[Any],
    me_times: Set[datetime],
    *,
    window_seconds: float = 3.0,
) -> None:
    """Legacy inbound-only trace backfill when message_events missed a row."""
    for idx, row in enumerate(trace_rows):
        if not getattr(row, "message", None):
            continue
        if _trace_near_message_event(
            getattr(row, "created_at", None),
            me_times,
            window_seconds=window_seconds,
        ):
            continue
        messages.append({
            "id": f"in-{idx}",
            "direction": "in",
            "body": row.message,
            "time": row.created_at.isoformat() if row.created_at else "",
            "isAI": False,
            "eventType": "customer",
            "_ts": row.created_at,
        })


def merge_trace_rows_into_timeline(
    messages: List[Dict[str, Any]],
    trace_rows: Sequence[Any],
    me_times: Set[datetime],
    *,
    include_trace_outbound: bool = False,
    window_seconds: float = 3.0,
) -> None:
    """Augment a message_events timeline with trace fallbacks.

    Outbound ``ConversationTrace.response_text`` must NEVER appear as a
    sent AI reply in the merchant timeline — only persisted outbound
    ``MessageEvent`` rows carry send/provider evidence.
    """
    append_trace_inbound_fallbacks(
        messages,
        trace_rows,
        me_times,
        window_seconds=window_seconds,
    )
    if not include_trace_outbound:
        return
    for idx, row in enumerate(trace_rows):
        if not getattr(row, "response_text", None):
            continue
        if _trace_near_message_event(
            getattr(row, "created_at", None),
            me_times,
            window_seconds=window_seconds,
        ):
            continue
        messages.append({
            "id": f"out-{idx}",
            "direction": "out",
            "body": row.response_text,
            "time": row.created_at.isoformat() if row.created_at else "",
            "isAI": bool(getattr(row, "orchestrator_used", False)),
            "eventType": "ai",
            "_ts": row.created_at,
        })
