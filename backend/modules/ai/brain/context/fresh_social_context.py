"""
fresh_social_context.py
─────────────────────────
Phase 1.5 — Fresh-context rule for lightweight social turns after long gaps.

When a customer sends a bare emoji / status reaction / short social message
after >7 days of silence, do not inject stale ``conversation_summary`` or
old social history into compose. Platform-wide; no tenant hardcoding.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.context.fresh_social")

FRESH_SOCIAL_GAP_DAYS = 7

_SOCIAL_HISTORY_MARKERS = re.compile(
    r"(?:"
    r"اقتراح|فكرت|فكرة|كما\s+قلت|قبل\s+شهر|سابق|"
    r"بالنسبة\s+ل|واقتراح|رأيك|رأي|ممتاز|تمام\s+التمام|"
    r"شكر|جزاك|بارك|ماشاء|الله\s+يس|حياك|نورت"
    r")",
    re.I | re.UNICODE,
)


def days_since_last_activity(state: Any) -> Optional[float]:
    """Days since last persisted brain-state activity (proxy for last turn)."""
    raw = str(getattr(state, "updated_at", "") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return max(delta.total_seconds() / 86400.0, 0.0)
    except (TypeError, ValueError):
        return None


def has_open_support_case(
    state: Any,
    *,
    human_priority: bool = False,
) -> bool:
    from modules.ai.brain.state.stages import STAGE_SUPPORT  # noqa: PLC0415

    if human_priority:
        return True
    if str(getattr(state, "stage", "") or "").strip().lower() == STAGE_SUPPORT:
        return True
    return False


def should_apply_fresh_social_context(
    *,
    inbound_text: str,
    state: Any,
    intent_name: str = "",
    primary_customer_goal: str = "",
    inbound_metadata: Optional[dict] = None,
    human_priority: bool = False,
) -> Tuple[bool, str]:
    """
    True when compose must drop stale summary/history for this social turn.
    """
    from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
        has_active_commerce_from_state,
        is_lightweight_social_turn,
    )

    if not is_lightweight_social_turn(
        inbound_text,
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        inbound_metadata=inbound_metadata,
    ):
        return False, "not_lightweight_social"

    if has_active_commerce_from_state(state):
        return False, "active_order"

    if has_open_support_case(state, human_priority=human_priority):
        return False, "open_support_case"

    gap_days = days_since_last_activity(state)
    if gap_days is None:
        return False, "no_activity_timestamp"
    if gap_days <= FRESH_SOCIAL_GAP_DAYS:
        return False, "within_gap_window"

    return True, "stale_gap_fresh_social"


def filter_history_for_fresh_social(
    history: Optional[List[Dict[str, Any]]],
    *,
    current_message: str,
) -> List[Dict[str, Any]]:
    """Return minimal history — current turn only, no stale tail."""
    _ = history
    msg = str(current_message or "").strip()
    if not msg:
        return []
    return [{"direction": "in", "body": msg}]


def filter_recent_turns_for_fresh_social(*, current_message: str) -> List[str]:
    msg = str(current_message or "").strip()
    if not msg:
        return []
    return [f"customer: {msg}"]


def history_looks_social_only(history: Optional[List[Dict[str, Any]]]) -> bool:
    """Heuristic for telemetry — True when tail is mostly social/non-commerce."""
    rows = list(history or [])
    if not rows:
        return False
    socialish = 0
    for row in rows[-6:]:
        body = str(row.get("body") or "")
        if not body.strip():
            continue
        if _SOCIAL_HISTORY_MARKERS.search(body):
            socialish += 1
            continue
        if len(body.strip()) <= 8:
            socialish += 1
    return socialish >= max(1, len(rows[-6:]) // 2)


def log_fresh_social_context(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    applied: bool = False,
    reason: str = "",
    gap_days: Optional[float] = None,
) -> None:
    try:
        logger.info(
            "[FRESH_SOCIAL_CONTEXT] tenant=%s phone=*%s applied=%s reason=%s gap_days=%s",
            tenant_id,
            phone_tail,
            applied,
            reason or "-",
            f"{gap_days:.1f}" if gap_days is not None else "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break turn
        pass


__all__ = [
    "FRESH_SOCIAL_GAP_DAYS",
    "days_since_last_activity",
    "filter_history_for_fresh_social",
    "filter_recent_turns_for_fresh_social",
    "has_open_support_case",
    "history_looks_social_only",
    "log_fresh_social_context",
    "should_apply_fresh_social_context",
]
